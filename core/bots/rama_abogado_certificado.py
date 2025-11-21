# core/bots/rama_abogado_certificado.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

# ---------- Config ----------
URL = "https://vigenciaspublicas.ramajudicial.gov.co/Certificados.aspx"
NOMBRE_SITIO = "rama_abogado_certificado"

# tiempos
NAV_TIMEOUT = 120_000
TINY   = 400
SHORT  = 1200
MEDIUM = 2500
LONG   = 4000
XLONG  = 9000

# ---------- Helpers de BD ----------
async def _guardar_resultado(consulta_id, fuente_obj, estado, mensaje, rel_path, score=1):
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=score,          # Score fijo = 1
        estado=estado,        # "Validada" / "Sin Validar"
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

# ---------- Render PDF → PNG ----------
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
        await viewer.wait_for_timeout(MEDIUM)
        await viewer.screenshot(path=abs_png, full_page=True)
        await viewer.close()
        return True
    except Exception:
        return False

# ---------- Texto del PDF ----------
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

# ---------- Parsing del PDF (NO inscrito vs inscrito) ----------
def _parse_mensaje_from_pdf_text(texto: str, cedula: str) -> str:
    """
    Arma el mensaje final desde el contenido del PDF.
    - Caso NO inscrito: frase canónica con la cédula.
    - Caso inscrito: intenta extraer nombre, estado y datos clave.
    """
    t = _clean(texto)

    # Caso “NO ESTÁ INSCRITA”
    rx_no = re.compile(
        r"NO\s+EST[ÁA]\s+INSCRIT[AO]A?.*?Unidad.*?Registro.*?Abogados.*?como\s+abogad[oa]",
        re.I | re.S
    )
    if rx_no.search(t):
        return (
            f"La persona identificada con Cédula de Ciudadanía N.° {cedula}, "
            "NO ESTÁ INSCRITA en la Unidad de Registro Nacional de Abogados y Auxiliares de la Justicia como abogado(a)."
        )

    # Caso inscrito: nombre (permitimos saltos con DOTALL)
    m_nombre = re.search(
        r"que\s+el\(la\)\s+(?P<nombre>.+?)\s*,\s*quien\s+se\s+identifica\s+con\s+C[ÉE]DULA",
        t, re.I | re.S
    )
    nombre = _clean(m_nombre.group("nombre")) if m_nombre else ""

    # Estado/fecha RNA
    m_estado_rna = re.search(
        r"VIGENCIA\s+RNA.*?ESTADO\s+DE\s+INSCRIPCI[ÓO]N\s+RNA\s*(?P<estado>\w+)",
        t, re.I | re.S
    )
    estado_rna = _clean(m_estado_rna.group("estado")) if m_estado_rna else ""

    m_fecha_rna = re.search(
        r"FECHA\s+DE\s+INSCRIPCI[ÓO]N\s+RNA\s*1?\s*(?P<fecha>\d{2}/\d{2}/\d{4})",
        t, re.I
    )
    fecha_rna = m_fecha_rna.group("fecha") if m_fecha_rna else ""

    # TPA: número / estado / fecha
    m_tpa_num = re.search(
        r"N\.\s*°?\s*DE\s*TARJETA\s*PROFESIONAL\s*(?P<num>\d+)",
        t, re.I | re.S
    )
    tpa_num = m_tpa_num.group("num") if m_tpa_num else "NO REGISTRA"

    m_tpa_estado = re.search(
        r"VIGENCIA\s+TPA.*?ESTADO\s*2?\s*(?P<estado>\w+)",
        t, re.I | re.S
    )
    estado_tpa = _clean(m_tpa_estado.group("estado")) if m_tpa_estado else ""

    # ⬇️ FIX: uso correcto de (?P<fecha>...)
    m_tpa_fecha = re.search(
        r"FECHA\s+DE\s+EXPEDICI[ÓO]N\s*(?P<fecha>\d{2}/\d{2}/\d{4})",
        t, re.I
    )
    if m_tpa_fecha:
        fecha_tpa = m_tpa_fecha.group("fecha")
    else:
        # Fallback sin nombre de grupo
        m_tpa_fecha2 = re.search(r"FECHA\s+DE\s+EXPEDICI[ÓO]N\s*(\d{2}/\d{2}/\d{4})", t, re.I)
        fecha_tpa = m_tpa_fecha2.group(1) if m_tpa_fecha2 else ""

    partes = []
    partes.append("Registro encontrado (ABOGADO).")
    if nombre:
        partes.append(f"Nombre: {nombre}")
    partes.append(f"C.C.: {cedula}")
    if estado_rna or fecha_rna:
        partes.append(f"RNA: {estado_rna or '—'}" + (f", Fecha inscripción: {fecha_rna}" if fecha_rna else ""))
    if tpa_num or estado_tpa or fecha_tpa:
        partes.append(
            f"TPA: {tpa_num or '—'}"
            + (f", Estado: {estado_tpa}" if estado_tpa else "")
            + (f", Fecha expedición: {fecha_tpa}" if fecha_tpa else "")
        )
    return " | ".join(partes)

# ---------- Selecciones ----------
TIPO_DOC_LABEL = {
    "CC": "CÉDULA DE CIUDADANÍA",
    "CE": "CÉDULA DE EXTRANJERÍA",
    "TI": "TARJETA DE IDENTIDAD",
    "PAS": "PASAPORTE",
    "NIT": "NIT",
    "PPT": "PERMISO POR PROTECCIÓN TEMPORAL",
}

