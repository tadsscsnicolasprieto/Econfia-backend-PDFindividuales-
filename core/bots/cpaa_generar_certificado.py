# core/bots/cpaa_generar_certificado.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "cpaa_generar_certificado"
URL = "https://app1.cpaa.gov.co/generar_certificado.php"

# Selectores
SEL_SELECT_TIPO   = "#id_tipo_documento"
SEL_INPUT_NUM     = "#numero_identificacion"
SEL_BTN_SUBMIT    = 'input[type="submit"][value="Consultar"]'
SEL_BTN_GENERAR   = "a[href*='descargar_certificado.php'], a:has-text('Generar Certificado')"

# Timings
WAIT_AFTER_NAV     = 12000
WAIT_AFTER_CLICK   = 1500
EXTRA_RESULT_SLEEP = 3000  # ms

# Mapa de tipos
TIPO_DOC_MAP = {
    "CC": "1",  # Cédula de Ciudadanía
    "CE": "2",  # Cédula de Extranjería
    "PEP": "3",
    "PA": "4",  # Pasaporte
    "TI": "5",  # Tarjeta de Identidad
    "NIT": "6",
}

# -------- helpers PDF -> PNG (no depender del visor) --------
def _render_with_pypdfium2(abs_pdf: str, abs_png: str, scale: float = 2.0) -> bool:
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(abs_pdf)
        page0 = pdf.get_page(0)
        bmp = page0.render(scale=scale)
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
    if _render_with_pypdfium2(abs_pdf, abs_png): return True
    if _render_with_pymupdf(abs_pdf, abs_png):   return True
    if _render_with_pdf2image(abs_pdf, abs_png): return True
    # último recurso: abrir file:// y fotografiar
    try:
        viewer = await context.new_page()
        await viewer.goto(f"file://{abs_pdf}", wait_until="load")
        await viewer.wait_for_timeout(900)
        await viewer.screenshot(path=abs_png, full_page=True)
        await viewer.close()
        return True
    except Exception:
        return False

# -------- extraer texto del PDF --------
def _pdf_text_pypdf(abs_pdf: str) -> str:
    try:
        from pypdf import PdfReader
        r = PdfReader(abs_pdf)
        parts = []
        for pg in r.pages:
            try:
                parts.append(pg.extract_text() or "")
            except Exception:
                pass
        return "\n".join(parts)
    except Exception:
        return ""

def _pdf_text_pdfminer(abs_pdf: str) -> str:
    try:
        from pdfminer.high_level import extract_text
        return extract_text(abs_pdf) or ""
    except Exception:
        return ""

def _clean(s: str) -> str:
    return re.sub(r"[ \t]+", " ", (s or "").replace("\xa0", " ")).strip()

def _get_next_line(lines, idx):
    j = idx + 1
    while j < len(lines):
        val = lines[j].strip()
        if val:
            return val
        j += 1
    return ""

def _mensaje_desde_pdf(texto: str) -> str:
    """
    Devuelve un resumen legible con los campos más importantes.
    Intenta robustamente con líneas + palabras clave.
    """
    t = _clean(texto)
    lines = [ln.strip() for ln in t.splitlines()]

    # Nombre y CC en el párrafo largo
    nombre = ""
    cc = ""
    m = re.search(
        r"se\s+constat[oó]\s+que\s+el\s*\(la\)\s*señor\s*\(a\)\s+(.+?)\s+identificado\(a\)\s+con\s+C[ée]dula.*?No\.?\s*([\d\.]+)",
        t, re.I
    )
    if m:
        nombre = _clean(m.group(1))
        cc = _clean(m.group(2).replace(".", ""))

    titulo = tarjeta = fecha = estado = ""

    # buscar por etiquetas
    for i, ln in enumerate(lines):
        ln_up = ln.upper()
        if "TITULO PROFESIONAL" in ln_up or "TÍTULO PROFESIONAL" in ln_up:
            titulo = _get_next_line(lines, i)
        elif ("NO. TARJETA PROFESIONAL" in ln_up) or ("N° DE TARJETA" in ln_up) or ("N." in ln_up and "TARJETA" in ln_up):
            tarjeta = _get_next_line(lines, i)
        elif "FECHA EXPEDICIÓN" in ln_up or "FECHA DE EXPEDICIÓN" in ln_up:
            fecha = _get_next_line(lines, i)
        elif ln_up.strip() == "ESTADO":
            estado = _get_next_line(lines, i)

    partes = []
    if nombre or cc:
        partes.append(f"{nombre}, C.C. {cc}".strip(", "))
    if titulo:
        partes.append(f"Título profesional: {titulo}")
    if tarjeta:
        partes.append(f"No. tarjeta: {tarjeta}")
    if fecha:
        partes.append(f"Fecha expedición: {fecha}")
    if estado:
        partes.append(f"Estado: {estado}")

    # fallback si no se logró extraer nada
    if not partes:
        if "VIGENTE" in t.upper():
            partes.append("Estado: Vigente")
        if "NO REGISTRA" in t.upper():
            partes.append("Observación: No registra")
    return " | ".join(partes) if partes else "Certificado descargado (no se pudo extraer campos clave)."

