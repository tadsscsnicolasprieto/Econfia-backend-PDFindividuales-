import os
import re
import asyncio
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

GEN_URL = "https://www.procuraduria.gov.co/Pages/Generacion-de-antecedentes.aspx"
NOMBRE_SITIO = "procuraduria_certificado"

TIPO_DOC_MAP = {
    'CC': '1', 'PEP': '0', 'NIT': '2', 'CE': '5', 'PPT': '10'
}

PREGUNTAS_RESPUESTAS = {
    '¿ Cuanto es 9 - 2 ?': '7',
    '¿ Cuanto es 3 X 3 ?': '9',
    '¿ Cuanto es 6 + 2 ?': '8',
    '¿ Cuanto es 2 X 3 ?': '6',
    '¿ Cuanto es 3 - 2 ?': '1',
    '¿ Cuanto es 4 + 3 ?': '7'
}

# --- helpers de screenshot / render ---

async def _fullpage_screenshot(page, path):
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    await page.screenshot(path=path, full_page=True)

# Poppler opcional para pdf2image (Windows)
POPPLER_PATH = getattr(settings, "POPPLER_PATH", os.getenv("POPPLER_PATH"))

def _render_pdf_primera_pagina_pymupdf(path_pdf: str, path_png: str, zoom: float = 2.0) -> bool:
    """Render nítido SOLO del documento con PyMuPDF (preferido)."""
    try:
        import fitz  # PyMuPDF
        with fitz.open(path_pdf) as doc:
            if doc.page_count == 0:
                return False
            pg = doc[0]
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pix.save(path_png)
        return os.path.exists(path_png) and os.path.getsize(path_png) > 0
    except Exception:
        return False

def _render_pdf_primera_pagina_pdf2image(path_pdf: str, path_png: str, dpi: int = 300) -> bool:
    """Render SOLO del documento con pdf2image (requiere Poppler)."""
    try:
        from pdf2image import convert_from_path
        kwargs = {"dpi": dpi, "first_page": 1, "last_page": 1}
        if POPPLER_PATH:
            kwargs["poppler_path"] = POPPLER_PATH
        imgs = convert_from_path(path_pdf, **kwargs)
        if imgs:
            imgs[0].save(path_png, "PNG")
            return True
        return False
    except Exception:
        return False

async def _screenshot_pdf_element(context, abs_pdf: str, abs_png: str) -> None:
    """
    Fallback final: abrir file://<pdf> y capturar el <embed> del visor Chrome
    (evita miniaturas/toolbar del visor).
    """
    viewer = await context.new_page()
    file_url = Path(abs_pdf).resolve().as_uri()
    await viewer.goto(file_url, wait_until="load")
    # el <embed> puede variar según versión de Chromium
    embed = viewer.locator(
        "embed#pdf-embed, embed[type='application/x-google-chrome-pdf'], embed[type*='pdf']"
    ).first
    await embed.wait_for(state="visible", timeout=10000)
    await embed.screenshot(path=abs_png)
    await viewer.close()


