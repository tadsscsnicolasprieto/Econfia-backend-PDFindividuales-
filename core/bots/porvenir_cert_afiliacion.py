# core/bots/porvenir_cert_afiliacion.py
import os
import re
from datetime import datetime
from pathlib import Path

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from core.models import Resultado, Fuente

# Nuevo: URL de aterrizaje + URL objetivo (la que ya usabas)
LANDING_URL = "https://www.porvenir.com.co/certificados-y-extractos"
URL = "https://www.porvenir.com.co/web/certificados-y-extractos/certificado-de-afiliacion"
NOMBRE_SITIO = "porvenir_cert_afiliacion"

# Mapa del tipo de documento (valores del <select>)
TIPO_DOC_MAP = {"CC": "CC", "CE": "CE", "TI": "TI"}

POPPLER_PATH = getattr(settings, "POPPLER_PATH", os.getenv("POPPLER_PATH"))

# -------- Helpers PDF / texto / screenshots --------
def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

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
    """Abre file:// del PDF y captura solo el <embed> (evita UI del visor)."""
    viewer = await context.new_page()
    file_url = Path(abs_pdf).resolve().as_uri()
    await viewer.goto(file_url, wait_until="load")
    loc = viewer.locator("embed#pdf-embed, embed[type*='pdf']").first
    await loc.wait_for(state="visible", timeout=10000)
    await loc.screenshot(path=abs_png)
    await viewer.close()

def _extraer_frase_afiliado(texto: str) -> str:
    """
    Intenta tomar la oración que incluye 'se encuentra afiliado(a) al Fondo de Pensiones Obligatorias Porvenir.'
    """
    t = _normalize_ws(texto)
    m = re.search(r"(identificado\(a\).*?se\s+encuentra\s+afiliad[oa].*?Porvenir\.)", t, re.I)
    if m:
        return _normalize_ws(m.group(1))
    return "Se encuentra afiliado(a) al Fondo de Pensiones Obligatorias Porvenir."

# ---------------- Bot principal ----------------
async def consultar_porvenir_cert_afiliacion(consulta_id: int, cedula: str, tipo_doc: str):
    fuente_obj = await sync_to_async(lambda: Fuente.objects.filter(nombre=NOMBRE_SITIO).first())()
    if not fuente_obj:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No existe Fuente con nombre='{NOMBRE_SITIO}'", archivo=""
        )
        return

    # Rutas de salida
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"porvenir_{cedula}_{ts}"
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
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                accept_downloads=True, viewport={"width": 1400, "height": 900}, locale="es-CO"
            )
            page = await context.new_page()

            # ---- PASO HUMANO EXTRA: entrar al landing y clicar "Descárgalo aquí"
            await page.goto(LANDING_URL, wait_until="domcontentloaded", timeout=90000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Posible banner de cookies (no rompe si no existe)
            try:
                # intenta botones comunes
                cookie_btn = page.locator(
                    "button:has-text('Aceptar'), button:has-text('Acepto'), "
                    "button[aria-label*='acept'], .cookie-accept"
                ).first
                await cookie_btn.click(timeout=3000)
            except Exception:
                pass

            # Click robusto al link "Descárgalo aquí"
            try:
                # 1) por texto visible
                link = page.locator("a:has-text('Descárgalo aquí')").first
                if await link.count() == 0:
                    # 2) por clase + href parcial
                    link = page.locator(
                        "a.prv-btn.prv-btn--green.prv-btn--round-corner[href*='certificado-de-afiliacion']"
                    ).first

                await link.scroll_into_view_if_needed(timeout=5000)
                async with page.expect_navigation(url_or_predicate=lambda u: "certificado-de-afiliacion" in u, timeout=15000):
                    await link.click(force=True)
            except Exception:
                # Fallback: ir directo
                await page.goto(URL, wait_until="domcontentloaded", timeout=90000)

            # Asegurar que estamos en la página objetivo
            try:
                await page.wait_for_url(lambda u: "certificado-de-afiliacion" in u, timeout=10000)
            except Exception:
                # Si no llegó por cualquier razón, forzar la URL destino
                await page.goto(URL, wait_until="domcontentloaded", timeout=90000)

            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # ---- A partir de aquí, tu flujo normal tal cual ----

            # 2) Formulario (ids dinámicos → usamos ends-with)
            await page.wait_for_selector('select[id$="_documento"]', timeout=20000)
            await page.select_option('select[id$="_documento"]', value=tipo_val)
            await page.fill('input[id$="_numeroIdentificacion"]', str(cedula))

            # 3) Click y esperar posible descarga
            download = None
            try:
                async with page.expect_download(timeout=15000) as dl:
                    await page.click("#submitDescargarCertificado")
                download = await dl.value
            except Exception:
                # Si no descargó, puede ser no afiliado o demora; seguimos validando abajo
                pass

            # 4) Caso NO afiliado (mensaje p.p-status)
            try:
                status = page.locator("p.p-status").first
                await status.wait_for(state="visible", timeout=4000)
                # Tomar screenshot completo como evidencia
                await page.screenshot(path=abs_png, full_page=True)
                msg = _normalize_ws(await status.inner_text())
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    estado="Validada",
                    mensaje=msg,
                    score=1,
                    archivo=rel_png
                )
                return
            except Exception:
                pass

            # 5) Si hay descarga → guardar PDF y generar PNG “solo documento”
            if download:
                await download.save_as(abs_pdf)

                png_ok = _render_pdf_primera_pagina_pymupdf(abs_pdf, abs_png, zoom=2.0)
                if not png_ok:
                    png_ok = _render_pdf_primera_pagina_pdf2image(abs_pdf, abs_png, dpi=300)
                if not png_ok:
                    await _screenshot_pdf_embed(context, abs_pdf, abs_png)

                # Extraer texto y armar mensaje
                texto = _texto_pdf_pymupdf(abs_pdf) or _texto_pdf_pdfminer(abs_pdf)
                mensaje = _extraer_frase_afiliado(texto)

                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    estado="Validada",
                    mensaje=mensaje,
                    score=1,
                    archivo=rel_png
                )
                return

            # 6) Fallback absoluto (ni mensaje ni descarga): screenshot y mensaje genérico
            await page.screenshot(path=abs_png, full_page=True)
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                estado="Sin Validar",
                mensaje="No fue posible determinar el estado (sin mensaje y sin descarga). Revise la evidencia.",
                score=1,
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
