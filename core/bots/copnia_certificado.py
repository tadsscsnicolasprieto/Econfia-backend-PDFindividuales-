# core/bots/copnia_certificado.py
import os
import asyncio
import base64
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://tramites.copnia.gov.co/Copnia_Microsite/CertificateOfGoodStanding/CertificateOfGoodStandingStart"
NOMBRE_SITIO = "copnia_certificado"

# Mapa de tipos de documento -> value del <select id="DocumentType">
TIPO_DOC_MAP = {
    "CC": 1,  # CÉDULA DE CIUDADANÍA
    "CE": 2,  # CÉDULA DE EXTRANJERÍA
    "PEP": 3, # PERMISO ESPECIAL DE PERMANENCIA
    "PAS": 4, # PASAPORTE
    "TI": 5,  # TARJETA IDENTIDAD
    "NIT": 6, # NIT
    "PPT": 7, # PERMISO POR PROTECCIÓN TEMPORAL
}

# Timings más relajados
NAV_TIMEOUT = 120_000
TINY   = 500
SHORT  = 1500
MEDIUM = 3000
LONG   = 5000
XLONG  = 9000

async def _guardar_resultado(consulta_id, fuente_obj, estado, mensaje, rel_path, score=0):
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=score,
        estado=estado,
        mensaje=mensaje,
        archivo=rel_path,
    )

async def _wait_idle(page, t=LONG):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=t)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=t)
    except Exception:
        pass

async def _screenshot_pdf_only(target_page, out_path) -> bool:
    """Recorta SOLO el visor PDF si la página lo muestra."""
    # iframes (elige el más grande visible)
    try:
        frames = target_page.locator("iframe")
        n = await frames.count()
        best, area = None, 0
        for i in range(n):
            bb = await frames.nth(i).bounding_box()
            if bb:
                a = bb["width"] * bb["height"]
                if a > area:
                    area, best = a, frames.nth(i)
        if best and area > 0:
            await best.scroll_into_view_if_needed()
            await asyncio.sleep(TINY/1000)
            await best.screenshot(path=out_path)
            return True
    except Exception:
        pass

    # embed/object
    for sel in ["embed[type='application/pdf']", "object[type='application/pdf']"]:
        try:
            el = target_page.locator(sel).first
            if await el.is_visible(timeout=2000):
                await el.scroll_into_view_if_needed()
                await asyncio.sleep(TINY/1000)
                await el.screenshot(path=out_path)
                return True
        except Exception:
            pass

    # pdf.js / contenedores típicos
    for sel in ["#viewerContainer", "#viewer", ".pdfViewer", "#pageContainer1", "div#page-container"]:
        try:
            el = target_page.locator(sel).first
            if await el.is_visible(timeout=2000):
                await el.scroll_into_view_if_needed()
                await asyncio.sleep(TINY/1000)
                await el.screenshot(path=out_path)
                return True
        except Exception:
            pass

    return False

def _render_with_pypdfium2(abs_pdf: str, abs_png: str, scale: float = 2.0) -> bool:
    """Renderiza con pypdfium2 (recomendado)."""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(abs_pdf)
        page0 = pdf.get_page(0)
        bitmap = page0.render(scale=scale)  # scale 2.0 ≈ ~192 DPI
        pil = bitmap.to_pil()
        pil.save(abs_png)
        page0.close()
        pdf.close()
        return True
    except Exception:
        return False

