# core/bots/cpae_certificado.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://tramites.cpae.gov.co/public?show=generateCertification"
NOMBRE_SITIO = "cpae_certificado"

# Mapea tus tipos cortos a la etiqueta del select (Angular Material)
TIPO_DOC_MAP = {
    "CC": "CÉDULA DE CIUDADANÍA",
    "CE": "CÉDULA DE EXTRANJERÍA",
}

# --- Timings ---
TINY   = 400
SHORT  = 1200
MEDIUM = 2500
LONG   = 4000
XLONG  = 9000
NAV_TIMEOUT = 120_000


def _safe(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"


async def _wait_idle(page, t=LONG):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=t)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=t)
    except Exception:
        pass


# ---------- Render PDF → PNG (evitar depender del visor del navegador) ----------
def _render_with_pypdfium2(abs_pdf: str, abs_png: str, scale: float = 2.0) -> bool:
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(abs_pdf)
        page0 = pdf.get_page(0)
        bmp = page0.render(scale=scale)  # ~ 192 dpi
        pil = bmp.to_pil()
        pil.save(abs_png)
        page0.close()
        pdf.close()
        return True
    except Exception:
        return False


def _render_with_pymupdf(abs_pdf: str, abs_png: str, dpi: int = 220) -> bool:
    try:
        import fitz
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
    if _render_with_pypdfium2(abs_pdf, abs_png):
        return True
    if _render_with_pymupdf(abs_pdf, abs_png):
        return True
    if _render_with_pdf2image(abs_pdf, abs_png):
        return True

    # Último recurso: abrir file:// y capturar
    try:
        viewer = await context.new_page()
        await viewer.goto(f"file://{abs_pdf}", wait_until="load")
        await viewer.wait_for_timeout(MEDIUM)
        await viewer.screenshot(path=abs_png, full_page=True)
        await viewer.close()
        return True
    except Exception:
        return False