# --------- BOT principal ---------
async def consultar_cpaa_generar_certificado(consulta_id: int, tipo_doc: str, numero: str):
    browser = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=1,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    try:
        # 2) Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_num = re.sub(r"\s+", "_", (numero or "").strip()) or "consulta"

        png_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"
        abs_png  = os.path.join(absolute_folder, png_name)
        rel_png  = os.path.join(relative_folder, png_name).replace("\\", "/")

        pdf_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.pdf"
        abs_pdf  = os.path.join(absolute_folder, pdf_name)
        rel_pdf  = os.path.join(relative_folder, pdf_name).replace("\\", "/")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1440,1000",
                ]
            )
            ctx = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1440, "height": 1000},
                locale="es-CO",
                user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
            )
            page = await ctx.new_page()

            # 3) Navegar
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # 4) Seleccionar tipo de documento
            await page.wait_for_selector(SEL_SELECT_TIPO, state="visible", timeout=20000)
            value = TIPO_DOC_MAP.get((tipo_doc or "").strip().upper(), "1")
            await page.select_option(SEL_SELECT_TIPO, value=value)

            # 5) Ingresar número y Consultar
            await page.fill(SEL_INPUT_NUM, "")
            await page.type(SEL_INPUT_NUM, str(numero or ""), delay=20)

            await page.click(SEL_BTN_SUBMIT)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_CLICK)
            except Exception:
                pass

            # 6) Esperar respuesta
            await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)

            # 7) ¿Hay botón/enlace Generar Certificado?
            generar = page.locator(SEL_BTN_GENERAR).first
            tiene_generar = False
            try:
                await generar.wait_for(state="visible", timeout=4000)
                tiene_generar = True
            except Exception:
                tiene_generar = False

            if not tiene_generar:
                # No hay certificado para descargar → evidencia de la página con el mensaje que muestre
                await page.screenshot(path=abs_png, full_page=True)
                await ctx.close(); await browser.close(); browser = None
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=1,
                    estado="Validada",
                    mensaje="No hay certificado asociado (CPAA).",
                    archivo=rel_png,
                )
                return

            # 8) Descargar PDF del certificado
            download = None
            try:
                await generar.scroll_into_view_if_needed()
                async with page.expect_download(timeout=90_000) as dl:
                    await generar.click()
                download = await dl.value
            except Exception:
                # fallback: a veces el download se emite a nivel de contexto
                try:
                    async with ctx.expect_event("download", timeout=90_000) as dl:
                        await generar.click()
                    download = await dl.value
                except Exception:
                    # si aún no, evidencia
                    await page.screenshot(path=abs_png, full_page=True)
                    await ctx.close(); await browser.close(); browser = None
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=1,
                        estado="Validada",
                        mensaje="Se intentó generar el certificado pero no se detectó descarga.",
                        archivo=rel_png,
                    )
                    return

            await download.save_as(abs_pdf)

            # 9) Renderizar SOLO el PDF a PNG
            ok = await _render_pdf_to_png(abs_pdf, abs_png, ctx)
            if not ok:
                # respaldo: screenshot de la vista actual
                await page.screenshot(path=abs_png, full_page=True)

            # 10) Leer PDF y armar mensaje
            text = _pdf_text_pypdf(abs_pdf)
            if not text.strip():
                text = _pdf_text_pdfminer(abs_pdf)
            mensaje = _mensaje_desde_pdf(text)

            await ctx.close(); await browser.close(); browser = None

        # 11) Registrar OK con mensaje del PDF
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=1,
            estado="Validada",
            mensaje=mensaje,
            archivo=rel_png,   # evidencia PNG del PDF
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=1,
                estado="Sin Validar",
                mensaje=str(e),
                archivo="",
            )
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