async def _select_tipo_doc(page, tipo_doc: str):
    sel = page.locator(
        "select#ddlTipoDocumento, select#ddlTipoIdentificacion, "
        "select[name='ddlTipoDocumento'], select[name='ddlTipoIdentificacion']"
    ).first
    await sel.wait_for(state="visible", timeout=15_000)
    await sel.scroll_into_view_if_needed()
    label = TIPO_DOC_LABEL.get((tipo_doc or "").upper(), "CÉDULA DE CIUDADANÍA")
    try:
        await sel.select_option(label=label)
        return
    except Exception:
        fallback_val = "1" if (tipo_doc or "").upper() == "CC" else None
        if fallback_val:
            try:
                await sel.select_option(value=fallback_val)
                return
            except Exception:
                pass
        return

# ---------- BOT ----------
async def consultar_rama_abogado_certificado(consulta_id: int, cedula: str, tipo_doc: str):
    """
    Rama Judicial – Certificado vigencias públicas (ABOGADO).
    - Selecciona ABOGADO, tipo doc, cédula.
    - Descarga PDF, lo renderiza a PNG y extrae mensaje.
    """
    navegador = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await _guardar_resultado(consulta_id, None, "Sin Validar",
                                 f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", "", score=1)
        return

    # Rutas
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_num = (cedula or "").strip().replace(" ", "_") or "consulta"
    pdf_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.pdf"
    png_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"

    abs_pdf = os.path.join(absolute_folder, pdf_name)
    rel_pdf = os.path.join(relative_folder, pdf_name).replace("\\", "/")
    abs_png = os.path.join(absolute_folder, png_name)
    rel_png = os.path.join(relative_folder, png_name).replace("\\", "/")

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
                ],
            )
            context = await navegador.new_context(
                accept_downloads=True,
                viewport={"width": 1440, "height": 1000},
                locale="es-CO",
                user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
            )
            page = await context.new_page()

            # 1) Ir
            await page.goto(URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(SHORT)
            await _wait_idle(page, t=LONG)

            # 2) Seleccionar ABOGADO
            sel_calidad = page.locator("select#ddlCalidad, select[name='ddlCalidad']").first
            await sel_calidad.wait_for(state="visible", timeout=20_000)
            await sel_calidad.scroll_into_view_if_needed()
            try:
                await sel_calidad.select_option(value="1")  # ABOGADO
            except Exception:
                try:
                    await sel_calidad.select_option(label=re.compile("ABOGADO", re.I))
                except Exception:
                    pass
            await page.wait_for_timeout(TINY)

            # 3) Tipo de documento
            await _select_tipo_doc(page, tipo_doc)
            await page.wait_for_timeout(TINY)

            # 4) Cédula
            txt_doc = page.locator("#txtDocumento, input[name='txtDocumento']").first
            await txt_doc.wait_for(state="visible", timeout=15_000)
            await txt_doc.scroll_into_view_if_needed()
            await txt_doc.fill("")
            await txt_doc.type(str(cedula or "").strip(), delay=20)
            await page.wait_for_timeout(TINY)

            # 5) Consultar / Generar
            btn = page.locator(
                "input[type='submit'][value*='Consultar'], "
                "input[type='submit'][value*='Generar'], "
                "button#btnGenerar, input#btnGenerar, "
                "button:has-text('Consultar'), button:has-text('Generar')"
            ).first
            await btn.wait_for(state="visible", timeout=20_000)
            await btn.scroll_into_view_if_needed()

            # Capturar descarga (page y fallback context)
            download = None
            try:
                async with page.expect_download(timeout=120_000) as dl:
                    await btn.click()
                download = await dl.value
            except Exception:
                try:
                    async with context.expect_event("download", timeout=120_000) as dl:
                        await btn.click()
                    download = await dl.value
                except Exception:
                    # Sin evento de descarga → evidencia de la vista
                    await page.wait_for_timeout(XLONG)
                    await page.evaluate("window.scrollTo(0,0)")
                    await page.screenshot(path=abs_png, full_page=True)
                    await navegador.close(); navegador = None
                    await _guardar_resultado(
                        consulta_id, fuente_obj, "Validada",
                        "No se detectó descarga, se guardó evidencia de la vista.", rel_png, score=1
                    )
                    return

            # 6) Guardar PDF
            await download.save_as(abs_pdf)
            await page.wait_for_timeout(800)

            # 7) Render a PNG (sin visor)
            ok = await _render_pdf_to_png(abs_pdf, abs_png, context)
            if not ok:
                await page.evaluate("window.scrollTo(0,0)")
                await page.screenshot(path=abs_png, full_page=True)

            # 8) Leer PDF → mensaje
            text = _pdf_text_pypdf(abs_pdf)
            if not text.strip():
                text = _pdf_text_pdfminer(abs_pdf)

            mensaje = _parse_mensaje_from_pdf_text(text, str(cedula).strip())

            await navegador.close(); navegador = None

        # 9) Guardar resultado
        await _guardar_resultado(consulta_id, fuente_obj, "Validada", mensaje, rel_png, score=1)

    except Exception as e:
        try:
            if navegador:
                await navegador.close()
        except Exception:
            pass
        await _guardar_resultado(
            consulta_id, fuente_obj, "Sin Validar", f"{type(e).__name__}: {e}", "", score=1
        )