async def consultar_cpae_certificado(consulta_id: int, tipo_doc: str, cedula: str):
    """
    CPAE – Generar certificado de vigencia y antecedentes.
    Flujos:
      A) Sin matrículas asociadas -> aparece modal "No existen matrículas asociadas al graduado" -> screenshot FULL PAGE y guardar.
      B) Con matrículas -> seleccionar la opción numérica -> Generar certificado -> descargar PDF -> renderizar 1ª página a PNG -> guardar.
    """
    browser = None
    context = None
    page = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=1,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo="",
        )
        return

    try:
        # Carpeta resultados/<consulta_id>
        rel_folder = os.path.join("resultados", str(consulta_id))
        abs_folder = os.path.join(settings.MEDIA_ROOT, rel_folder)
        os.makedirs(abs_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_num = _safe(cedula)

        png_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"
        abs_png  = os.path.join(abs_folder, png_name)
        rel_png  = os.path.join(rel_folder, png_name).replace("\\", "/")

        pdf_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.pdf"
        abs_pdf  = os.path.join(abs_folder, pdf_name)
        rel_pdf  = os.path.join(rel_folder, pdf_name).replace("\\", "/")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1440,1000",
                ],
            )
            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1440, "height": 1000},
                locale="es-CO",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            # 1) Abrir
            await page.goto(URL, wait_until="domcontentloaded", timeout= NAV_TIMEOUT)
            await page.wait_for_timeout(SHORT)
            await _wait_idle(page, t=LONG)

            # 2) Tipo de documento (mat-select)
            mat_doc_select = page.locator("mat-select[formcontrolname='documentType']").first
            await mat_doc_select.wait_for(state="visible", timeout=20_000)
            await mat_doc_select.scroll_into_view_if_needed()
            await page.wait_for_timeout(200)
            await mat_doc_select.click()
            etiqueta = TIPO_DOC_MAP.get((tipo_doc or "").upper(), "CÉDULA DE CIUDADANÍA")
            await page.locator("mat-option .mat-option-text", has_text=etiqueta).first.click()
            await page.wait_for_timeout(TINY)

            # 3) Número de documento
            input_doc = page.locator("input[formcontrolname='documentNumber']").first
            await input_doc.wait_for(state="visible", timeout=15_000)
            await input_doc.scroll_into_view_if_needed()
            await input_doc.fill("")
            await input_doc.type(str(cedula or "").strip(), delay=20)
            await page.wait_for_timeout(SHORT)

            # 4) Abrir desplegable de MATRÍCULAS (varios posibles triggers)
            #    - Si no aparece el conocido, probamos alternativas.
            reg_trigger = page.locator("#mat-select-value-3").first
            try:
                await reg_trigger.wait_for(state="visible", timeout=10_000)
                await reg_trigger.scroll_into_view_if_needed()
                await page.wait_for_timeout(200)
                await reg_trigger.click()
            except Exception:
                clicked = False
                for sel in [
                    "mat-select[formcontrolname='registerNumber']",
                    "mat-select[placeholder*='Matr']",
                    "mat-select"
                ]:
                    try:
                        cand = page.locator(sel).nth(1) if sel == "mat-select" else page.locator(sel).first
                        await cand.wait_for(state="visible", timeout=3000)
                        await cand.scroll_into_view_if_needed()
                        await page.wait_for_timeout(200)
                        await cand.click()
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    # No se pudo abrir el desplegable
                    await page.screenshot(path=abs_png, full_page=True)
                    await browser.close(); browser = None
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=1,
                        estado="Validada",
                        mensaje="No se pudo abrir el desplegable de matrículas.",
                        archivo=rel_png,
                    )
                    return

            await page.wait_for_timeout(SHORT)

            # Detectar modal "No existen matrículas ..."
            modal_no = page.locator("p.modal-text-govco:has-text('No existen matrículas asociadas al graduado')")
            opciones = page.locator("mat-option .mat-option-text")

            hubo_modal = False
            try:
                await modal_no.wait_for(state="visible", timeout=4000)
                hubo_modal = True
            except Exception:
                hubo_modal = False

            if hubo_modal:
                # Full page screenshot del estado con modal
                try:
                    await page.evaluate("window.scrollTo(0, 0)")
                except Exception:
                    pass
                await page.wait_for_timeout(600)
                await page.screenshot(path=abs_png, full_page=True)

                await browser.close(); browser = None
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=1,
                    estado="Validada",
                    mensaje="No existen matrículas asociadas al graduado.",
                    archivo=rel_png,
                )
                return

            # 5) Si no hubo modal, elegir la primera opción numérica
            count = 0
            try:
                count = await opciones.count()
            except Exception:
                count = 0

            if count == 0:
                # Nada para elegir → evidencia y salir
                await page.screenshot(path=abs_png, full_page=True)
                await browser.close(); browser = None
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=1,
                    estado="Validada",
                    mensaje="No se listaron matrículas en el desplegable.",
                    archivo=rel_png,
                )
                return

            # Elegir la primera opción cuyo texto sea numérico
            clicked = False
            for i in range(count):
                txt = (await opciones.nth(i).inner_text() or "").strip()
                if re.fullmatch(r"\d+", txt):
                    await opciones.nth(i).click()
                    clicked = True
                    break
            if not clicked:
                # Elegir la primera opción por defecto
                await opciones.first.click()

            await page.wait_for_timeout(SHORT)

            # 6) Generar certificado (botón)
            gen_btn = page.locator("button.btn-govco.fill-btn-govco:has-text('Generar certificado')").first
            if not await gen_btn.is_visible():
                gen_btn = page.locator("button.btn-govco.fill-btn-govco").first

            await gen_btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(200)

            # Capturar descarga
            download = None
            try:
                async with page.expect_download(timeout=120_000) as dl_info:
                    await gen_btn.click()
                download = await dl_info.value
            except Exception:
                try:
                    async with context.expect_event("download", timeout=120_000) as dl_info:
                        await gen_btn.click()
                    download = await dl_info.value
                except Exception:
                    # Sin evento de descarga → evidencia del estado
                    await page.wait_for_timeout(XLONG)
                    await page.screenshot(path=abs_png, full_page=True)
                    await browser.close(); browser = None
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=1,
                        estado="Validada",
                        mensaje="Se intentó generar el certificado pero no se detectó descarga.",
                        archivo=rel_png,
                    )
                    return

            # Guardar PDF
            await download.save_as(abs_pdf)
            await page.wait_for_timeout(800)

            # 7) Renderizar SOLO el PDF a PNG
            ok = await _render_pdf_to_png(abs_pdf, abs_png, context)
            if not ok:
                # respaldo: screenshot de la página
                await page.screenshot(path=abs_png, full_page=True)

            await browser.close(); browser = None

        # Registrar OK apuntando al PNG (evidencia del PDF)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=1,
            estado="Validada",
            mensaje="Certificado generado (PDF descargado).",
            archivo=rel_png,
        )

    except Exception as e:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=1,
            estado="Sin Validar",
            mensaje=str(e),
            archivo="",
        )