# ============ BOT PRINCIPAL ============
async def generar_certificado_procuraduria(consulta_id: int, cedula: str, tipo_doc: str):
    """
    Genera el certificado y deja evidencia SOLO del documento:
      1) Descarga PDF.
      2) PNG con PyMuPDF (preferido) -> pdf2image -> screenshot del <embed>.
    Mantiene tus mensajes/score actuales.
    """
    browser = context = page = None
    evidencia_rel = ""
    try:
        # --- rutas de salida ---
        relative_folder = os.path.join('resultados', str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"procuraduria_cert_{cedula}_{ts}"
        abs_png = os.path.join(absolute_folder, f"{base}.png")
        rel_png = os.path.join(relative_folder, f"{base}.png").replace("\\", "/")
        abs_pdf = os.path.join(absolute_folder, f"{base}.pdf")
        err_png_abs = os.path.join(absolute_folder, f"{base}_error.png")
        err_png_rel = os.path.join(relative_folder, f"{base}_error.png").replace("\\", "/")

        # --- validaciones ---
        tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())
        if not tipo_doc_val:
            raise ValueError(f"Tipo de documento no válido: {tipo_doc}")

        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                accept_downloads=True, viewport={"width": 1600, "height": 1000}, locale="es-CO"
            )
            page = await context.new_page()

            # 1) Cargar página
            try:
                await page.goto(GEN_URL, wait_until="domcontentloaded", timeout=90000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
            except PWTimeout:
                await _fullpage_screenshot(page, err_png_abs)
                evidencia_rel = err_png_rel
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj, score=1,
                    estado="Sin validar", mensaje="La página de generación no cargó o está caída.",
                    archivo=evidencia_rel
                )
                return

            await page.wait_for_timeout(600)

            # 2) Localizar iframe del certificado
            frame = None
            for f in page.frames:
                if "/webcert/" in (f.url or "") or "Certificado" in (f.url or ""):
                    frame = f
                    break
            if not frame and page.frames and len(page.frames) > 1:
                frame = page.frames[-1]
            if not frame:
                await _fullpage_screenshot(page, err_png_abs)
                evidencia_rel = err_png_rel
                raise Exception("No se encontró el iframe de generación del certificado.")

            # 3) Formulario
            await frame.wait_for_selector('#ddlTipoID', timeout=20000)
            await frame.select_option('#ddlTipoID', value=tipo_doc_val)
            await frame.fill('#txtNumID', str(cedula))

            # 4) Resolver pregunta
            solved = False
            ultima_pregunta = ""
            for _ in range(12):
                try:
                    ultima_pregunta = (await frame.locator('#lblPregunta, [id*=lblPregunta]').inner_text()).strip()
                except Exception:
                    ultima_pregunta = ""
                resp = PREGUNTAS_RESPUESTAS.get(ultima_pregunta)
                if resp:
                    try:
                        await frame.fill('#txtRespuestaPregunta', resp)
                    except Exception:
                        await frame.locator("input[id*=txtRespuesta]").fill(resp)
                    solved = True
                    break
                try:
                    await frame.click('#ImageButton1')  # refrescar
                except Exception:
                    pass
                await asyncio.sleep(1)

            if not solved:
                await _fullpage_screenshot(page, err_png_abs)
                evidencia_rel = err_png_rel
                raise Exception(f"No se pudo resolver la pregunta. Última: '{ultima_pregunta}'")

            # 5) Generar
            prev_len = await frame.evaluate("() => document.documentElement.outerHTML.length")
            await frame.locator('#btnExportar').evaluate("b => b.click()")
            try:
                await frame.wait_for_function(
                    "prev => document.documentElement.outerHTML.length !== prev",
                    arg=prev_len, timeout=30000
                )
            except Exception:
                pass

            # 6) Descargar PDF (preferido)
            try:
                async with page.expect_download(timeout=60000) as dl_info:
                    await frame.locator('#btnDescargar').click()
                download = await dl_info.value
                await download.save_as(abs_pdf)

                # Evidencia: PyMuPDF -> pdf2image -> <embed>
                if _render_pdf_primera_pagina_pymupdf(abs_pdf, abs_png, zoom=2.0) or \
                   _render_pdf_primera_pagina_pdf2image(abs_pdf, abs_png, dpi=300):
                    evidencia_rel = rel_png
                else:
                    await _screenshot_pdf_element(context, abs_pdf, abs_png)
                    evidencia_rel = rel_png

                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=1,
                    estado="Validada",
                    mensaje="Certificado generado (ver evidencia).",
                    archivo=evidencia_rel
                )
                return

            except Exception as e:
                # 7) Fallback: si NO hubo descarga (visor en el mismo tab)
                # Abrimos file:// y capturamos el <embed> para tener SOLO el documento
                await _screenshot_pdf_element(context, abs_pdf, abs_png)  # por si algo quedó escrito
                evidencia_rel = rel_png if os.path.exists(abs_png) else err_png_rel
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj, score=1,
                    estado="Sin validar", mensaje=f"No se logró descargar el PDF: {str(e)}",
                    archivo=evidencia_rel
                )
                return

    except Exception as e:
        if evidencia_rel == "" and page is not None:
            try:
                await _fullpage_screenshot(page, err_png_abs)
                evidencia_rel = err_png_rel
            except Exception:
                evidencia_rel = ""
        try:
            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
        except Exception:
            fuente_obj = None
        if fuente_obj:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=1,
                estado="Sin validar",
                mensaje=f"Error en generación: {str(e)}",
                archivo=evidencia_rel
            )
    finally:
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
