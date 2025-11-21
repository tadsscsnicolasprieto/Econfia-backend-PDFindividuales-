# bots/ecfr_part744_appendix_pdf.py
import os, re, asyncio
from datetime import datetime
from urllib.parse import urlencode
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL_PAGE = "https://www.ecfr.gov/current/title-15/subtitle-B/chapter-VII/subchapter-C/part-744/appendix-Supplement%20No.%204%20to%20Part%20744"
URL_SEARCH_FALLBACK = "https://www.ecfr.gov/search"
NOMBRE_SITIO = "ecfr_part744_appendix_pdf"

def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

PRINT_CLEAN = """
@media print {
  header, nav, footer, .usa-footer, .usa-header, .usa-banner,
  .site-feedback, .feedback, #back-to-top, .usa-skipnav,
  .usa-identifier, .usa-modal, .usa-tooltip, .usa-alert,
  .usa-navbar, .usa-nav, .eCFR-footer, .eCFR-header { display:none !important; }
  body { margin: 0 !important; padding: 0 !important; }
}
"""

async def _is_request_access(page) -> bool:
    """Detecta la pantalla de 'Request Access' del FederalRegister/eCFR."""
    try:
        # 1) Título o texto visible
        if await page.title() and re.search(r"Request Access", await page.title(), re.I):
            return True
    except Exception:
        pass
    try:
        loc = page.locator("text=/Request Access/i").first
        if await loc.count() > 0 and await loc.is_visible():
            return True
    except Exception:
        pass
    try:
        # 2) HTML con frases típicas + captcha
        html = (await page.content()) or ""
        if re.search(r"Due to aggressive automated scraping|Request Access for", html, re.I):
            return True
        if "recaptcha" in html.lower() and "Request Access" in html:
            return True
    except Exception:
        pass
    return False

async def consultar_ecfr_part744_appendix_pdf(consulta_id: int, nombre: str):
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

    nombre = (nombre or "").strip()
    if not nombre:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje="El nombre a buscar llegó vacío.", archivo=""
        )
        return

    # Rutas de salida
    carpeta_rel = os.path.join("resultados", str(consulta_id))
    carpeta_abs = os.path.join(settings.MEDIA_ROOT, carpeta_rel)
    os.makedirs(carpeta_abs, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = _safe_name(nombre)
    out_pdf_abs = os.path.join(carpeta_abs, f"{NOMBRE_SITIO}_{safe}_{ts}.pdf")
    out_pdf_rel = os.path.join(carpeta_rel, os.path.basename(out_pdf_abs))
    out_png_abs = os.path.join(carpeta_abs, f"{NOMBRE_SITIO}_{safe}_{ts}.png")
    out_png_rel = os.path.join(carpeta_rel, os.path.basename(out_png_abs))

    INPUT = "input#suggestion[name='cfr[reference]']"

    mensaje_final = ""
    score_final = 0
    final_url = None

    try:
        # ---------- Fase visible ----------
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            ctx = await navegador.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = await ctx.new_page()

            await page.goto(URL_PAGE, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass

            # Bloqueo temprano
            if await _is_request_access(page):
                try:
                    await page.screenshot(path=out_png_abs, full_page=True)
                except Exception:
                    pass
                await navegador.close(); navegador = None
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj,
                    score=0, estado="Sin Validar",
                    mensaje=("Acceso bloqueado por eCFR/FederalRegister (Request Access). "
                             "No fue posible validar; requiere resolver CAPTCHA o whitelisting de IP."),
                    archivo=out_png_rel
                )
                return

            # Buscar por nombre
            plan_b = False
            try:
                await page.wait_for_selector(INPUT, state="visible", timeout=7000)
                inp = page.locator(INPUT)
                await inp.click(force=True)
                try:
                    await inp.fill("")
                except Exception:
                    pass
                await inp.type(nombre, delay=35)
                try:
                    await page.evaluate(
                        "(sel)=>{const el=document.querySelector(sel); if(el){el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));}}",
                        INPUT
                    )
                except Exception:
                    pass
                await page.keyboard.press("Enter")
                try:
                    await page.wait_for_url("**/cfr-reference**", timeout=7000)
                except Exception:
                    pass
            except Exception:
                plan_b = True

            if plan_b:
                qs = urlencode({"search[query]": nombre})
                await page.goto(f"{URL_SEARCH_FALLBACK}?{qs}", wait_until="domcontentloaded", timeout=120000)

                # Bloqueo también en plan B
                if await _is_request_access(page):
                    try:
                        await page.screenshot(path=out_png_abs, full_page=True)
                    except Exception:
                        pass
                    await navegador.close(); navegador = None
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id, fuente=fuente_obj,
                        score=0, estado="Sin Validar",
                        mensaje=("Acceso bloqueado por eCFR/FederalRegister (Request Access) durante la búsqueda. "
                                 "No fue posible validar; requiere resolver CAPTCHA o whitelisting de IP."),
                        archivo=out_png_rel
                    )
                    return

            # Señales de resultados
            for sel in ["div.search-results", "div#results", "section.search-results", "main .search-results", "main"]:
                try:
                    await page.wait_for_selector(sel, timeout=5000)
                    break
                except Exception:
                    continue
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(0.5)

            # NO resultados vs resumen
            nores = page.locator(".content-notification.info .message").first
            if await nores.count() > 0 and await nores.is_visible():
                txt = (await nores.inner_text()).strip()
                mensaje_final = txt
                score_final = 0
            else:
                summary = page.locator(".page-summary.search-summary p").first
                if await summary.count() > 0 and await summary.is_visible():
                    ptxt = (await summary.inner_text()).strip()
                    m = re.search(r"\bof\s+(\d[\d,]*)\b", ptxt, flags=re.I)
                    if m:
                        total = int(m.group(1).replace(",", ""))
                        mensaje_final = ptxt
                        score_final = 10 if total > 0 else 0
                    else:
                        mensaje_final = ptxt
                        score_final = 10
                else:
                    mensaje_final = "Se encontraron hallazgos"
                    score_final = 10

            final_url = page.url
            await navegador.close(); navegador = None

        # ---------- Fase headless: PDF + PNG ----------
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = await ctx.new_page()

            await page.goto(final_url, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            # SI AQUÍ apareciera Request Access (algunas rutas redirigen), salimos.
            if await _is_request_access(page):
                try:
                    await page.screenshot(path=out_png_abs, full_page=True)
                except Exception:
                    pass
                await browser.close()
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj,
                    score=0, estado="Sin Validar",
                    mensaje=("Acceso bloqueado por eCFR/FederalRegister (Request Access) en la página de resultados."),
                    archivo=out_png_rel
                )
                return

            try:
                await page.add_style_tag(content=PRINT_CLEAN)
                await page.emulate_media(media="print")
            except Exception:
                pass

            await page.pdf(
                path=out_pdf_abs,
                format="A4",
                print_background=True,
                margin={"top": "10mm", "right": "10mm", "bottom": "10mm", "left": "10mm"},
                page_ranges="1-2"
            )

            # PNG desde PDF (preferido) o screenshot del HTML
            png_ok = False
            try:
                from pdf2image import convert_from_path
                imgs = convert_from_path(out_pdf_abs, dpi=300, first_page=1, last_page=1)
                if imgs:
                    imgs[0].save(out_png_abs, "PNG")
                    png_ok = True
            except Exception:
                png_ok = False
            if not png_ok:
                await page.screenshot(path=out_png_abs, full_page=True)

            await browser.close()

        # Registrar (flujo normal)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=out_png_rel
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj, score=0,
                estado="Sin Validar", mensaje=str(e), archivo=""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
