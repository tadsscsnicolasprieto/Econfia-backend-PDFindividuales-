# bots/personeria.py
import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente
import PyPDF2  # para leer el texto del PDF
import fitz    # PyMuPDF, para convertir PDF a imagen

PAGE_URL = "https://antecedentes.personeriabogota.gov.co/expedicion-antecedentes"
NOMBRE_SITIO = "personeria"
TEXTO_OK = "NO REGISTRA SANCIONES NI INHABILIDADES VIGENTES"

# Mapeo de tipos de documento
TIPOS_DOC = {
    "CC": "1",   # Cédula de ciudadanía
    "CE": "2",   # Cédula de extranjería
    "PEP": "10", # Permiso especial de permanencia
    "PPT": "11", # Permiso por protección temporal
    "TI": "3",   # Tarjeta de identidad
}


async def consultar_personeria(consulta_id, cedula, tipo_doc, fecha_expedicion):
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_name = f"personeria_{cedula}_{timestamp}.pdf"
    png_name = f"personeria_{cedula}_{timestamp}.png"

    absolute_pdf = os.path.join(absolute_folder, pdf_name)
    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name)

    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)

    intentos = 0
    exito = False
    last_exception = None

    # Obtener el value real del tipo de documento
    tipo_doc_value = TIPOS_DOC.get(tipo_doc.upper())
    if not tipo_doc_value:
        raise ValueError(f"Tipo de documento no soportado: {tipo_doc}")

    while intentos < 3 and not exito:
        intentos += 1
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(accept_downloads=True)
                page = await context.new_page()

                print(f"[Intento {intentos}] Ingresando a Personería...")

                await page.goto(PAGE_URL, timeout=60000)

                # Seleccionar tipo de documento (usando el mapeo)
                await page.select_option("#tipo_documento", tipo_doc_value)
                await page.fill("#documento", cedula)

                # Formatear fecha
                fecha_fmt = datetime.strptime(fecha_expedicion, "%Y-%m-%d").strftime("%d/%m/%Y")

                # Quitar readonly y setear fecha con flatpickr
                await page.evaluate("document.getElementById('fecha_expedicion').removeAttribute('readonly')")
                await page.evaluate(f"""
                    var fp = document.getElementById('fecha_expedicion')._flatpickr;
                    if(fp) {{
                        fp.setDate('{fecha_fmt}', true);
                    }}
                """)

                # Enviar formulario
                await page.click("button[type='submit']")
                await page.wait_for_selector(".btn.btn-link.my-2.ms-1", timeout=30000)

                # Descargar archivo
                async with page.expect_download() as download_info:
                    await page.click(".btn.btn-link.my-2.ms-1")
                download = await download_info.value
                await download.save_as(absolute_pdf)

                await browser.close()

            # Leer PDF y verificar texto 
            pdf_text = ""
            try:
                with open(absolute_pdf, "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    for ppage in reader.pages:
                        pdf_text += ppage.extract_text() or ""
            except Exception as e:
                raise Exception(f"No se pudo leer el PDF: {e}")

            #Convertir primera página a PNG 
            try:
                pdf_doc = fitz.open(absolute_pdf)
                page = pdf_doc[0]  # primera página
                pix = page.get_pixmap(dpi=200)
                pix.save(absolute_png)
                pdf_doc.close()
            except Exception as e:
                raise Exception(f"No se pudo convertir PDF a PNG: {e}")

            # Evaluar resultado
            if TEXTO_OK in pdf_text.upper():
                score = 0
                mensaje = TEXTO_OK
            else:
                score = 10
                mensaje = "Se encontraron hallazgos en la consulta"

            # Guardar en BD como éxito (✅ solo guardamos la imagen)
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validado",
                mensaje=mensaje,
                archivo=relative_png
            )
            exito = True

        except Exception as e:
            last_exception = e
            try:
                if "page" in locals():
                    # Pantallazo en caso de error
                    await page.screenshot(path=absolute_png, full_page=True)
            except Exception as ss_err:
                print(f"No se pudo tomar pantallazo del error: {ss_err}")
            finally:
                try:
                    await browser.close()
                except:
                    pass

    # Si falla a los 3 intentos
    if not exito:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje="Ocurrió un problema al obtener la información de la fuente",
            archivo=relative_png if os.path.exists(absolute_png) else ""
        )
