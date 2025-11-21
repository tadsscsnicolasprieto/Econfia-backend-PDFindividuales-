# consulta/eu_fin_sanctions.py
import os, re, asyncio, unicodedata
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = ("https://data.europa.eu/data/datasets/"
       "consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions?locale=en")
NOMBRE_SITIO = "eu_fin_sanctions"

PRINT_CLEAN = """
@media print {
  header, nav, footer, .ecl-footer, .ecl-site-header, .ecl-site-footer,
  .cookie-banner, .ot-sdk-container, .onetrust-pc-dark-filter, .onetrust-banner-sdk,
  .ecl-u-mt-m, .ecl-u-mb-l, .ecl-u-mt-l, .ecl-u-my-l, .ecl-u-pt-l, .ecl-u-pb-l,
  .api-custom, .ec-sort, .ecl-pagination, .ecl-u-flex, .btn-group,
  .ecl-site-header__banner, .ecl-u-d-print-none { display: none !important; }
  body { margin: 0 !important; padding: 0 !important; }
}
html, body { overflow: visible !important; }
"""

# --- Espera robusta ---
MAX_WAIT_RESULTS_MS = 210_000  # 120s
POLL_MS = 1_000

SKELETON_SELECTORS = [
    ".skeleton", ".ecl-skeleton", ".loading",
    ".ecl-spinner", ".ecl-loader", "[aria-busy='true']",
]

RESULT_HEADLINE_SEL = "div.ds-result-headline"
RESULT_TITLES_SEL = (
    "a.dataset-info-box div[data-cy='dataset-title'] h2.card-title, "
    "a.dataset-info-box h2.card-title, "
    "[data-cy='dataset-title'] .card-title, "
    "h2.card-title"
)

def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s).strip()
    return s.casefold()

async def _click_cookies(page):
    selectors = [
        "button:has-text('Accept all cookies')",
        "button:has-text('Accept only essential cookies')",
        "button#onetrust-accept-btn-handler",
        "button:has-text('Accept cookies')",
        "button:has-text('Accept all')",
    ]
    for sel in selectors:
        try:
            await page.locator(sel).first.click(timeout=1500)
            return True
        except Exception:
            pass
    return False

async def _wait_results(page):
    """Espera a que aparezcan resultados y desaparezcan loaders/skeletons."""
    # disparadores suaves de carga
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.8)
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass

    elapsed = 0
    while elapsed < MAX_WAIT_RESULTS_MS:
        try:
            # ¿headline visible?
            if await page.locator(RESULT_HEADLINE_SEL).count() > 0:
                # esperar a que se oculten skeletons si los hay
                all_hidden = True
                for sel in SKELETON_SELECTORS:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        try:
                            await loc.first.wait_for(state="hidden", timeout=1_000)
                        except Exception:
                            all_hidden = False
                            break
                if all_hidden:
                    # pequeño respiro final para rehidratación
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5_000)
                    except Exception:
                        pass
                    return True

            # ¿títulos ya renderizados?
            if await page.locator(RESULT_TITLES_SEL).count() > 0:
                try:
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass
                return True
        except Exception:
            pass

        await asyncio.sleep(POLL_MS / 1000)
        elapsed += POLL_MS

    return False

