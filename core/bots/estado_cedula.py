# core/bots/estado_cedula.py
import os
import re
import unicodedata
import asyncio
from datetime import datetime, date  # <-- para aceptar date/datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright
import fitz  # PyMuPDF

from core.models import Resultado, Fuente

URL = "https://certvigenciacedula.registraduria.gov.co/Datos.aspx"
NOMBRE_SITIO = "estado_cedula"

# ----------------- utils -----------------
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s).strip()
    return s.upper()

def _pdf_text(path: str) -> str:
    try:
        with fitz.open(path) as doc:
            return "\n".join(page.get_text("text") or "" for page in doc)
    except Exception:
        return ""

def _pdf_first_page_png(pdf_path: str, png_path: str, zoom: float = 2.0) -> bool:
    try:
        with fitz.open(pdf_path) as doc:
            if doc.page_count == 0:
                return False
            page = doc[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pix.save(png_path)
        return os.path.exists(png_path) and os.path.getsize(png_path) > 0
    except Exception:
        return False

def _make_fallback_pdf(pdf_path: str, text: str) -> None:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
        c = canvas.Canvas(pdf_path, pagesize=A4)
        w, h = A4
        c.setFont("Helvetica", 12)
        c.drawString(40, h - 60, text[:200])
        c.save()
    except Exception:
        pass

def _parse_datos(texto_pdf: str) -> dict:
    """Extrae campos del certificado del texto plano del PDF (tolerante a acentos/espacios)."""
    T = _norm(texto_pdf)

    def grab(rx: str):
        m = re.search(rx, T, flags=re.I)
        return (m.group(1).strip() if m else "")

    datos = {
        "cedula": grab(r"CEDULA\s+DE\s+CIUDADANIA\s*[:\-]\s*([\d\.\s]+)"),
        "fecha" : grab(r"FECHA\s+DE\s+EXPEDICION\s*[:\-]\s*([A-Z0-9\s]+)"),
        "lugar" : grab(r"LUGAR\s+DE\s+EXPEDICION\s*[:\-]\s*([A-Z\s\-\(\)]+)"),
        "nombre": grab(r"A\s+NOMBRE\s+DE\s*[:\-]\s*([A-Z\s\.\-]+)"),
        "estado": grab(r"ESTADO\s*[:\-]\s*([A-Z]+)"),
    }
    datos["cedula"] = datos["cedula"].replace(" ", "")
    return datos

def _mensaje_datos(datos: dict) -> str:
    encontrados = {k: v for k, v in datos.items() if v}
    if not encontrados:
        return "No se detectaron datos en el PDF del certificado. Revise el archivo."
    parts = []
    if datos.get("cedula"): parts.append(f"Cédula de Ciudadanía: {datos['cedula']}")
    if datos.get("fecha"):  parts.append(f"Fecha de Expedición: {datos['fecha']}")
    if datos.get("lugar"):  parts.append(f"Lugar de Expedición: {datos['lugar']}")
    if datos.get("nombre"): parts.append(f"A nombre de: {datos['nombre']}")
    if datos.get("estado"): parts.append(f"Estado: {datos['estado']}")
    return "\n".join(parts)

def _split_fecha(fecha_expedicion) -> tuple[str, str, str]:
    """
    Acepta datetime.date, datetime.datetime o str.
    Devuelve (YYYY, M, D) sin ceros a la izquierda.
    Soporta 'YYYY-MM-DD' y 'DD-MM-YYYY' (y '/' como separador).
    """
    if isinstance(fecha_expedicion, (datetime, date)):
        return str(fecha_expedicion.year), str(fecha_expedicion.month), str(fecha_expedicion.day)

    s = str(fecha_expedicion or "").strip().replace("/", "-")
    parts = [p for p in s.split("-") if p]
    if len(parts) != 3:
        raise ValueError("Formato de fecha inválido. Usa YYYY-MM-DD o date/datetime")
    if len(parts[0]) == 4:  # YYYY-MM-DD
        y, m, d = parts
    else:                   # DD-MM-YYYY
        d, m, y = parts
    return str(int(y)), str(int(m)), str(int(d))

async def _select_fecha(page, d: str, m: str, y: str):
    """Selecciona día/mes/año intentando value sin/padded y por label."""
    # Día
    try:
        await page.select_option('#ContentPlaceHolder1_DropDownList1', value=d)
    except Exception:
        try:
            await page.select_option('#ContentPlaceHolder1_DropDownList1', value=d.zfill(2))
        except Exception:
            await page.select_option('#ContentPlaceHolder1_DropDownList1', label=d.zfill(2))
    # Mes
    try:
        await page.select_option('#ContentPlaceHolder1_DropDownList2', value=m)
    except Exception:
        try:
            await page.select_option('#ContentPlaceHolder1_DropDownList2', value=m.zfill(2))
        except Exception:
            await page.select_option('#ContentPlaceHolder1_DropDownList2', label=m.zfill(2))
    # Año
    try:
        await page.select_option('#ContentPlaceHolder1_DropDownList3', value=y)
    except Exception:
        await page.select_option('#ContentPlaceHolder1_DropDownList3', label=y)

# ----------------- BOT -----------------
async def consultar_estado_cedula(consulta_id: int, cedula: str, fecha_expedicion):
    """
    - Llenar formulario (Datos.aspx), reintentar CAPTCHA 'LANAP' hasta pasar.
    - En Respuesta.aspx, click en 'Generar Certificado' y descargar PDF.
    - Generar PNG (primera página) y extraer campos para mensaje.
    - Guardar Resultado con PNG en 'archivo'.
    """
    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    # Rutas
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_name = f"{NOMBRE_SITIO}_{cedula}_{ts}.pdf"
    png_name = f"{NOMBRE_SITIO}_{cedula}_{ts}.png"
    abs_pdf = os.path.join(absolute_folder, pdf_name)
    rel_pdf = os.path.join(relative_folder, pdf_name).replace("\\", "/")
    abs_png = os.path.join(absolute_folder, png_name)
    rel_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    navegador = None
    try:
        anio, mes, dia = _split_fecha(fecha_expedicion)

        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            ctx = await navegador.new_context(accept_downloads=True, viewport={"width": 1440, "height": 950})
            page = await ctx.new_page()

            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # Llenar formulario
            await page.fill('#ContentPlaceHolder1_TextBox1', str(cedula))
            await _select_fecha(page, dia, mes, anio)

            await asyncio.sleep(0.6)

            # Reintento de CAPTCHA fijo "LANAP"
            MAX_INTENTOS = 10
            paso = False
            for intento in range(1, MAX_INTENTOS + 1):
                await page.fill('#ContentPlaceHolder1_TextBox2', 'LANAP')
                await asyncio.sleep(0.25)
                await page.click('#ContentPlaceHolder1_Button1')

                # si aparece diálogo -> captcha falló
                try:
                    dialog = await page.wait_for_event("dialog", timeout=1800)
                    await dialog.dismiss()
                    await page.fill('#ContentPlaceHolder1_TextBox2', '')
                    await asyncio.sleep(0.45)
                    continue
                except Exception:
                    pass

                # verificar navegación a Respuesta.aspx
                try:
                    await page.wait_for_url("**/Respuesta.aspx**", timeout=3500)
                    paso = True
                    break
                except Exception:
                    # no hubo diálogo pero tampoco cambió; intentar de nuevo
                    await page.fill('#ContentPlaceHolder1_TextBox2', '')
                    await asyncio.sleep(0.45)

            if not paso:
                raise RuntimeError("CAPTCHA no validado tras varios intentos.")

            # Respuesta.aspx -> Generar certificado (descarga PDF)
            await asyncio.sleep(0.6)
            async with page.expect_download(timeout=60000) as dl:
                await page.click("input#ContentPlaceHolder1_Button1")  # Generar Certificado
            download = await dl.value
            try:
                await download.save_as(abs_pdf)
            except Exception:
                tmp = await download.path()
                if tmp:
                    os.replace(tmp, abs_pdf)

            await navegador.close()
            navegador = None

        # Evidencia PNG + extracción de datos
        if not os.path.exists(abs_pdf) or os.path.getsize(abs_pdf) < 500:
            _make_fallback_pdf(abs_pdf, f"RNEC – sin datos visibles para cédula {cedula}")

        _pdf_first_page_png(abs_pdf, abs_png, zoom=2.0)
        texto = _pdf_text(abs_pdf)
        datos = _parse_datos(texto)
        mensaje = _mensaje_datos(datos)

        # Guardar resultado (PNG en archivo)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Validada",
            mensaje=mensaje,
            archivo=rel_png
        )

    except Exception as e:
        # Evidencia mínima si hubo error
        try:
            _make_fallback_pdf(abs_pdf, f"RNEC – error: {e}")
            _pdf_first_page_png(abs_pdf, abs_png, zoom=2.0)
        except Exception:
            pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje=str(e) or "Ocurrió un problema al obtener el certificado",
            archivo=rel_png if os.path.exists(abs_png) else ""
        )
        try:
            if navegador:
                await navegador.close()
        except Exception:
            pass