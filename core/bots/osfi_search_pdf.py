# bots/osfi_search_pdf_async.py
import os
import re
import asyncio
from datetime import datetime
from urllib.parse import urlencode
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente
import PyPDF2  # <--- para leer el PDF

URL = "https://www.osfi-bsif.gc.ca/en"
NOMBRE_SITIO = "osfi_search"

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
        c.drawCentredString(w/2, h/2, text)
        c.save()
    except Exception:
        pass

PRINT_CLEAN = """
@media print{
  header, nav, footer, .gcweb-menu, .gc-subway, .page-actions,
  .breadcrumb, .wb-srch-qry, .wb-share, .wb-lbx, .wb-tabs,
  .gc-followus, .gc-most-requested, .gc-features, .gc-prtts,
  .gc-pg-hlpfl, .gc-promo, .gc-sub-footer { display:none !important; }
  body { margin:0 !important; padding:0 !important; }
}
html, body { overflow: visible !important; }
"""

async def _run_consulta(nombre: str, cedula: str, out_pdf_abs: str) -> None:
    """Ejecución 1 intento: navegar, buscar y exportar PDF"""
    async with async_playwright() as p:
        # --- Fase 1: búsqueda ---
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-CA")
        page = await ctx.new_page()

        await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass

        input_sel  = "#wb-srch-q"
        submit_sel = "#wb-srch-sub"

        await page.wait_for_selector(input_sel, state="visible", timeout=15000)
        campo = page.locator(input_sel)
        await campo.scroll_into_view_if_needed()
        await campo.fill(nombre)

        submitted = False
        try:
            await page.locator(submit_sel).click(timeout=2000)
            submitted = True
        except Exception:
            pass
        if not submitted:
            try:
                await campo.press("Enter")
                submitted = True
            except Exception:
                pass

        if not submitted:
            qs = urlencode({"search-keys": nombre})
            await page.goto(f"https://www.osfi-bsif.gc.ca/en/search?{qs}",
                            wait_until="domcontentloaded", timeout=120000)

        try:
            await page.wait_for_url("**/search**", timeout=20000)
        except Exception:
            pass

        final_url = page.url
        await browser.close()

        # --- Fase 2: PDF ---
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-CA")
        page = await ctx.new_page()
        await page.goto(final_url, wait_until="domcontentloaded", timeout=120000)

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
            page_ranges="1"
        )
        await browser.close()


def _leer_pdf(out_pdf_abs: str) -> str:
    """Extraer texto de PDF"""
    try:
        with open(out_pdf_abs, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            texto = ""
            for page in reader.pages:
                texto += page.extract_text() or ""
            return texto
    except Exception:
        return ""


async def consultar_osfi_search_pdf(consulta_id: int, nombre: str, cedula: str):
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = _safe_name(nombre)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_pdf_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{cedula}_{ts}.pdf")
    out_pdf_rel = os.path.join(relative_folder, os.path.basename(out_pdf_abs))

    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)

    intentos = 0
    exito = False
    error_final = None

    while intentos < 3 and not exito:
        try:
            intentos += 1
            await _run_consulta(nombre, cedula, out_pdf_abs)

            if not os.path.exists(out_pdf_abs) or os.path.getsize(out_pdf_abs) < 500:
                raise Exception("El PDF quedó vacío o muy pequeño.")

            texto = _leer_pdf(out_pdf_abs)
            if "NO REGISTRA SANCIONES NI INHABILIDADES VIGENTES" in texto.upper():
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Validado",
                    mensaje="NO REGISTRA SANCIONES NI INHABILIDADES VIGENTES",
                    archivo=out_pdf_rel
                )
            else:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=10,
                    estado="Validado",
                    mensaje="Se encontraron hallazgos en la consulta",
                    archivo=out_pdf_rel
                )
            exito = True

        except Exception as e:
            error_final = str(e)
            if intentos >= 3:  # al tercer intento falla definitivo
                _fallback_blank_pdf(out_pdf_abs, f"OSFI – error: {e}")
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin validar",
                    mensaje="Ocurrió un problema al obtener la información de la fuente",
                    archivo=""
                )
            else:
                await asyncio.sleep(2)  # espera corta entre reintentos
