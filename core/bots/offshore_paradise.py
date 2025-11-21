# core/bots/offshore_paradise.py
import os
import re
import unicodedata
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://offshoreleaks.icij.org/investigations/paradise-papers"
NOMBRE_SITIO = "offshore_paradise"

def _norm_name(s: str) -> str:
    """
    Normaliza para comparación exacta visual (insensible a mayúsculas y espacios dobles).
    - Uppercase
    - Quita diacríticos
    - Colapsa múltiples espacios
    - Normaliza espacios alrededor de comas
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s*,\s*", ", ", s)       # ", " consistente
    s = re.sub(r"\s+", " ", s)            # colapsa espacios
    return s.strip().upper()

async def consultar_offshore_paradise(consulta_id: int, cedula: str, nombre: str):
    """
    Abre Paradise Papers (ICIJ), busca 'nombre' y toma hasta 3 capturas.
    - Coincidencia exacta en columna 'Officer' => score=5
    - Sin coincidencia exacta => score=1
    Reintenta hasta 3 veces ante error.
    """
    # --- paths ---
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe_nombre = re.sub(r"[^\w\.-]+", "_", (nombre or "consulta").strip()) or "consulta"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    if not nombre:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje="Nombre vacío para la consulta.",
            archivo=""
        )
        return

    objetivo = _norm_name(nombre)

    # ---- reintentos ----
    for intento in range(1, 4):
        page = None
        browser = None
        try:
            archivos = []
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    locale="es-ES"
                )
                page = await context.new_page()

                # 1) Ir al sitio
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                # 2) Consentimiento (best effort)
                try:
                    await page.locator('input#accept').check(timeout=3000)
                    await page.locator('button.btn.btn-primary.btn-block.btn-lg').click(timeout=3000)
                    await page.wait_for_timeout(1500)
                except Exception:
                    for sel in (
                        "button:has-text('I accept')",
                        "button:has-text('Accept all')",
                        "#onetrust-accept-btn-handler",
                    ):
                        try:
                            await page.locator(sel).first.click(timeout=1200)
                            break
                        except Exception:
                            pass

                # 3) Buscar
                await page.wait_for_selector('input[name="q"]', timeout=15000)
                await page.fill('input[name="q"]', nombre or "")
                await page.keyboard.press("Enter")
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(2500)

                # ¿No results?
                no_results = False
                mensaje_nores = "Sin coincidencias"
                for sel in ("text=No results", ".no-results", ".alert-warning:has-text('No results')"):
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() and await loc.is_visible():
                            try:
                                mensaje_nores = (await loc.inner_text()) or mensaje_nores
                            except Exception:
                                pass
                            no_results = True
                            break
                    except Exception:
                        pass

                # 4) Contar/leer filas de la tabla principal
                match_exacto = False
                filas = page.locator("table.search__results__table tbody tr")
                try:
                    n = await filas.count()
                    for i in range(n):
                        fila = filas.nth(i)
                        # Columna Officer -> primer <td> (el <a> tiene el nombre)
                        try:
                            texto = await fila.locator("td:nth-child(1)").inner_text(timeout=1500)
                        except Exception:
                            texto = ""
                        if _norm_name(texto) == objetivo:
                            match_exacto = True
                            break
                except Exception:
                    pass

                # 5) Capturas hasta 3 páginas
                for i in range(1, 4):
                    png_name = f"{NOMBRE_SITIO}_{safe_nombre}_{ts}_page{i}.png"
                    abs_path = os.path.join(absolute_folder, png_name)
                    rel_path = os.path.join(relative_folder, png_name).replace("\\", "/")
                    await page.screenshot(path=abs_path, full_page=True)
                    archivos.append(rel_path)

                    # Paginación
                    next_button = page.locator('a.page-link[aria-label="Next »"]')
                    try:
                        if await next_button.count() and await next_button.is_enabled():
                            await next_button.click()
                            try:
                                await page.wait_for_load_state("networkidle", timeout=15000)
                            except Exception:
                                pass
                            await page.wait_for_timeout(2500)
                        else:
                            break
                    except Exception:
                        break

                # --- score + mensaje ---
                if no_results:
                    score = 1
                    mensaje = mensaje_nores
                else:
                    if match_exacto:
                        score = 5
                        mensaje = "Se encontraron hallazgos."
                    else:
                        score = 1
                        mensaje = "No se encontraron coincidencias exactas."

                try:
                    await browser.close()
                except Exception:
                    pass

            # Guardar resultado exitoso
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validada",
                mensaje=mensaje,
                archivo=",".join(archivos) if archivos else ""
            )
            return

        except Exception as e:
            # Captura de error del intento
            error_png = f"{NOMBRE_SITIO}_{safe_nombre}_{ts}_error_intento{intento}.png"
            abs_error = os.path.join(absolute_folder, error_png)
            rel_error = os.path.join(relative_folder, error_png).replace("\\", "/")
            try:
                if page is not None:
                    await page.screenshot(path=abs_error, full_page=True)
                else:
                    rel_error = ""
            except Exception:
                rel_error = ""

            print(f"[ERROR] Intento {intento} falló: {e}")

            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass

            if intento == 3:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin Validar",
                    mensaje="Ocurrió un problema al obtener la información de la fuente",
                    archivo=rel_error
                )