def _render_with_pymupdf(abs_pdf: str, abs_png: str, dpi: int = 220) -> bool:
    """Renderiza con PyMuPDF (fitz)."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(abs_pdf)
        page = doc[0]
        mat = fitz.Matrix(dpi/72, dpi/72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        pix.save(abs_png)
        doc.close()
        return True
    except Exception:
        return False

def _render_with_pdf2image(abs_pdf: str, abs_png: str, dpi: int = 300) -> bool:
    """Renderiza con pdf2image (requiere Poppler)."""
    try:
        from pdf2image import convert_from_path
        imgs = convert_from_path(abs_pdf, dpi=dpi, first_page=1, last_page=1)
        if imgs:
            imgs[0].save(abs_png, "PNG")
            return True
    except Exception:
        pass
    return False

async def _render_pdf_to_png(abs_pdf: str, abs_png: str, context) -> bool:
    """
    Orden de preferencia:
      1) pypdfium2
      2) PyMuPDF (fitz)
      3) pdf2image
      4) Fallback viewer (data:URI) + screenshot del <embed>
    """
    if _render_with_pypdfium2(abs_pdf, abs_png):
        return True
    if _render_with_pymupdf(abs_pdf, abs_png):
        return True
    if _render_with_pdf2image(abs_pdf, abs_png):
        return True

    # 4) Fallback: abrir como data:URI y recortar <embed> (suele ser más fiable en headless que file://)
    try:
        with open(abs_pdf, "rb") as f:
            b64pdf = base64.b64encode(f.read()).decode("utf-8")

        viewer = await context.new_page()
        await viewer.set_viewport_size({"width": 1366, "height": 1800})
        await viewer.set_content(f"""