async def consultar_eu_fin_sanctions(consulta_id: int, nombre_completo: str):
    """
    Abre el dataset EU sanctions, busca `nombre_completo`, toma PNG y PDF, y decide:
      - Si aparece “datasets found (0)” => score=0, mensaje exactamente “datasets found (0)”.
      - Si hay resultados, verifica coincidencia EXACTA por título de dataset:
          * true => score=10, mensaje="se encontraron coincidencias"
          * false => score=0, mensaje="no se han encontrado coincidencias"
    Guarda PNG (o PDF si PNG falla) en resultados/<consulta_id>/ y crea el registro.
    """
    navegador = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    nombre_completo = (nombre_completo or "").strip()
    if not nombre_completo:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje="Nombre vacío para la consulta.", archivo=""
        )
        return

    # Carpeta destino
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    # Archivos
    safe = re.sub(r"[^\w\.-]+", "_", nombre_completo) or "consulta"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_name = f"{NOMBRE_SITIO}_{safe}_{timestamp}.pdf"
    png_name = f"{NOMBRE_SITIO}_{safe}_{timestamp}.png"
    out_pdf_abs = os.path.join(absolute_folder, pdf_name)
    out_pdf_rel = os.path.join(relative_folder, pdf_name)
    out_png_abs = os.path.join(absolute_folder, png_name)
    out_png_rel = os.path.join(relative_folder, png_name)

    score_final = 0
    mensaje_final = "ocurrio un error"
    final_url = URL

    try:
        # -------- FASE 1: Navegar, buscar y SCREENSHOT (headful) --------
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            context = await navegador.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="en-US"
            )
            page = await context.new_page()

            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            await _click_cookies(page)

            # Buscar por nombre
            await page.wait_for_selector("input.search-input", timeout=20000)
            await page.fill("input.search-input", nombre_completo)
            await asyncio.sleep(0.3)
            # botón de buscar (a veces hay varios)
            for sel in ["button.search-button", "button.ecl-button", "button[type='submit']"]:
                try:
                    await page.locator(sel).first.click(timeout=1500)
                    break
                except Exception:
                    continue

            # --- Espera robusta a resultados/skeletons ---
            await _wait_results(page)

            # HEADLINE: ¿datasets found (0)?
            headline = page.locator(RESULT_HEADLINE_SEL).first
            zero_detected = False
            if await headline.count() > 0 and await headline.is_visible():
                try:
                    htxt = (await headline.inner_text()).strip()
                    htxt_norm = re.sub(r"\s+", " ", htxt)
                    if re.search(r"datasets found\s*\(\s*0\s*\)", htxt_norm, flags=re.I):
                        zero_detected = True
                        score_final = 0
                        mensaje_final = "datasets found (0)"
                except Exception:
                    pass

            # Si no es 0, buscar coincidencia EXACTA en títulos
            if not zero_detected:
                objetivo = _norm(nombre_completo)
                exact_match = False
                try:
                    titles = page.locator(RESULT_TITLES_SEL)
                    n = await titles.count()
                    for i in range(n):
                        t = (await titles.nth(i).inner_text() or "").strip()
                        if _norm(t) == objetivo:
                            exact_match = True
                            break
                except Exception:
                    exact_match = False

                if exact_match:
                    score_final = 10
                    mensaje_final = "se encontraron coincidencias"
                else:
                    score_final = 0
                    mensaje_final = "no se han encontrado coincidencias"

            # Screenshot SIEMPRE (pantalla completa)
            try:
                await page.screenshot(path=out_png_abs, full_page=True)
            except Exception:
                pass

            final_url = page.url
            await context.close()
            await navegador.close()
            navegador = None

        # -------- FASE 2: PDF (headless) --------
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = await ctx.new_page()

            await page.goto(final_url, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            try:
                await page.locator(f"{RESULT_HEADLINE_SEL}, {RESULT_TITLES_SEL}").first.wait_for(
                    state="visible", timeout=10_000
                )
            except Exception:
                pass
            try:
                await page.add_style_tag(content=PRINT_CLEAN)
                await page.emulate_media(media="print")
            except Exception:
                pass

            try:
                await page.pdf(
                    path=out_pdf_abs,
                    format="A4",
                    print_background=True,
                    margin={"top": "10mm", "right": "10mm", "bottom": "10mm", "left": "10mm"},
                    page_ranges="1-2"
                )
            except Exception:
                pass

            await ctx.close()
            await browser.close()

        # -------- Registrar en BD --------
        archivo_rel = ""
        if os.path.exists(out_png_abs) and os.path.getsize(out_png_abs) > 0:
            archivo_rel = out_png_rel
        elif os.path.exists(out_pdf_abs) and os.path.getsize(out_pdf_abs) > 500:
            archivo_rel = out_pdf_rel

        estado = "Validada" if archivo_rel else "Sin Validar"
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj,
            score=score_final, estado=estado,
            mensaje=mensaje_final, archivo=archivo_rel
        )

    except Exception:
        try:
            if navegador is not None:
                await navegador.close()
        except Exception:
            pass
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje="ocurrio un error", archivo=""
        )
