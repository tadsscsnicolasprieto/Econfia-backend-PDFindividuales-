# core/bots/colpensiones_rpm.py
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from core.models import Resultado, Fuente

# Nuevo: landing + URL objetivo
LANDING_URL = "https://sede.colpensiones.gov.co/login"
URL = "https://sede.colpensiones.gov.co/loader.php?lServicio=Se&lTipo=Process&lFuncion=start&id=2"
NOMBRE_SITIO = "colpensiones_rpm"

# Mapa del <select id="fieldFrm356">
TIPO_DOC_MAP = {
    "CC": "231",
    "CE": "232",
    "NU": "705",  # Número único de identificación
    "PA": "706",
    "TI": "707",
    "CD": "708",
    "RE": "709",
    "PT": "4029",  # PPT
}

# Rutas opcionales
POPPLER_PATH = getattr(settings, "POPPLER_PATH", os.getenv("POPPLER_PATH"))
TESSERACT_CMD = getattr(settings, "TESSERACT_CMD", os.getenv("TESSERACT_CMD"))

# -------- helpers texto / OCR / render --------
def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def _strip_accents_upper(s: str) -> str:
    s = _normalize_ws(s or "")
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return s.upper()

def _texto_pdf_pymupdf(path: str) -> str:
    try:
        import fitz
        with fitz.open(path) as doc:
            return "\n".join(pg.get_text("text") or "" for pg in doc)
    except Exception:
        return ""

def _texto_pdf_pdfminer(path: str) -> str:
    try:
        from pdfminer.high_level import extract_text
        return extract_text(path) or ""
    except Exception:
        return ""

def _render_pdf_primera_pagina_pymupdf(path_pdf: str, path_png: str, zoom: float = 2.0) -> bool:
    try:
        import fitz
        with fitz.open(path_pdf) as doc:
            if doc.page_count < 1:
                return False
            pg = doc[0]
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pix.save(path_png)
        return os.path.exists(path_png) and os.path.getsize(path_png) > 0
    except Exception:
        return False

def _render_pdf_primera_pagina_pdf2image(path_pdf: str, path_png: str, dpi: int = 300) -> bool:
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

async def _screenshot_pdf_embed(context, abs_pdf: str, abs_png: str) -> None:
    """Abre el PDF local en una pestaña y captura SOLO el <embed> (evita UI del visor)."""
    viewer = await context.new_page()
    file_url = Path(abs_pdf).resolve().as_uri()
    await viewer.goto(file_url, wait_until="load")
    sel = "embed#pdf-embed, embed[type='application/x-google-chrome-pdf'], embed[type*='pdf']"
    loc = viewer.locator(sel).first
    await loc.wait_for(state="visible", timeout=10000)
    await loc.screenshot(path=abs_png)
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

def _decidir_mensaje(texto: str) -> tuple[str, int]:
    """
    Si aparece 'NO ESTA REGISTRADO/A ... REGIMEN DE PRIMA MEDIA ... COLPENSIONES' => 'no registrado' (score 1).
    En cualquier otro caso => 'registrado' (score 1).
    """
    t = _strip_accents_upper(texto)
    neg_hit = ("NO ESTA REGISTRADO" in t or "NO ESTA REGISTRADA" in t) and \
              ("REGIMEN DE PRIMA MEDIA" in t) and ("COLPENSIONES" in t)

    if neg_hit:
        msg = ("No está registrado/a en el Régimen de Prima Media con Prestación Definida (RPM) "
               "administrado por la Administradora Colombiana de Pensiones COLPENSIONES.")
        return (msg, 1)

    msg = ("Está registrado/a en el Régimen de Prima Media con Prestación Definida (RPM) "
           "administrado por la Administradora Colombiana de Pensiones COLPENSIONES.")
    return (msg, 1)