<!doctype html>
<html><head><meta charset="utf-8">
<style>
  html,body {{ margin:0; height:100%; background:#fff; }}
  #pdf {{ width:100vw; height:100vh; display:block; border:0; }}
</style>
</head><body>
  <embed id="pdf" type="application/pdf" src="data:application/pdf;base64,{b64pdf}"/>
</body></html>
""", wait_until="domcontentloaded")

        pdf_el = viewer.locator("#pdf")
        await pdf_el.wait_for(state="visible", timeout=25_000)

        # Pausas + scroll para forzar render
        await viewer.wait_for_timeout(2500)
        for _ in range(6):
            try:
                await viewer.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await viewer.wait_for_timeout(250)
                await viewer.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass
            await viewer.wait_for_timeout(900)

        await pdf_el.screenshot(path=abs_png)
        await viewer.close()
        return True
    except Exception:
        return False

async def consultar_copnia_certificado(consulta_id: int, cedula: str, tipo_doc: str):
    """
    COPNIA – Generar certificado:
      1) Selecciona "Generar certificado" / "Número de identificación" / tipo doc / número
      2) Busca y, si hay listado, entra con 'a.linkConsultar'
      3) Click en 'Generar Certificado de Vigencia'
      4) Captura la DESCARGA del PDF y la guarda
      5) Renderiza PNG de la 1ª página (biblioteca -> viewer fallback)
    """
    navegador = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await _guardar_resultado(consulta_id, None, "Sin Validar",
                                 f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", "", score=0)
        return

    # Rutas
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_num = (cedula or "").strip().replace(" ", "_") or "consulta"

    pdf_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.pdf"
    png_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"

    abs_pdf  = os.path.join(absolute_folder, pdf_name)
    rel_pdf  = os.path.join(relative_folder, pdf_name).replace("\\", "/")
    abs_png  = os.path.join(absolute_folder, png_name)
    rel_png  = os.path.join(relative_folder, png_name).replace("\\", "/")

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1440,1000",
                ]
            )
            context = await navegador.new_context(
                accept_downloads=True,
                viewport={"width": 1440, "height": 1000},
                locale="es-CO",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            # 1) Inicio
            await page.goto(URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            await asyncio.sleep(SHORT/1000)
            await _wait_idle(page, t=LONG)

            # Selecciones iniciales
            await page.select_option("#ActionCode", value="1"); await asyncio.sleep(TINY/1000)
            await page.select_option("#SearchWithCode", value="1"); await asyncio.sleep(TINY/1000)

            tipo_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())
            if not tipo_val:
                raise ValueError(f"Tipo de documento no soportado: {tipo_doc}")
            await page.select_option("#DocumentType", value=str(tipo_val)); await asyncio.sleep(TINY/1000)
            await page.fill("#DocumentNumber", str(cedula or "").strip()); await asyncio.sleep(TINY/1000)

            # Consultar
            await page.click("#btnConsult")
            await asyncio.sleep(SHORT/1000)
            await _wait_idle(page, t=XLONG)

            # 2) ¿Hay link 'Consultar'?
            link_consulta = page.locator("a.linkConsultar").first
            no_data_msg   = page.locator("text=No se encontraron matrículas")
            has_link = False
            try:
                await link_consulta.wait_for(state="visible", timeout=10_000)
                has_link = True
            except Exception:
                has_link = False

            if not has_link:
                try:
                    if await no_data_msg.is_visible(timeout=2000):
                        await page.evaluate("window.scrollTo(0,0)")
                        await page.screenshot(path=abs_png, full_page=True)
                        await context.close(); await navegador.close(); navegador = None
                        await _guardar_resultado(consulta_id, fuente_obj, "Validada",
                                                 "No se encontraron matrículas", rel_png, score=0)
                        return
                except Exception:
                    pass
                await page.evaluate("window.scrollTo(0,0)")
                await page.screenshot(path=abs_png, full_page=True)
                await context.close(); await navegador.close(); navegador = None
                await _guardar_resultado(consulta_id, fuente_obj, "Validada",
                                         "Resultado de consulta (sin detalle claro)", rel_png, score=0)
                return

            # 3) Entrar a detalle
            await link_consulta.scroll_into_view_if_needed(); await asyncio.sleep(TINY/1000)
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=25_000):
                await link_consulta.click()
            await _wait_idle(page, t=LONG)
            await page.wait_for_timeout(SHORT)

            # 4) Generar certificado → capturar descarga
            btn_gen = page.locator("#btnGenerateCertificate").first
            await btn_gen.scroll_into_view_if_needed(); await asyncio.sleep(TINY/1000)

            download = None
            try:
                async with page.expect_download(timeout=120_000) as dl_info:
                    await btn_gen.click()
                download = await dl_info.value
            except Exception:
                try:
                    async with context.expect_event("download", timeout=120_000) as dl_info:
                        await btn_gen.click()
                    download = await dl_info.value
                except Exception:
                    # No hubo descarga: quizá visor; recortarlo y salir
                    recortado = await _screenshot_pdf_only(page, abs_png)
                    if not recortado:
                        await page.evaluate("window.scrollTo(0,0)")
                        await page.screenshot(path=abs_png, full_page=True)
                    await context.close(); await navegador.close(); navegador = None
                    await _guardar_resultado(consulta_id, fuente_obj, "Validada",
                                             "Certificado generado (visor)", rel_png, score=1)
                    return

            # Guardar el PDF descargado (y darle un respiro al FS)
            await download.save_as(abs_pdf)
            await asyncio.sleep(0.8)

            # 5) Renderizar PNG (evita depender del visor del navegador)
            ok = await _render_pdf_to_png(abs_pdf, abs_png, context)
            if not ok:
                # último fallback: abrir file:// y tratar de recortar visor
                viewer = await context.new_page()
                await viewer.goto(f"file://{abs_pdf}", wait_until="load")
                await viewer.wait_for_timeout(MEDIUM)
                rec = await _screenshot_pdf_only(viewer, abs_png)
                if not rec:
                    await viewer.screenshot(path=abs_png, full_page=True)
                await viewer.close()

            # Cerrar navegador
            await context.close(); await navegador.close(); navegador = None

        # 6) Registrar OK
        await _guardar_resultado(
            consulta_id, fuente_obj, "Validada",
            "Certificado generado exitosamente", rel_png, score=1
        )

    except Exception as e:
        # evidencia si algo quedó abierto
        try:
            if navegador:
                ctxs = navegador.contexts
                if ctxs and ctxs[0].pages:
                    try:
                        await ctxs[0].pages[-1].screenshot(path=abs_png, full_page=True)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            if navegador:
                await navegador.close()
        except Exception:
            pass
        await _guardar_resultado(
            consulta_id, fuente_obj, "Sin Validar",
            f"{type(e).__name__}: {e}", "", score=0
        )
