import os
import re
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://www.nationalcrimeagency.gov.uk/most-wanted"
NOMBRE_SITIO = "nca_most_wanted"

# ----------------- Utilidades -----------------
def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

async def _screenshot_page(page, out_png_abs: str):
    os.makedirs(os.path.dirname(out_png_abs), exist_ok=True)
    await page.screenshot(path=out_png_abs, full_page=True)

PRINT_FIX = """
@media print {
  header, .header, #header, .site-header, .nca-header, .masthead, .navbar,
  .global-header, .cookiebar, .ccc-notify, .ccc-header { display: block !important; }
  .no-print { display: block !important; }
}
"""

SEARCH_SELECTORS = [
    "#mod-finder-searchword253",
    "input[name='q'][role='searchbox']",
    "input[placeholder*='Search' i]",
    "input[type='search']",
    "input.search-query",
]

COOKIE_BTN = "#ccc-recommended-settings"

# ----------------- Bot principal -----------------
async def consultar_nca_most_wanted_pdf(consulta_id: int, nombre: str, cedula):
    nombre = (nombre or "").strip()
    if not nombre:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje="El nombre llegó vacío.",
            archivo="",
        )
        return

    rel_folder = os.path.join("resultados", str(consulta_id))
    abs_folder = os.path.join(settings.MEDIA_ROOT, rel_folder)
    os.makedirs(abs_folder, exist_ok=True)

    safe = _safe_name(nombre)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_abs = os.path.join(abs_folder, f"{NOMBRE_SITIO}_{cedula}_{ts}.png")
    screenshot_rel = os.path.join(rel_folder, os.path.basename(screenshot_abs))

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-GB")
            page = await ctx.new_page()
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # Cookies
            try:
                await page.wait_for_selector(COOKIE_BTN, state="visible", timeout=2500)
                await page.locator(COOKIE_BTN).click(timeout=2500)
            except Exception:
                pass

            # Campo búsqueda
            search = None
            for sel in SEARCH_SELECTORS:
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=2500)
                    cand = page.locator(sel).first
                    if await cand.is_visible():
                        search = cand
                        break
                except Exception:
                    continue

            if not search:
                search = page.get_by_role("searchbox").first

            await search.click(force=True)
            await search.fill(nombre)
            try:
                async with page.expect_navigation(timeout=15000):
                    await search.press("Enter")
            except Exception:
                await page.wait_for_timeout(3000)

            await page.wait_for_timeout(3000)

            # Revisar si existe contenedor de "No Results Found"
            no_result = page.locator("#search-result-empty").first
            if await no_result.count() > 0:
                texto_div = (await no_result.locator("p").text_content()) or "No Results Found"
                score = 0
                mensaje = texto_div.strip()
            else:
                score = 10
                mensaje = "Posible hallazgo"

            # Pantallazo siempre
            await page.add_style_tag(content=PRINT_FIX)
            await page.emulate_media(media="print")
            await _screenshot_page(page, screenshot_abs)

            await ctx.close()
            await browser.close()

        # Guardar en BD
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score,
            estado="Validada",
            mensaje=mensaje,
            archivo=screenshot_rel,
        )

    except Exception as e:
        try:
            await _screenshot_page(page, screenshot_abs)
        except Exception:
            pass
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje=f"Error: {e}",
            archivo=screenshot_rel,
        )
