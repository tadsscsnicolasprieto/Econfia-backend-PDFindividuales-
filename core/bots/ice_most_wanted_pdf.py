# bots/ice_most_wanted_pdf.py
import os, re
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright
from core.models import Resultado, Fuente

URL = "https://www.ice.gov/most-wanted"
NOMBRE_SITIO = "ice_most_wanted_pdf"

def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

def _fallback_blank_pdf(out_pdf_abs: str, text: str):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        os.makedirs(os.path.dirname(out_pdf_abs), exist_ok=True)
        c = canvas.Canvas(out_pdf_abs, pagesize=A4)
        w, h = A4
        c.setFont("Helvetica", 12)
        c.drawCentredString(w / 2, h / 2, text)
        c.save()
        return True
    except Exception:
        return False

async def _screenshot_to_pdf(page, out_pdf_abs):
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    tmp_png = out_pdf_abs.replace(".pdf", ".png")
    os.makedirs(os.path.dirname(out_pdf_abs), exist_ok=True)

    await page.screenshot(path=tmp_png, full_page=True)

    img = ImageReader(tmp_png)
    iw, ih = img.getSize()
    pdf = canvas.Canvas(out_pdf_abs, pagesize=(iw, ih))
    pdf.drawImage(img, 0, 0, width=iw, height=ih)
    pdf.save()
    try:
        os.remove(tmp_png)
    except Exception:
        pass

async def consultar_ice_most_wanted_pdf(consulta_id: int, nombre: str):
    navegador, ctx = None, None
    out_pdf_abs, out_pdf_rel = "", ""
    score_final, mensaje_final = 0, ""

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
            estado="Sin Validar", mensaje="El nombre llegó vacío.", archivo=""
        )
        return

    # Rutas
    rel_folder = os.path.join("resultados", str(consulta_id))
    abs_folder = os.path.join(settings.MEDIA_ROOT, rel_folder)
    os.makedirs(abs_folder, exist_ok=True)

    safe = _safe_name(nombre)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_pdf_abs = os.path.join(abs_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.pdf")
    out_pdf_rel = os.path.join(rel_folder, os.path.basename(out_pdf_abs))

    INPUT = "#extended-search-field-small, input.usagov-search-autocomplete[placeholder='Search']"
    RESULTS_ITEM = ".result-title-label, .result-desc"
    NO_RESULTS = ".spelling-suggestion-wrapper"

    try:
        # ---- FASE 1: búsqueda ----
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = await navegador.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = await ctx.new_page()

            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            await page.wait_for_selector(INPUT, timeout=15000)
            inp = page.locator(INPUT).first
            await inp.fill(nombre)
            await inp.press("Enter")
            await page.wait_for_timeout(4000)

            final_url = page.url
            await ctx.close()
            await navegador.close()
            navegador, ctx = None, None

        # ---- FASE 2: validar resultados + PDF con screenshot ----
        async with async_playwright() as p:
            browser2 = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            ctx2 = await browser2.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page2 = await ctx2.new_page()

            await page2.goto(final_url, wait_until="domcontentloaded", timeout=120000)
            await page2.wait_for_timeout(4000)

            # Validar si hay resultados exactos
            found = False
            items = await page2.locator(RESULTS_ITEM).all_text_contents()
            for txt in items:
                if nombre.lower() in txt.lower():
                    score_final = 10
                    mensaje_final = f'Se encontraron resultados para "{nombre}"'
                    found = True
                    break

            if not found:
                # Revisar si aparece el aviso de "No results found"
                if await page2.locator(NO_RESULTS).count() > 0:
                    mensaje_final = f'Sorry, no results found for "{nombre}". Try entering fewer or more general search terms.'
                else:
                    mensaje_final = f'Sorry, no results found for "{nombre}".'
                score_final = 0

            # Screenshot -> PDF
            await _screenshot_to_pdf(page2, out_pdf_abs)

            await ctx2.close()
            await browser2.close()

        if not os.path.exists(out_pdf_abs) or os.path.getsize(out_pdf_abs) < 500:
            _fallback_blank_pdf(out_pdf_abs, f"ICE – sin datos visibles para: {nombre}")

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=score_final,
            estado="Validada", mensaje=mensaje_final, archivo=out_pdf_rel
        )

    except Exception as e:
        if out_pdf_abs and (not os.path.exists(out_pdf_abs) or os.path.getsize(out_pdf_abs) < 500):
            _fallback_blank_pdf(out_pdf_abs, f"ICE – error: {e}")
        if ctx: await ctx.close()
        if navegador: await navegador.close()
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje=str(e), archivo=""
        )