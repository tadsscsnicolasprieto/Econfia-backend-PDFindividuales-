# core/bots/contraloria_certificado.py
import os
import re
import asyncio
from datetime import datetime
from pathlib import Path

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright
import aiohttp

from core.models import Resultado, Fuente

PAGE_URL = "https://cfiscal.contraloria.gov.co/certificados/certificadopersonanatural.aspx"
SITE_KEY = "6LcfnjwUAAAAAIyl8ehhox7ZYqLQSVl_w1dmYIle"
CAPSOLVER_API_KEY = "CAP-99C7B12571DFBDC693C4EFACEE4D9F64BD7678F38556667407E34ABBEEA59830"
NOMBRE_SITIO = "contraloria"

# Regex tolerantes (espacios/saltos)
RX_NO = re.compile(r"NO\s+SE\s+ENCUENTRA\s+REPORTADO\s+COMO\s+RESPONSABLE\s+FISCAL", re.I)
RX_SI = re.compile(r"\bSE\s+ENCUENTRA\s+REPORTADO\s+COMO\s+RESPONSABLE\s+FISCAL\b", re.I)

POS_NO = "NO SE ENCUENTRA REPORTADO COMO RESPONSABLE FISCAL"
POS_SI = "SE ENCUENTRA REPORTADO COMO RESPONSABLE FISCAL"

# Rutas opcionales
POPPLER_PATH = getattr(settings, "POPPLER_PATH", os.getenv("POPPLER_PATH"))  # ej: r"C:\poppler\bin"
TESSERACT_CMD = getattr(settings, "TESSERACT_CMD", os.getenv("TESSERACT_CMD"))  # ej: r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# ---------------- CAPTCHA ----------------
async def _resolver_captcha_cappy():
    create_url = "https://api.capsolver.com/createTask"
    result_url = "https://api.capsolver.com/getTaskResult"
    async with aiohttp.ClientSession() as session:
        payload = {
            "clientKey": CAPSOLVER_API_KEY,
            "task": {
                "type": "ReCaptchaV2TaskProxyLess",
                "websiteURL": PAGE_URL,
                "websiteKey": SITE_KEY
            }
        }
        async with session.post(create_url, json=payload) as resp:
            task_response = await resp.json()
        if "taskId" not in task_response:
            raise RuntimeError(f"No se pudo crear la tarea CAPTCHA: {task_response}")
        task_id = task_response["taskId"]

        for _ in range(40):
            await asyncio.sleep(2)
            async with session.post(result_url, json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}) as resp:
                result = await resp.json()
            if result.get("status") == "ready":
                return result["solution"]["gRecaptchaResponse"]
            if result.get("status") == "failed":
                raise RuntimeError("Falló el CAPTCHA: " + result.get("errorDescription", "Sin detalle"))
        raise RuntimeError("Timeout esperando respuesta del CAPTCHA")

# --------------- UTILIDADES TEXTO/IMAGEN ---------------
def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def _texto_pdf_pypdf(path: str) -> str:
    try:
        from pypdf import PdfReader
        r = PdfReader(path)
        parts = []
        for pg in r.pages:
            try:
                parts.append(pg.extract_text() or "")
            except Exception:
                pass
        return "\n".join(parts)
    except Exception:
        return ""

def _texto_pdf_pdfminer(path: str) -> str:
    try:
        from pdfminer.high_level import extract_text  # paquete: pdfminer.six
        return extract_text(path) or ""
    except Exception:
        return ""

def _texto_pdf_pymupdf(path: str) -> str:
    """Texto con PyMuPDF (suele ser más fiel que pypdf en algunos PDFs)."""
    try:
        import fitz  # PyMuPDF
        with fitz.open(path) as doc:
            return "\n".join((pg.get_text("text") or "") for pg in doc)
    except Exception:
        return ""

def _render_pdf_primera_pagina_pymupdf(path_pdf: str, path_png: str, zoom: float = 2.0) -> bool:
    """Render limpio al estilo de tu segunda imagen (preferido)."""
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
    """Render con pdf2image (requiere Poppler)."""
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
    """Fallback: screenshot del <embed> del visor PDF (evita miniaturas/toolbar)."""
    viewer = await context.new_page()
    file_url = Path(abs_pdf).resolve().as_uri()
    await viewer.goto(file_url, wait_until="load")
    embed = viewer.locator(
        "embed#pdf-embed, embed[type='application/x-google-chrome-pdf'], embed[type*='pdf']"
    ).first
    await embed.wait_for(state="visible", timeout=10000)
    await embed.screenshot(path=abs_png)
    await viewer.close()

def _ocr_png(path_png: str) -> str:
    try:
        from PIL import Image
        import pytesseract
        if TESSERACT_CMD:
            pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
        return pytesseract.image_to_string(Image.open(path_png), lang="spa") or ""
    except Exception:
        return ""

