# core/bots/ofac_programs_site_search_pdf.py
import os
import re
import unicodedata
from datetime import datetime

from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
import fitz  # PyMuPDF

from core.models import Resultado, Fuente

URL = "https://ofac.treasury.gov/sanctions-programs-and-country-information"
NOMBRE_SITIO = "ofac_programs_sactions"  # Asegúrate que coincide con Fuente.nombre en BD


# -------- utilidades --------
def _safe(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

def _norm(s: str) -> str:
    s = (s or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s).strip()
    return s.upper()

def _pdf_first_page_png(pdf_path: str, png_path: str, zoom: float = 2.0) -> bool:
    try:
        with fitz.open(pdf_path) as doc:
            if doc.page_count == 0:
                return False
            page = doc[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pix.save(png_path)
        return os.path.exists(png_path) and os.path.getsize(png_path) > 0
    except Exception:
        return False

def _pdf_text(pdf_path: str) -> str:
    try:
        with fitz.open(pdf_path) as doc:
            return "\n".join(page.get_text("text") or "" for page in doc)
    except Exception:
        return ""

def _make_fallback_pdf(pdf_path: str, text: str) -> None:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
        c = canvas.Canvas(pdf_path, pagesize=A4)
        w, h = A4
        c.setFont("Helvetica", 12)
        c.drawString(40, h - 60, text[:120])
        c.save()
    except Exception:
        pass


# -------- BOT --------
async def consultar_ofac_programs_site_search_pdf(consulta_id: int, nombre: str, cedula=None, **_):
    """
    - Busca 'nombre' en OFAC Programs.
    - Imprime 1a página a PDF y genera PNG.
    - Guarda PNG en Resultado.archivo.
    - Si hay 403/CloudFront, deja mensaje y estado 'Sin validar'.
    """
    # Fuente
    fuente_obj = await sync_to_async(lambda: Fuente.objects.filter(nombre=NOMBRE_SITIO).first())()
    if not fuente_obj:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin validar",
            archivo="",
            mensaje=f"No existe Fuente con nombre='{NOMBRE_SITIO}'"
        )
        return

    # Rutas
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = _safe(nombre)

    pdf_name = f"{NOMBRE_SITIO}_{safe}_{ts}.pdf"
    png_name = f"{NOMBRE_SITIO}_{safe}_{ts}.png"

    abs_pdf = os.path.join(absolute_folder, pdf_name)
    rel_pdf = os.path.join(relative_folder, pdf_name).replace("\\", "/")
    abs_png = os.path.join(absolute_folder, png_name)
    rel_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    mensaje = "No se encontraron hallazgos"
    estado = "Validado"

    page = None
    try:
        # --- Navegar y buscar (puede devolver 403) ---
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                locale="en-US",
                viewport={"width": 1440, "height": 1000},
            )
            page = await context.new_page()

            # 1) Ir a la página principal y chequear status
            resp = await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            status = resp.status if resp else 0
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass

            # Si ya aquí es 403, igual seguiremos para imprimir el error en PDF
            # 2) Abrir buscador
            try:
                toggle = page.locator("[aria-controls='megamenu-search']").first
                await toggle.click(timeout=8000)
            except Exception:
                pass

            # 3) Buscar
            await page.locator("#megamenu-search #query").wait_for(state="visible", timeout=10000)
            await page.fill("#megamenu-search #query", nombre or "")
            try:
                await page.click("#megamenu-search input[type='submit'][name='commit']", timeout=5000)
            except Exception:
                await page.press("#megamenu-search #query", "Enter")

            # 4) Esperar resultados (o error)
            try:
                await page.wait_for_url("**/search**", timeout=15000)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=7000)
            except Exception:
                pass

            # 5) Imprimir PDF (primera página)
            try:
                await page.emulate_media(media="print")
            except Exception:
                pass
            await page.pdf(
                path=abs_pdf,
                format="A4",
                print_background=True,
                margin={"top": "10mm", "right": "10mm", "bottom": "10mm", "left": "10mm"},
                page_ranges="1",
            )
            await browser.close()

        # Si PDF quedó vacío, escribimos uno mínimo
        if not os.path.exists(abs_pdf) or os.path.getsize(abs_pdf) < 500:
            _make_fallback_pdf(abs_pdf, f"OFAC – sin datos visibles para: {nombre}")

        # PNG evidencia
        _pdf_first_page_png(abs_pdf, abs_png, zoom=2.0)

        # Detección 403 por contenido o status
        pdf_txt = _norm(_pdf_text(abs_pdf))
        if status == 403 or "403 ERROR" in pdf_txt or "REQUEST BLOCKED" in pdf_txt or "CLOUDFRONT" in pdf_txt:
            mensaje = "El sitio OFAC devolvió 403 (CloudFront): acceso bloqueado o rate limited."
            estado = "Sin validar"

        # Guardar resultado apuntando al PNG
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado=estado,
            archivo=rel_png,
            mensaje=mensaje
        )
        return {
            "estado": estado,
            "archivo_png": rel_png,
            "archivo_pdf": rel_pdf,
            "mensaje": mensaje,
            "score": 0
        }

    except Exception as e:
        # Evidencia mínima en error
        try:
            _make_fallback_pdf(abs_pdf, f"OFAC – error: {e}")
            _pdf_first_page_png(abs_pdf, abs_png, zoom=2.0)
        except Exception:
            pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            archivo=rel_png if os.path.exists(abs_png) else "",
            mensaje=str(e) or "Ocurrió un problema al obtener la información de la fuente"
        )
        return {"estado": "Sin validar", "archivo_png": rel_png if os.path.exists(abs_png) else "", "archivo_pdf": rel_pdf, "mensaje": str(e), "score": 0}
