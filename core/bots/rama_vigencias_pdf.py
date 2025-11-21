# bots/rama_vigencias_pdf.py
import os
import re
import unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright
import fitz  # PyMuPDF

from core.models import Resultado, Fuente  # ajusta si tu app cambia

URL = "https://vigenciaspublicas.ramajudicial.gov.co/Certificados.aspx"
NOMBRE_SITIO = "rama_vigencias_pdf"

# Mapeo flexible para tipo de documento
TIPO_DOC_MAP = {
    "1": {"1", "CC", "CEDULA", "CÉDULA", "CEDULA DE CIUDADANIA", "CÉDULA DE CIUDADANÍA"},
    "2": {"2", "CE", "CEDULA DE EXTRANJERIA", "CÉDULA DE EXTRANJERÍA"},
    "4": {"4", "PPT", "PERMISO POR PROTECCION TEMPORAL", "PERMISO POR PROTECCIÓN TEMPORAL"},
}

def _resolver_valor_tipo_doc(tipo_doc: str) -> str:
    t = (tipo_doc or "").strip().upper()
    for value, aliases in TIPO_DOC_MAP.items():
        if t in aliases:
            return value
    return "1"  # default CC

# ---------- Utilidades texto / normalización ----------
def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def _strip_accents_upper(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _normalize_spaces(s).upper()

# ---------- PyMuPDF: texto y render ----------
def _texto_pdf_pymupdf(path: str) -> str:
    try:
        parts = []
        with fitz.open(path) as doc:
            for page in doc:
                # "text" plano suele ser suficiente; prueba "blocks" si necesitas más robustez
                parts.append(page.get_text("text") or "")
        return "\n".join(parts)
    except Exception:
        return ""

def _render_pagina1_pymupdf(pdf_path: str, png_path: str, zoom: float = 2.0) -> bool:
    """
    Renderiza la primera página a PNG. zoom=2.0 ~ 144 DPI*2 -> imagen nítida.
    """
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

# Regex robustos (texto sin tildes)
_RX_NO = re.compile(r"\bNO\s+ESTA\s+INSCRITA\b")
_RX_SI = re.compile(r"\bESTA\s+INSCRITA\b")

def _decidir_estado(texto: str) -> tuple[str, int]:
    """
    Prioriza la negativa (NO ESTA INSCRITA) sobre la positiva.
    Devuelve (mensaje, score). Ajusta el score a tu escala si quieres.
    """
    T = _strip_accents_upper(texto)
    if _RX_NO.search(T):
        return ("NO ESTÁ INSCRITA", 0)
    if _RX_SI.search(T) and not _RX_NO.search(T):
        return ("ESTÁ INSCRITA", 10)
    return ("PDF descargado; no se detectó 'ESTÁ INSCRITA' / 'NO ESTÁ INSCRITA'. Revise el archivo.", 0)

# -------------- BOT PRINCIPAL --------------
async def consultar_rama_vigencias_pdf(consulta_id: int, tipo_doc: str, numero: str):
    """
    Rama Judicial – Certificado de Vigencia
    - Descarga PDF
    - Genera PNG de evidencia (PyMuPDF)
    - Extrae texto (PyMuPDF) y fija mensaje según INSCRITA/NO INSCRITA
    - Guarda Resultado apuntando al PNG
    """
    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin validar",
            archivo="",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}"
        )
        return

    # Carpetas y nombres
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    pdf_name = f"{NOMBRE_SITIO}_{consulta_id}_{ts}.pdf"
    png_name = f"{NOMBRE_SITIO}_{consulta_id}_{ts}.png"

    abs_pdf = os.path.join(absolute_folder, pdf_name)
    rel_pdf = os.path.join(relative_folder, pdf_name).replace("\\", "/")
    abs_png = os.path.join(absolute_folder, png_name)
    rel_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 1000})
            page = await ctx.new_page()

            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # seleccionar ABOGADO
            await page.select_option("#ddlCalidad", value="1")

            # tipo doc
            value_tipo = _resolver_valor_tipo_doc(tipo_doc)
            await page.select_option("#ddlTipoDocumento", value=value_tipo)

            # número doc
            await page.fill("#txtDocumento", (numero or "").strip())

            # preparar descarga
            async with page.expect_download(timeout=90000) as dl_info:
                await page.click("#btnTpVigente")
            download = await dl_info.value

            # guardar
            try:
                await download.save_as(abs_pdf)
            except Exception:
                tmp = await download.path()
                if tmp:
                    os.replace(tmp, abs_pdf)

            await browser.close()
            browser = None

        # ---- Evidencia PNG (PyMuPDF) ----
        _render_pagina1_pymupdf(abs_pdf, abs_png, zoom=2.0)

        # ---- Texto del PDF (PyMuPDF) ----
        text = _texto_pdf_pymupdf(abs_pdf)

        # Decidir por contenido
        mensaje, score = _decidir_estado(text)

        # Guardar resultado apuntando al PNG (evidencia)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score,
            estado="Validado",
            archivo=rel_png,
            mensaje=mensaje
        )

        # Si quieres, puedes retornar también la ruta del PDF
        return {
            "estado": "Validado",
            "archivo_png": rel_png,
            "archivo_pdf": rel_pdf,
            "mensaje": mensaje,
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
            score=0,
            estado="Sin validar",
            archivo="",
            mensaje=str(e)
        )
        return {"estado": "Sin Validar", "archivo_png": "", "archivo_pdf": "", "mensaje": str(e), "score": 0}
