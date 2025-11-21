# core/bots/offshore.py
import os
import re
import unicodedata
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://offshoreleaks.icij.org/"
NOMBRE_SITIO = "offshore"

def _norm(s: str) -> str:
    """Normaliza para comparación exacta: quita tildes, compacta espacios y casefold."""
    s = (s or "").strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s).strip()
    return s.casefold()

async def _tabla_tiene_match_exacto(page, nombre_objetivo: str) -> bool:
    """
    Revisa ÚNICAMENTE la tabla de resultados dentro de #search_results.
    Devuelve True si algún <td:first-child><a> (columna Entity) coincide EXACTAMENTE con el nombre.
    """
    try:
        # Esperar a que aparezca el contenedor de resultados o se llene la tabla
        try:
            await page.wait_for_selector("#search_results", timeout=20000)
        except Exception:
            pass

        # Esperar a que se oculte el spinner si existe
        try:
            sp = page.locator("img[data-spinner]")
            if await sp.count() > 0:
                await sp.first.wait_for(state="hidden", timeout=15000)
        except Exception:
            pass

        sel_links = "#search_results table tbody tr td:first-child a"
        links = page.locator(sel_links)
        n = await links.count()
        objetivo = _norm(nombre_objetivo)
        for i in range(n):
            txt = (await links.nth(i).inner_text() or "").strip()
            if _norm(txt) == objetivo:
                return True
    except Exception:
        pass
    return False

async def consultar_offshore(consulta_id: int, nombre: str, cedula):
    """
    Abre Offshore Leaks (ICIJ), busca 'nombre' y toma hasta 3 capturas.
    Reintenta hasta 3 veces si hay error, guardando pantallazo en cada intento.
    Guarda en MEDIA_ROOT/resultados/<consulta_id>/.
    Score: 5 si hay coincidencia EXACTA en la tabla de resultados (#search_results), si no 1.
    """
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = re.sub(r"\s+", "_", (nombre or "consulta").strip()) or "consulta"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    intentos = 0
    max_intentos = 3
    rel_paths = []
    error_final = None

    while intentos < max_intentos:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                page = await context.new_page()

                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                # Popup / consentimiento
                try:
                    await page.locator('input#accept').check(timeout=8000)
                    await page.locator('button.btn.btn-primary.btn-block.btn-lg').click(timeout=4000)
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass

                # Buscar
                await page.wait_for_selector('input[name="q"]', timeout=15000)
                await page.fill('input[name="q"]', nombre or "")
                await page.keyboard.press("Enter")
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(3000)

                # Flag de coincidencia exacta en cualquiera de las páginas
                found_exact = False

                # Capturar hasta 3 páginas y evaluar coincidencia en cada una
                for i in range(1, 4):
                    # Evaluar coincidencia exacta SOLO en la tabla de resultados
                    if not found_exact:
                        try:
                            if await _tabla_tiene_match_exacto(page, nombre):
                                found_exact = True
                        except Exception:
                            pass

                    # Captura
                    png_name = f"{NOMBRE_SITIO}_{cedula}_{ts}_page{i}.png"
                    absolute_path = os.path.join(absolute_folder, png_name)
                    relative_path = os.path.join(relative_folder, png_name).replace("\\", "/")
                    await page.screenshot(path=absolute_path, full_page=True)
                    rel_paths.append(relative_path)

                    # Siguiente página si existe
                    next_button = page.locator('a.page-link[aria-label="Next »"]')
                    try:
                        if await next_button.count() and await next_button.is_enabled():
                            await next_button.click()
                            try:
                                await page.wait_for_load_state("networkidle", timeout=15000)
                            except Exception:
                                pass
                            await page.wait_for_timeout(3000)
                        else:
                            break
                    except Exception:
                        break

                await browser.close()

            # Registrar en BD (score según coincidencia exacta)
            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
            score = 5 if 'found_exact' in locals() and found_exact else 1
            mensaje = "Coincidencia exacta encontrada" if score == 5 else "Sin coincidencia exacta en resultados"
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validado",
                mensaje=mensaje,
                archivo=",".join(rel_paths)
            )
            return

        except Exception as e:
            intentos += 1
            error_final = e

            # Inicializar rutas para evitar errores si ocurre excepción antes de su definición
            error_png = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{cedula}_{ts}_error{intentos}.png")
            try:
                if 'page' in locals():
                    await page.screenshot(path=error_png, full_page=True)
                else:
                    with open(error_png, "wb") as f:
                        f.write(b"\x89PNG\r\n\x1a\n")  # PNG mínimo
                rel_paths.append(os.path.join(relative_folder, os.path.basename(error_png)).replace("\\", "/"))
            except Exception:
                pass

            if intentos < max_intentos:
                continue  # reintenta

    # Falló 3 veces → registrar error
    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=0,
        estado="Sin validar",
        mensaje="Ocurrió un problema al obtener la información de la fuente",
        archivo=",".join(rel_paths)
    )
