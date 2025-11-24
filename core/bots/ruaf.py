import os
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from core.resolver.captcha_img2 import resolver_captcha_imagen
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente
import cv2
import numpy as np
import fitz  # PyMuPDF


def preprocesar_captcha(ruta_origen, ruta_destino):
    img = cv2.imread(ruta_origen)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    lower_green = np.array([35, 40, 40])
    upper_green = np.array([85, 255, 255])
    mask = cv2.inRange(hsv, lower_green, upper_green)
    letras_negras = cv2.bitwise_not(mask)
    cv2.imwrite(ruta_destino, letras_negras)


def pdf_a_imagen(pdf_path, output_path, dpi=200):
    """Convierte la primera página de un PDF en PNG usando PyMuPDF."""
    doc = fitz.open(pdf_path)
    pagina = doc[0]  # primera página
    pix = pagina.get_pixmap(dpi=dpi)
    pix.save(output_path)
    doc.close()


url = "https://ruaf.sispro.gov.co/Filtro.aspx"
nombre_sitio = "ruaf"

TIPO_DOC_MAP = {
    'CC': '5|CC', 'PA': '6|PA', 'AS': '7|AS', 'CD': '10|CD',
    'CN': '12|CN', 'SC': '13|SC', 'PE': '14|PE', 'PT': '15|PT',
    'MS': '1|MS', 'RC': '2|RC', 'TI': '3|TI', 'CE': '4|CE'
}


async def consultar_ruaf(cedula, tipo_doc, fecha_expedicion, consulta_id):
    MAX_INTENTOS = 3
    relative_folder = os.path.join('resultados', str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    fuente_obj = await sync_to_async(Fuente.objects.filter(nombre=nombre_sitio).first)()

    for intento_general in range(1, MAX_INTENTOS + 1):
        try:
            print(f"🔄 Intento general {intento_general} de {MAX_INTENTOS}")
            tipo_doc_val = TIPO_DOC_MAP.get(tipo_doc.upper())
            if not tipo_doc_val:
                raise ValueError(f"Tipo de documento no válido: {tipo_doc}")

            async with async_playwright() as p:
                navegador = await p.chromium.launch(headless=True)
                pagina = await navegador.new_page()
                await pagina.goto(url)

                # Selección de radio y enviar formulario inicial
                if await pagina.locator('input[id="MainContent_RadioButtonList1_0"]').is_visible():
                    await pagina.click('input[id="MainContent_RadioButtonList1_0"]')
                if await pagina.locator('input[id="MainContent_btnEnviar"]').is_visible():
                    await pagina.click('input[id="MainContent_btnEnviar"]')

                # Llenar formulario
                await pagina.wait_for_selector('select[id="ddlTiposDocumentos"]')
                await pagina.select_option('select[id="ddlTiposDocumentos"]', tipo_doc_val)
                await pagina.fill('input[id="MainContent_txbNumeroIdentificacion"]', cedula)

                if isinstance(fecha_expedicion, datetime):
                    fecha_str = fecha_expedicion.strftime("%Y-%m-%d")
                elif hasattr(fecha_expedicion, "strftime"):  # datetime.date
                    fecha_str = fecha_expedicion.strftime("%Y-%m-%d")
                else:
                    fecha_str = str(fecha_expedicion)

                fecha_formateada = datetime.strptime(fecha_str, "%Y-%m-%d").strftime("%d/%m/%Y")
                await pagina.fill('input[id="MainContent_datepicker"]', fecha_formateada)
                await pagina.keyboard.press("Escape")
                await asyncio.sleep(1)

                # Intentos de captcha
                for intento_captcha in range(1, MAX_INTENTOS + 1):
                    captcha_path = os.path.join(absolute_folder, f"captcha_{nombre_sitio}.png")
                    await pagina.wait_for_selector('img[src*="CaptchaImage.axd"]', timeout=10000)
                    await pagina.locator('img[src*="CaptchaImage.axd"]').screenshot(path=captcha_path)

                    preprocesar_captcha(captcha_path, captcha_path)
                    await asyncio.sleep(1)
                    captcha_texto = await resolver_captcha_imagen(captcha_path)
                    os.remove(captcha_path)

                    await pagina.fill('input[id="MainContent_txtCaptcha"]', captcha_texto)
                    await pagina.click("input[id='MainContent_btnVerify']")
                    await pagina.wait_for_load_state("networkidle")

                    await pagina.wait_for_selector('span#MainContent_lblMessage', timeout=5000)
                    mensaje = (await pagina.locator('span#MainContent_lblMessage').inner_text()).strip()

                    if "Texto Inválido" in mensaje:
                        print(f"❌ Captcha inválido (intento {intento_captcha})")
                        continue
                    elif "Texto Válido" in mensaje:
                        print("✅ Captcha válido, descargando PDF...")
                        await pagina.click('input[id="MainContent_btnConsultar"]')
                        
                        await pagina.click('a#ctl00_MainContent_rvConsulta_ctl09_ctl04_ctl00_ButtonLink')
                        
                        await asyncio.sleep(2)
                        await pagina.keyboard.press("Enter")
                        await pagina.wait_for_selector('a.ActiveLink[title="PDF"]', timeout=5000)

                        # Descargar PDF
                        async with pagina.expect_download() as descarga_info:
                            await pagina.click('a.ActiveLink[title="PDF"]')
                        descarga = await descarga_info.value

                        pdf_path = os.path.join(
                            absolute_folder,
                            f"{nombre_sitio}_{cedula}_{timestamp}.pdf"
                        )
                        await descarga.save_as(pdf_path)

                        # Convertir PDF a PNG
                        imagen_path = pdf_path.replace(".pdf", ".png")
                        pdf_a_imagen(pdf_path, imagen_path)

                        if fuente_obj:
                            await sync_to_async(Resultado.objects.create)(
                                consulta_id=consulta_id,
                                fuente=fuente_obj,
                                score=0,
                                estado="Validado",
                                mensaje="",
                                archivo=os.path.join(relative_folder, os.path.basename(imagen_path))
                            )
                        await navegador.close()
                        return
                    else:
                        continue

                # si no pasó captcha
                await navegador.close()
                print("⚠️ Fallo captcha en todos los intentos")

        except Exception as e:
            print(f"❌ Error intento {intento_general}: {e}")
            if intento_general == MAX_INTENTOS:
                error_screenshot = os.path.join(
                    absolute_folder,
                    f"{nombre_sitio}_{cedula}_{timestamp}_error.png"
                )
                try:
                    if 'pagina' in locals():
                        await pagina.screenshot(path=error_screenshot, full_page=False)
                except:
                    img_blank = np.ones((400, 600, 3), dtype=np.uint8) * 255
                    cv2.putText(img_blank, "Error en la consulta", (50, 200),
                                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2, cv2.LINE_AA)
                    cv2.imwrite(error_screenshot, img_blank)

                if fuente_obj:
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=0,
                        estado="Sin validar",
                        mensaje="No se pudo realizar la consulta en el momento.",
                        archivo=os.path.join(relative_folder, os.path.basename(error_screenshot))
                    )
