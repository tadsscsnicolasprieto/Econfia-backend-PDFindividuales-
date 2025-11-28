import os
import asyncio
from datetime import datetime
from django.conf import settings

from playwright.async_api import async_playwright
from asgiref.sync import sync_to_async

from core.resolver.captcha_img2 import resolver_captcha_imagen
from core.models import Resultado, Fuente

import cv2
import numpy as np
import fitz  # PyMuPDF
import traceback


URL = "https://ruaf.sispro.gov.co/Filtro.aspx?AspxAutoDetectCookieSupport=1"
NOMBRE_SITIO = "ruaf"

TIPO_DOC_MAP = {
    'CC': '5|CC', 'PA': '6|PA', 'AS': '7|AS', 'CD': '10|CD',
    'CN': '12|CN', 'SC': '13|SC', 'PE': '14|PE', 'PT': '15|PT',
    'MS': '1|MS', 'RC': '2|RC', 'TI': '3|TI', 'CE': '4|CE'
}


def preprocesar_captcha(ruta_origen, ruta_destino):
    """Resalta las letras negras sobre fondo verde."""
    img = cv2.imread(ruta_origen)
    if img is None:
        return
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    lower_green = np.array([35, 40, 40])
    upper_green = np.array([85, 255, 255])
    mask = cv2.inRange(hsv, lower_green, upper_green)
    letras_negras = cv2.bitwise_not(mask)
    cv2.imwrite(ruta_destino, letras_negras)


def pdf_a_imagen(pdf_path, output_path, dpi=200):
    """Convierte la primera página de un PDF en PNG usando PyMuPDF."""
    doc = fitz.open(pdf_path)
    pagina = doc[0]
    pix = pagina.get_pixmap(dpi=dpi)
    pix.save(output_path)
    doc.close()


async def _aceptar_terminos(page):
    """
    Acepta el modal de términos y condiciones si aparece.
    Es robusto: prueba varios selectores típicos.
    """
    print("⏳ Verificando modal de Términos y Condiciones...")

    try:
        # Buscamos algún checkbox típico del modal
        chk_selectors = [
            '#MainContent_chkPoliticas',
            '#MainContent_chkAcepto',
            'input[type="checkbox"][id*="chk"]',
        ]

        checkbox = None
        for sel in chk_selectors:
            if await page.locator(sel).count() > 0:
                checkbox = sel
                break

        if not checkbox:
            print("ℹ No se detectó modal de términos (posiblemente ya aceptado).")
            return

        print(f"✔ Checkbox de términos encontrado: {checkbox}")
        await page.click(checkbox)

        await asyncio.sleep(0.5)

        # Buscar botón de aceptar
        btn_selectors = [
            '#MainContent_btnAceptar',
            'input[id*="btnAceptar"]',
            'input[type="submit"][value*="Aceptar"]',
            'button:has-text("Aceptar")',
        ]

        btn = None
        for sel in btn_selectors:
            if await page.locator(sel).count() > 0:
                btn = sel
                break

        if not btn:
            print("⚠ No se encontró botón de Aceptar, se continúa de todas formas.")
            return

        print(f"✔ Botón de Aceptar encontrado: {btn}")
        await page.click(btn)

        # Esperar a que desaparezca el botón (el modal se cierra)
        try:
            await page.locator(btn).wait_for(state="detached", timeout=15000)
        except Exception:
            pass

        print("✔ Términos aceptados correctamente.")

    except Exception as e:
        print(f"⚠ Error aceptando términos, se continúa de todas formas: {e}")


