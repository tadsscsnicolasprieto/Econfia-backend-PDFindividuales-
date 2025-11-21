# bots/ecfr_search_pdf.py
import os
import re
import asyncio
from datetime import datetime
from urllib.parse import urlencode
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL_BASE     = "https://www.ecfr.gov"
URL_SEARCH   = f"{URL_BASE}/search"
NOMBRE_SITIO = "ecfr_search_pdf"

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

# ------- Detector de "Request Access" (gate de FederalRegister/eCFR) -------
REQ_ACCESS_TEXT = re.compile(r"\bRequest\s+Access\b", re.I)
GATE_HINTS = re.compile(r"Due to aggressive automated scraping|Request Access for|recaptcha", re.I)

async def _is_request_access(page) -> bool:
    try:
        title = await page.title()
        if title and REQ_ACCESS_TEXT.search(title):
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
        html = (await page.content()) or ""
        if REQ_ACCESS_TEXT.search(html) or GATE_HINTS.search(html):
            return True
    except Exception:
        pass
    return False


async def consultar_ecfr_search_pdf(consulta_id: int, nombre: str):
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

    # Rutas
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = _safe_name(nombre)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_pdf_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.pdf")
    out_pdf_rel = os.path.join(relative_folder, os.path.basename(out_pdf_abs)).replace("\\", "/")
    out_png_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.png")
    out_png_rel = os.path.join(relative_folder, os.path.basename(out_png_abs)).replace("\\", "/")

    INPUT = "input#search_query[name='search[query]']"
    FORM  = "form[action='/search']"

    mensaje_final = ""
    score_final   = 0
    estado_final  = "Validada"
    final_url     = URL_SEARCH

    try:
        # ===== FASE 1: navegar y leer estado de resultados =====
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            ctx = await navegador.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="en-US",
            )
            page = await ctx.new_page()
            await page.goto(URL_SEARCH, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Gate temprano
            if await _is_request_access(page):
                try: await page.screenshot(path=out_png_abs, full_page=True)
                except Exception: pass
                await navegador.close(); navegador = None
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj,
                    score=0, estado="Sin Validar",
                    mensaje=("Acceso bloqueado por eCFR/FederalRegister (Request Access). "
                             "No fue posible validar; requiere resolver CAPTCHA o whitelisting de IP."),
                    archivo=out_png_rel
                )
                return

            # Enviar búsqueda
            plan_b = False
            try:
                await page.wait_for_selector(INPUT, state="visible", timeout=6000)
                inp = page.locator(INPUT)
                await inp.click(force=True)
                await inp.fill(nombre)
                # submit "formal"
                await page.evaluate(
                    "(formSel,inSel)=>{const f=document.querySelector(formSel)||(document.querySelector(inSel)?.form); if(f){ if(f.requestSubmit) f.requestSubmit(); else f.submit(); }}",
                    FORM, INPUT
                )
                try:
                    await page.wait_for_url("**/search/results**", timeout=9000)
                except Exception:
                    pass
            except Exception:
                plan_b = True

            if plan_b:
                qs = urlencode({"search[query]": nombre})
                await page.goto(f"{URL_SEARCH}?{qs}", wait_until="domcontentloaded", timeout=120000)

            # Gate tras enviar
            if await _is_request_access(page):
                try: await page.screenshot(path=out_png_abs, full_page=True)
                except Exception: pass
                await navegador.close(); navegador = None
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj,
                    score=0, estado="Sin Validar",
                    mensaje=("Acceso bloqueado por eCFR/FederalRegister (Request Access) durante la búsqueda."),
                    archivo=out_png_rel
                )
                return

            # Esperar contenedor de resultados
            for sel in [
                "div.search-results","div#results","section.search-results",
                "ol.search-results","main .search-results","main .results","main"
            ]:
                try:
                    await page.wait_for_selector(sel, timeout=7000)
                    break
                except Exception:
                    continue

            try: await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception: pass
            await asyncio.sleep(0.6)

            # No results
            nores = page.locator(".content-notification.info .message").first
            if await nores.count() > 0 and await nores.is_visible():
                txt = (await nores.inner_text()).strip()
                mensaje_final = txt
                score_final   = 0
                estado_final  = "Validada"
            else:
                # Summary con total
                summary = page.locator(".page-summary.search-summary p").first
                if await summary.count() > 0 and await summary.is_visible():
                    ptxt = (await summary.inner_text()).strip()
                    m = re.search(r"\bof\s+(\d[\d,]*)\b", ptxt, flags=re.I)
                    if m:
                        total = int(m.group(1).replace(",", ""))
                        mensaje_final = ptxt
                        score_final   = 10 if total > 0 else 0
                    else:
                        mensaje_final = ptxt
                        score_final   = 10
                    estado_final = "Validada"
                else:
                    mensaje_final = "Se encontraron hallazgos"
                    score_final   = 10
                    estado_final  = "Validada"

            final_url = page.url

            # Evidencia temprana por si algo falla después
            try:
                await page.screenshot(path=out_png_abs, full_page=True)
            except Exception:
                pass

            await navegador.close()
            navegador = None

        # ===== FASE 2: PDF + PNG (PNG es lo que se guarda en BD) =====
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = await ctx.new_page()

            await page.goto(final_url, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

            # Si aquí aparece el gate, dejamos evidencia y salimos Sin Validar
            if await _is_request_access(page):
                try: await page.screenshot(path=out_png_abs, full_page=True)
                except Exception: pass
                await browser.close()
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj,
                    score=0, estado="Sin Validar",
                    mensaje=("Acceso bloqueado por eCFR/FederalRegister (Request Access) en la página de resultados."),
                    archivo=out_png_rel if os.path.exists(out_png_abs) else ""
                )
                return

            # PDF (respaldo; el archivo que se guarda en BD será el PNG)
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
                    margin={"top":"10mm","right":"10mm","bottom":"10mm","left":"10mm"},
                    page_ranges="1-2"
                )
            except Exception:
                # ignorar fallo de PDF
                pass

            # PNG final (si falla pdf2image, screenshot del HTML)
            png_ok = False
            try:
                if os.path.exists(out_pdf_abs) and os.path.getsize(out_pdf_abs) > 500:
                    from pdf2image import convert_from_path
                    imgs = convert_from_path(out_pdf_abs, dpi=200, first_page=1, last_page=1)
                    if imgs:
                        imgs[0].save(out_png_abs, "PNG")
                        png_ok = True
            except Exception:
                png_ok = False

            if not png_ok:
                try:
                    await page.screenshot(path=out_png_abs, full_page=True)
                except Exception:
                    pass

            await browser.close()

        # Guardar (flujo normal)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj,
            score=score_final, estado=estado_final,
            mensaje=mensaje_final, archivo=out_png_rel if os.path.exists(out_png_abs) else ""
        )

    except Exception as e:
        # Error general: Sin Validar + si hay PNG ya tomado, adjúntalo
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj, score=0,
                estado="Sin Validar", mensaje=str(e),
                archivo=out_png_rel if os.path.exists(out_png_abs) else ""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
