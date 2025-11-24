# bots/mha_individual_terrorists_pdf.py
import os
import re
import unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright
import fitz  # PyMuPDF

from core.models import Resultado, Fuente

URL = "https://www.mha.gov.in/en/page/individual-terrorists-under-uapa"
NOMBRE_SITIO = "mha_individual_terrorists"


# --------- utilidades ----------
def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

def _norm(s: str) -> str:
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s)
    return s.upper()

def _render_pdf_first_page_to_png(pdf_path: str, png_path: str, zoom: float = 2.0) -> bool:
    """Renderiza la primera página del PDF a PNG usando PyMuPDF."""
    try:
        with fitz.open(pdf_path) as doc:
            if doc.page_count == 0:
                return False
            page = doc[0]
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            pix.save(png_path)
        return os.path.exists(png_path) and os.path.getsize(png_path) > 0
    except Exception:
        return False

def _fallback_blank_pdf(out_pdf_abs: str, text: str) -> bool:
    """Si no se pudo generar el PDF, crea uno simple con un mensaje."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        os.makedirs(os.path.dirname(out_pdf_abs), exist_ok=True)
        c = canvas.Canvas(out_pdf_abs, pagesize=A4)
        w, h = A4
        c.setFont("Helvetica", 12)
        c.drawString(40, h - 60, text[:120])
        c.save()
        return True
    except Exception:
        return False


# --------- BOT PRINCIPAL ----------
async def consultar_mha_individual_terrorists_pdf(consulta_id: int, nombre: str, cedula):
    """
    - Busca `nombre` con la lupa del sitio MHA.
    - Si el listado contiene el nombre → mensaje = 'Se encontraron resultados', si no → 'No hay coincidencias'.
    - Imprime resultados a PDF (1ª página) y toma PNG de ese PDF (evidencia).
    - Guarda Resultado apuntando al PNG.
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
    safe_ced = _safe_name(str(cedula))

    pdf_name = f"{NOMBRE_SITIO}_{safe_ced}_{ts}.pdf"
    png_name = f"{NOMBRE_SITIO}_{safe_ced}_{ts}.png"

    abs_pdf = os.path.join(absolute_folder, pdf_name)
    rel_pdf = os.path.join(relative_folder, pdf_name).replace("\\", "/")
    abs_png = os.path.join(absolute_folder, png_name)
    rel_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    query_norm = _norm(nombre)
    final_url = URL
    mensaje = "No hay coincidencias"  # default
    try:
        # --- Etapa visible: buscar y evaluar resultados ---
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="en-IN"
            )
            page = await context.new_page()

            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # abrir caja de búsqueda si existe
            try:
                await page.locator("#toggleSearch").click(timeout=5000)
            except Exception:
                pass

            # escribir y buscar
            await page.wait_for_selector("#search_key_g", timeout=10000)
            await page.fill("#search_key_g", nombre or "")
            await page.wait_for_timeout(400)
            await page.keyboard.press("Enter")

            # esperar landing de resultados
            try:
                await page.wait_for_url("**/search**", timeout=20000)
            except Exception:
                pass

            # esperar contenedor de resultados
            for sel in (
                "div.view-content",
                "ul.view-what-s-new",
                "main .search-results",
                "main .view-content",
                "div.views-row",
                "ul.search-results",
                "main",
            ):
                try:
                    await page.wait_for_selector(sel, timeout=6000)
                    break
                except Exception:
                    continue

            await page.wait_for_timeout(1200)

            # Extraer títulos (anchors dentro de resultados)
            try:
                titles = await page.locator("div.views-field-title-1 a").all_inner_texts()
            except Exception:
                titles = []

            titles_norm = [_norm(t) for t in titles]
            if query_norm and any(query_norm in t for t in titles_norm):
                mensaje = "Se encontraron resultados"
            else:
                # si no hubo match exacto, igualmente si hay items podemos considerar que hubo resultados,
                # pero el requerimiento pide "no hay coincidencias" si no aparece el nombre.
                if not titles_norm:
                    mensaje = "No hay coincidencias"
                else:
                    mensaje = "No hay coincidencias"

            final_url = page.url
            await browser.close()

        # --- Etapa headless: imprimir a PDF (1 página) ---
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="en-IN"
            )
            page = await context.new_page()
            await page.goto(final_url, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
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

        # Fallback PDF en blanco si quedó vacío
        if not os.path.exists(abs_pdf) or os.path.getsize(abs_pdf) < 500:
            _fallback_blank_pdf(abs_pdf, f"MHA – sin datos visibles para: {nombre}")

        # --- PNG evidencia desde el PDF ---
        ok_png = _render_pdf_first_page_to_png(abs_pdf, abs_png, zoom=2.0)
        if not ok_png:
            # Si por alguna razón PyMuPDF falla, deja el archivo en blanco como evidencia mínima
            open(abs_png, "wb").close()

        # Guardar Resultado apuntando al PNG
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Validada",
            archivo=rel_png,
            mensaje=mensaje,
        )

        # (Opcional) devolver rutas
        return {
            "estado": "Validada",
            "archivo_png": rel_png,
            "archivo_pdf": rel_pdf,
            "mensaje": mensaje,
            "score": 0,
        }

    except Exception as e:
        # Intentar dejar constancia
        try:
            _fallback_blank_pdf(abs_pdf, f"MHA – error: {e}")
            _render_pdf_first_page_to_png(abs_pdf, abs_png, zoom=2.0)
        except Exception:
            pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            archivo="",
            mensaje=str(e) or "Ocurrió un problema en la validación",
        )
        return {"estado": "Sin validar", "archivo_png": "", "archivo_pdf": "", "mensaje": str(e), "score": 0}