async def consultar_ruaf(cedula, tipo_doc, consulta_id, fecha_expedicion=None):
    """
    Bot RUAF:
    - Acepta términos
    - Entra al iframe del formulario
    - Llenar datos
    - Resolver captcha y validar
    - Descargar PDF, convertir a PNG
    - Guardar Resultado en BD
    """
    MAX_INTENTOS = 3
    MAX_INTENTOS_CAPTCHA = 3

    relative_folder = os.path.join('resultados', str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    fuente_obj = await sync_to_async(Fuente.objects.filter(nombre=NOMBRE_SITIO).first)()

    # Normalizar fecha de expedición a dd/mm/YYYY
    # Si no se proporciona, usar la fecha de hoy
    if fecha_expedicion is None:
        fecha_str = datetime.now().strftime("%Y-%m-%d")
    elif isinstance(fecha_expedicion, datetime):
        fecha_str = fecha_expedicion.strftime("%Y-%m-%d")
    elif hasattr(fecha_expedicion, "strftime"):
        fecha_str = fecha_expedicion.strftime("%Y-%m-%d")
    else:
        fecha_str = str(fecha_expedicion)

    fecha_formateada = datetime.strptime(fecha_str, "%Y-%m-%d").strftime("%d/%m/%Y")

    tipo_documento_val = TIPO_DOC_MAP.get(str(tipo_doc).upper())
    if not tipo_documento_val:
        raise ValueError(f"Tipo de documento no válido: {tipo_doc}")

    navegador = None
    page = None

    for intento_general in range(1, MAX_INTENTOS + 1):
        try:
            print(f"🔄 [RUAF] Intento general {intento_general}/{MAX_INTENTOS}")

            async with async_playwright() as p:
                navegador = await p.chromium.launch(
                    headless=False,  # Desactivar headless para evitar detección
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-default-apps",
                        "--disable-sync",
                        "--disable-extensions",
                    ]
                )
                page = await navegador.new_page(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                
                # Agregar headers realistas
                await page.set_extra_http_headers({
                    "Accept-Language": "es-ES,es;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://www.google.com/",
                })
                
                await page.goto(URL, wait_until="load", timeout=60000)

                # 1) Aceptar términos y condiciones si aplica
                await _aceptar_terminos(page)

                # 2) Esperar iframe del formulario
                print("⏳ Esperando formulario RUAF...")
                
                # Esperar a que cargue la página completamente
                await page.wait_for_load_state("networkidle", timeout=30000)
                await asyncio.sleep(2)
                
                # DEBUG: Guardar screenshot para diagnosticar
                debug_screenshot = os.path.join(absolute_folder, f"debug_antes_iframe_{timestamp}.png")
                await page.screenshot(path=debug_screenshot, full_page=True)
                print(f"📸 Screenshot guardado en: {debug_screenshot}")
                
                # DEBUG: Guardar HTML para análisis
                debug_html = os.path.join(absolute_folder, f"debug_html_{timestamp}.html")
                with open(debug_html, 'w', encoding='utf-8') as f:
                    f.write(await page.content())
                print(f"📄 HTML guardado en: {debug_html}")
                
                # Intentar encontrar el formulario - primero en iframe, luego en la página principal
                frame_locator = None
                
                # Opción 1: Buscar iframe
                iframe_count = await page.locator('iframe').count()
                print(f"ℹ Número de iframes encontrados: {iframe_count}")
                
                if iframe_count > 0:
                    # Listar iframes
                    for i in range(min(iframe_count, 5)):
                        iframe_id = await page.locator('iframe').nth(i).get_attribute('id')
                        iframe_name = await page.locator('iframe').nth(i).get_attribute('name')
                        print(f"  - iframe[{i}]: id='{iframe_id}', name='{iframe_name}'")
                    
                    # Usar el primer iframe
                    frame_locator = page.frame_locator('iframe').nth(0)
                    print("✅ Usando primer iframe encontrado")
                
                # Opción 2: Si no hay iframe, buscar campos directamente en la página
                if not frame_locator:
                    print("⚠ No se encontró iframe, buscando campos en la página principal...")
                    
                    # Buscar selectores de tipo documento
                    select_count = await page.locator('select').count()
                    print(f"ℹ Selectores encontrados en página: {select_count}")
                    
                    if select_count > 0:
                        # Usar la página principal como "frame"
                        frame_locator = page
                        print("✅ Usando página principal para buscar campos")
                    else:
                        raise Exception("No se encontró formulario (ni iframe ni selectores en página principal)")
                

                # 3) Esperar campos dentro del iframe
                await frame_locator.locator('#MainContent_txbNumeroIdentificacion').wait_for(timeout=30000)

                # 4) Llenar formulario
                print("✏ Llenando formulario...")

                # Selección del tipo de documento
                # Varios selectores posibles
                select_candidates = [
                    '#ddlTiposDocumentos',
                    'select[id*="ddlTipos"]',
                    'select[id*="TiposDocumentos"]',
                    'select'
                ]
                select_selector = None
                for sel in select_candidates:
                    if await frame_locator.locator(sel).count() > 0:
                        select_selector = sel
                        break

                if not select_selector:
                    raise Exception("No se encontró el selector de tipo de documento dentro del iframe")

                await frame_locator.locator(select_selector).select_option(tipo_documento_val)
                await frame_locator.locator('#MainContent_txbNumeroIdentificacion').fill(cedula)

                await frame_locator.locator('#MainContent_datepicker').fill(fecha_formateada)
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)

                # 5) Intentos de captcha
                for intento_captcha in range(1, MAX_INTENTOS_CAPTCHA + 1):
                    print(f"🔐 Intento captcha {intento_captcha}/{MAX_INTENTOS_CAPTCHA}")

                    captcha_path = os.path.join(absolute_folder, f"captcha_{NOMBRE_SITIO}.png")

                    await frame_locator.locator('img[src*="Captcha"]').wait_for(timeout=10000)
                    await frame_locator.locator('img[src*="Captcha"]').screenshot(path=captcha_path)

                    preprocesar_captcha(captcha_path, captcha_path)
                    captcha_texto = await resolver_captcha_imagen(captcha_path)
                    try:
                        os.remove(captcha_path)
                    except FileNotFoundError:
                        pass

                    await frame_locator.locator('#MainContent_txtCaptcha').fill(captcha_texto)
                    await frame_locator.locator('#MainContent_btnVerify').click()

                    await page.wait_for_timeout(1500)

                    mensaje = (await frame_locator.locator('#MainContent_lblMessage').inner_text()).strip()
                    print(f"ℹ Mensaje captcha: {mensaje}")

                    if "Inválido" in mensaje or "Invalido" in mensaje:
                        print("❌ Captcha inválido, recargando...")
                        # Recargar captcha
                        await frame_locator.locator('img[src*="Captcha"]').click()
                        await asyncio.sleep(1)
                        continue

                    if "Válido" in mensaje or "Valido" in mensaje:
                        print("✅ Captcha válido, consultando...")

                        # Click en Consultar (esto carga el ReportViewer)
                        await frame_locator.locator('#MainContent_btnConsultar').click()

                        # Esperar toolbar/exportar a PDF
                        export_btn_selector = 'a#ctl00_MainContent_rvConsulta_ctl09_ctl04_ctl00_ButtonLink'
                        await frame_locator.locator(export_btn_selector).wait_for(timeout=25000)
                        await frame_locator.locator(export_btn_selector).click()
                        await asyncio.sleep(1)

                        # Esperar y click en opción PDF
                        pdf_link_selector = 'a.ActiveLink[title="PDF"]'
                        await frame_locator.locator(pdf_link_selector).wait_for(timeout=15000)

                        async with page.expect_download() as descarga_info:
                            await frame_locator.locator(pdf_link_selector).click()

                        descarga = await descarga_info.value

                        pdf_path = os.path.join(
                            absolute_folder,
                            f"{NOMBRE_SITIO}_{cedula}_{timestamp}.pdf"
                        )
                        await descarga.save_as(pdf_path)

                        # Convertir PDF a PNG
                        imagen_path = pdf_path.replace(".pdf", ".png")
                        pdf_a_imagen(pdf_path, imagen_path)

                        # Guardar en BD
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
                        print("✅ Consulta RUAF finalizada correctamente.")
                        return

                    # Si el mensaje no es claro, reintenta captcha
                    print("⚠ Mensaje captcha no reconocido, reintentando...")
                    await frame_locator.locator('img[src*="Captcha"]').click()
                    await asyncio.sleep(1)

                # Si se agotaron los intentos de captcha
                print("⚠ Fallo captcha en todos los intentos.")
                await navegador.close()

        except Exception as e:
            tb = traceback.format_exc()
            print(f"❌ Error intento general {intento_general}: {e}\n{tb}")

            if intento_general == MAX_INTENTOS:
                error_screenshot = os.path.join(
                    absolute_folder,
                    f"{NOMBRE_SITIO}_{cedula}_{timestamp}_error.png"
                )
                try:
                    if page:
                        await page.screenshot(path=error_screenshot, full_page=False)
                    else:
                        raise Exception("No hay page para screenshot")
                except Exception:
                    # Imagen en blanco con texto de error
                    img_blank = np.ones((400, 600, 3), dtype=np.uint8) * 255
                    cv2.putText(
                        img_blank,
                        "Error en la consulta RUAF",
                        (50, 200),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        1,
                        (0, 0, 0),
                        2,
                        cv2.LINE_AA
                    )
                    cv2.imwrite(error_screenshot, img_blank)

                if fuente_obj:
                    mensaje_err = f"No se pudo realizar la consulta en el momento. Error: {str(e)}"
                    tb_snippet = (tb or '').strip()[:1500]
                    if tb_snippet:
                        mensaje_err = mensaje_err + "\nTraceback:\n" + tb_snippet

                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=0,
                        estado="Sin validar",
                        mensaje=mensaje_err,
                        archivo=os.path.join(relative_folder, os.path.basename(error_screenshot))
                    )

        finally:
            try:
                if navegador:
                    await navegador.close()
            except Exception:
                pass

    print("⚠ RUAF: no fue posible realizar la consulta en ninguno de los intentos.")