# ----------------- BOT PRINCIPAL -----------------
async def consultar_colpensiones_rpm(consulta_id: int, cedula: str, tipo_doc: str):
    """
    Paso extra humano:
      - Ir a /login y hacer click en la tarjeta “Certificado de afiliación”.
      - Si falla, fallback directo al loader id=2.
    Luego flujo normal:
      1) Seleccionar tipo doc (#fieldFrm356), número (#fieldFrm978) y 'No' (#fieldFrm2544).
      2) Click 'Consultar' (#btnSgt).
      3) Click 'Descargar' → guardar PDF.
      4) PNG primera página y extracción de texto (PyMuPDF → pdfminer → OCR).
      5) Guardar Resultado con score=1 y mensaje según contenido.
    """
    fuente_obj = await sync_to_async(lambda: Fuente.objects.filter(nombre=NOMBRE_SITIO).first())()
    if not fuente_obj:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No existe Fuente con nombre='{NOMBRE_SITIO}'", archivo=""
        )
        return

    # Carpeta de salida
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"colpensiones_{cedula}_{ts}"
    abs_pdf = os.path.join(absolute_folder, f"{base}.pdf")
    rel_pdf = os.path.join(relative_folder, f"{base}.pdf").replace("\\", "/")
    abs_png = os.path.join(absolute_folder, f"{base}.png")
    rel_png = os.path.join(relative_folder, f"{base}.png").replace("\\", "/")

    browser = context = page = None

    try:
        tipo_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())
        if not tipo_val:
            raise ValueError(f"Tipo de documento no soportado: {tipo_doc!r}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1400, "height": 900},
                locale="es-CO",
            )
            page = await context.new_page()

            # ---- PASO HUMANO EXTRA: /login → tarjeta "Certificado de afiliación"
            await page.goto(LANDING_URL, wait_until="domcontentloaded", timeout=90000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Posible banner de cookies (no rompe si no existe)
            try:
                cookie_btn = page.locator(
                    "button:has-text('Aceptar'), button:has-text('Acepto'), "
                    "button[aria-label*='acept'], .cookie-accept"
                ).first
                await cookie_btn.click(timeout=3000)
            except Exception:
                pass

            # Intentar click por texto/tarjeta
            try:
                # a) Por título visible dentro de la tarjeta
                card = page.locator("a.grid-item:has(p.title:has-text('Certificado de afiliación'))").first
                if await card.count() == 0:
                    # b) Por imagen/href parcial
                    card = page.locator("a.grid-item[href*='loader.php'][href*='id=2']").first

                await card.scroll_into_view_if_needed(timeout=5000)
                async with page.expect_navigation(
                    url_or_predicate=lambda u: "loader.php" in u and "id=2" in u,
                    timeout=15000
                ):
                    await card.click(force=True)
            except Exception:
                # Fallback: ir directo al endpoint del trámite
                await page.goto(URL, wait_until="domcontentloaded", timeout=90000)

            # Asegurar que estamos en la página del trámite
            try:
                await page.wait_for_url(lambda u: "loader.php" in u and "id=2" in u, timeout=10000)
            except Exception:
                await page.goto(URL, wait_until="domcontentloaded", timeout=90000)

            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # ---- Flujo normal (sin cambios de fondo) ----

            # 1) Formulario
            await page.wait_for_selector("#fieldFrm356", timeout=20000)
            await page.select_option("#fieldFrm356", value=tipo_val)
            await page.fill("#fieldFrm978", str(cedula))
            await page.select_option("#fieldFrm2544", value="1807")  # "No"

            # 2) Consultar
            await page.click("#btnSgt")

            # 3) Esperar 'Descargar' y disparar la descarga
            descargar = page.locator("a.btn.btn-primary.btn-lg:has-text('Descargar')")
            try:
                await descargar.wait_for(state="visible", timeout=30000)
            except PWTimeout:
                # Fallback a una ruta interna típica si ya cambió la vista
                try:
                    base_url = page.url.split("/loader.php", 1)[0]
                    await page.goto(f"{base_url}/tramite/updInfo/2/6/1", wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass

            # 4) Descargar PDF
            try:
                async with page.expect_download(timeout=60000) as dl:
                    if await descargar.count() > 0 and await descargar.is_visible():
                        await descargar.click()
                    else:
                        await page.click("a.btn.btn-primary.btn-lg:has-text('Descargar')")
                download = await dl.value
                await download.save_as(abs_pdf)
            except Exception:
                # Si no se disparó el evento, seguimos con evidencia PNG (abajo)
                pass

            # 5) PNG (primera página o embed)
            png_ok = False
            if os.path.exists(abs_pdf) and os.path.getsize(abs_pdf) > 0:
                png_ok = _render_pdf_primera_pagina_pymupdf(abs_pdf, abs_png, zoom=2.0)
                if not png_ok:
                    png_ok = _render_pdf_primera_pagina_pdf2image(abs_pdf, abs_png, dpi=300)
                if not png_ok:
                    await _screenshot_pdf_embed(context, abs_pdf, abs_png)
                    png_ok = os.path.exists(abs_png) and os.path.getsize(abs_png) > 0
            else:
                # Sin archivo: intenta capturar el embed o toda la página
                try:
                    embed = page.locator("embed[type*='pdf'], embed#plugin, embed")
                    if await embed.count() > 0:
                        await embed.first.screenshot(path=abs_png)
                        png_ok = True
                    else:
                        await page.screenshot(path=abs_png, full_page=True)
                        png_ok = True
                except Exception:
                    await page.screenshot(path=abs_png, full_page=True)
                    png_ok = True

            # 6) Texto y decisión
            texto = ""
            if os.path.exists(abs_pdf) and os.path.getsize(abs_pdf) > 0:
                texto = _texto_pdf_pymupdf(abs_pdf) or _texto_pdf_pdfminer(abs_pdf)
            if not texto.strip() and png_ok:
                texto = _ocr_png(abs_png)

            mensaje, score = _decidir_mensaje(texto)

            # 7) Guardar resultado (apuntando al PNG)
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                estado="Validada",
                mensaje=mensaje,
                score=score,
                archivo=rel_png
            )

    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            estado="Sin Validar",
            mensaje=str(e),
            score=0,
            archivo=""
        )
    finally:
        try:
            if context:
                await context.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