def _decidir(texto: str) -> tuple[str, int]:
    """Prioriza la negativa; sólo afirma 'SE ENCUENTRA' si no aparece la negativa."""
    t = _normalize(texto).upper()
    if RX_NO.search(t):
        return (POS_NO, 0)
    if RX_SI.search(t) and not RX_NO.search(t):
        return (POS_SI, 10)
    return ("PDF descargado; no se detectó frase de estado en el contenido. Revise el archivo.", 0)

# ----------------- BOT PRINCIPAL -----------------
async def consultar_contraloria(consulta_id: int, cedula: str, tipo_doc: str, **kwargs):
    """
    - Descarga PDF del certificado (con captcha).
    - Evidencia PNG: PyMuPDF (preferido) -> pdf2image -> <embed>.
    - Texto: pypdf -> pdfminer -> PyMuPDF -> OCR (fallback).
    - Guarda Resultado con el PNG.
    """
    fuente_obj = await sync_to_async(lambda: Fuente.objects.filter(nombre=NOMBRE_SITIO).first())()
    if not fuente_obj:
        raise RuntimeError(f"No existe Fuente con nombre='{NOMBRE_SITIO}'")

    # Rutas
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_name = f"contraloria_{cedula}_{ts}.pdf"
    png_name = f"contraloria_{cedula}_{ts}.png"
    txt_name = f"contraloria_{cedula}_{ts}.txt"
    txt_ocr_name = f"contraloria_{cedula}_{ts}_ocr.txt"

    abs_pdf = os.path.join(absolute_folder, pdf_name)
    rel_pdf = os.path.join(relative_folder, pdf_name).replace("\\", "/")
    abs_png = os.path.join(absolute_folder, png_name)
    rel_png = os.path.join(relative_folder, png_name).replace("\\", "/")
    abs_txt = os.path.join(absolute_folder, txt_name)
    abs_txt_ocr = os.path.join(absolute_folder, txt_ocr_name)

    browser = None
    try:
        token = await _resolver_captcha_cappy()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                accept_downloads=True, locale="es-CO", viewport={"width": 1366, "height": 900}
            )
            page = await context.new_page()

            # 1) Ir y llenar
            await page.goto(PAGE_URL, wait_until="domcontentloaded")
            await page.select_option("#ddlTipoDocumento", str(tipo_doc))
            await page.fill("#txtNumeroDocumento", str(cedula))

            # 2) Inyectar token en el form (ASP.NET)
            await page.evaluate(
                """(token) => {
                    const form = document.querySelector('#aspnetForm') || document.querySelector('form');
                    let el = form && form.querySelector('#g-recaptcha-response');
                    if (!el) {
                        el = document.createElement('textarea');
                        el.id = 'g-recaptcha-response';
                        el.name = 'g-recaptcha-response';
                        el.style.display = 'none';
                        (form || document.body).appendChild(el);
                    }
                    el.value = token;
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                }""",
                token
            )

            # 3) Click y esperar descarga
            async with page.expect_download(timeout=90000) as dl:
                await page.click("#btnBuscar")
            download = await dl.value
            await download.save_as(abs_pdf)

            # 4) Evidencia PNG:
            #    (a) PyMuPDF (idéntico a tu 'estado_cedula')
            rendered = _render_pdf_primera_pagina_pymupdf(abs_pdf, abs_png, zoom=2.0)
            if not rendered:
                # (b) pdf2image (si hay Poppler)
                rendered = _render_pdf_primera_pagina_pdf2image(abs_pdf, abs_png, dpi=300)
            if not rendered:
                # (c) Fallback: screenshot del <embed>
                await _screenshot_pdf_element(context, abs_pdf, abs_png)

            await browser.close()
            browser = None

        # 5) Texto del PDF: pypdf -> pdfminer -> PyMuPDF
        text = _texto_pdf_pypdf(abs_pdf)
        if not text.strip():
            text = _texto_pdf_pdfminer(abs_pdf)
        if not text.strip():
            text = _texto_pdf_pymupdf(abs_pdf)

        # Guardar texto (auditoría)
        try:
            with open(abs_txt, "w", encoding="utf-8") as f:
                f.write(text or "")
        except Exception:
            pass

        msg, score = _decidir(text)

        # 6) OCR si aún no detectamos frase
        if "no se detectó frase" in msg.lower():
            ocr_text = _ocr_png(abs_png)
            try:
                with open(abs_txt_ocr, "w", encoding="utf-8") as f:
                    f.write(ocr_text or "")
            except Exception:
                pass
            msg, score = _decidir(ocr_text)

        # 7) Guardar Resultado
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            estado="Validada",
            mensaje=msg,
            score=score,
            archivo=rel_png
        )

        return {
            "estado": "Validada",
            "archivo_png": rel_png,
            "archivo_pdf": rel_pdf,
            "mensaje": msg,
            "score": score
        }

    except Exception as e:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            estado="Sin Validar",
            mensaje=str(e),
            score=0,
            archivo=""
        )
        return {"estado": "Sin Validar", "archivo_png": "", "archivo_pdf": "", "mensaje": str(e), "score": 0}
