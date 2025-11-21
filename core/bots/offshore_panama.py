# core/bots/offshore_panama.py
import os
import re
import unicodedata
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://offshoreleaks.icij.org/investigations/panama-papers"
NOMBRE_SITIO = "offshore_panama"

def _norm_name(s: str) -> str:
    """
    Normaliza para comparación 'exacta visual':
    - Quita diacríticos
    - Insensible a mayúsculas
    - Colapsa espacios
    - Normaliza coma a ', '
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s*,\s*", ", ", s)  # coma consistente
    s = re.sub(r"\s+", " ", s)      # colapsa espacios
    return s.strip().upper()

async def consultar_offshore_panama(consulta_id: int, cedula: str, nombre: str):
    """
    Abre Panama Papers (ICIJ), busca 'nombre' y toma hasta 3 capturas.
    - Coincidencia exacta (columna Officer) => score=5
    - Sin coincidencia exacta / No results => score=1
    Reintenta hasta 3 veces y guarda pantallazo en errores.
    """
    # Carpetas
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

    # Reintentos
    for intento in range(1, 4):
        page = None
        browser = None
        try:
            rel_paths = []
            match_exacto = False
            no_results = False
            mensaje_nores = "Sin coincidencias"
            resultados_count = 0

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

                # 2) Consentimiento
                try:
                    await page.locator('input#accept').check(timeout=3000)
                    await page.locator('button.btn.btn-primary.btn-block.btn-lg').click(timeout=3000)
                    await asyncio.sleep(1.2)
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

                # 3) Buscar nombre
                await page.wait_for_selector('input[name="q"]', timeout=15000)
                await page.fill('input[name="q"]', nombre or "")
                await page.keyboard.press("Enter")
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await asyncio.sleep(2)

                # 4) Verificación + capturas hasta 3 páginas
                for i in range(1, 4):
                    # ¿No results?
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

                    # Leer filas de la tabla principal (Officer en la 1ra columna)
                    filas = page.locator("table.search__results__table tbody tr")
                    try:
                        n = await filas.count()
                        resultados_count = max(resultados_count, n)
                        for k in range(n):
                            fila = filas.nth(k)
                            try:
                                texto = await fila.locator("td:nth-child(1)").inner_text(timeout=1500)
                            except Exception:
                                texto = ""
                            if _norm_name(texto) == objetivo:
                                match_exacto = True
                                break
                    except Exception:
                        pass

                    # Captura de evidencia
                    png_name = f"{NOMBRE_SITIO}_{safe_nombre}_{ts}_page{i}.png"
                    abs_path = os.path.join(absolute_folder, png_name)
                    rel_path = os.path.join(relative_folder, png_name).replace("\\", "/")
                    await page.screenshot(path=abs_path, full_page=True)
                    rel_paths.append(rel_path)

                    if match_exacto or no_results:
                        break  # ya tenemos veredicto

                    # Paginación
                    next_button = page.locator('a.page-link[aria-label="Next »"]')
                    try:
                        if await next_button.count() and await next_button.is_enabled():
                            await next_button.click()
                            try:
                                await page.wait_for_load_state("networkidle", timeout=15000)
                            except Exception:
                                pass
                            await asyncio.sleep(2)
                        else:
                            break
                    except Exception:
                        break

                # --- score + mensaje ---
                if no_results or resultados_count == 0:
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
                archivo=",".join(rel_paths) if rel_paths else ""
            )
            return  # éxito

        except Exception as e:
            # Pantallazo de error
            error_png = f"{NOMBRE_SITIO}_{safe_nombre}_{ts}_error_intento{intento}.png"
            abs_error_path = os.path.join(absolute_folder, error_png)
            rel_error_path = os.path.join(relative_folder, error_png).replace("\\", "/")
            try:
                if page is not None:
                    await page.screenshot(path=abs_error_path, full_page=True)
                else:
                    rel_error_path = ""
            except Exception:
                rel_error_path = ""

            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass

            # Último intento → guardar error
            if intento == 3:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin Validar",
                    mensaje="Ocurrió un problema al obtener la información de la fuente",
                    archivo=rel_error_path
                )
