import os
import asyncio
from datetime import datetime
from core.models import Resultado, Fuente 
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from django.conf import settings
from core.resolver.captcha_img2 import resolver_captcha_imagen_sync

url = "https://web.sispro.gov.co/THS/Cliente/ConsultasPublicas/ConsultaPublicaDeTHxIdentificacion.aspx"
nombre_sitio = "rethus"

TIPO_DOC_MAP = {
    'CC': 'CC',
    'TI': 'TI',
    'CE': 'CE',
    'PT': 'PT',
}

async def consultar_rethus(consulta_id, cedula, tipo_doc, nombre, apellido):
    MAX_INTENTOS = 3
    primer_nombre = nombre.strip().split()[0] if nombre else ""
    primer_apellido = apellido.strip().split()[0] if apellido else ""
    
    tipo_doc_val = TIPO_DOC_MAP.get(tipo_doc.upper())
    if not tipo_doc_val:
        raise ValueError(f"Tipo de documento no válido: {tipo_doc}")

    relative_folder = os.path.join('resultados', str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    intento_global = 0

    while intento_global < MAX_INTENTOS:
        intento_global += 1
        print(f"\n=== Intento {intento_global} / {MAX_INTENTOS} ===")

        async with async_playwright() as p:
            navegador = None
            try:
                navegador = await p.chromium.launch(headless=True)
                pagina = await navegador.new_page()
                await pagina.goto(url, timeout=30000)
                await pagina.wait_for_selector('select[id="ctl00_cntContenido_ddlTipoIdentificacion"]', timeout=15000)

                # Llenar formulario
                await pagina.select_option('select[id="ctl00_cntContenido_ddlTipoIdentificacion"]', tipo_doc_val)
                await pagina.fill('input[id="ctl00_cntContenido_txtNumeroIdentificacion"]', cedula)
                await pagina.fill('input[id="ctl00_cntContenido_txtPrimerNombre"]', primer_nombre)
                await pagina.fill('input[id="ctl00_cntContenido_txtPrimerApellido"]', primer_apellido)
                await pagina.wait_for_timeout(800)

                # Captura captcha y resolución
                captcha_path = os.path.join(absolute_folder, f"captcha_{nombre_sitio}.png")
                await pagina.wait_for_selector('img[id="imgCaptcha"]', timeout=15000)
                await pagina.screenshot(path=captcha_path)
                captcha_texto = await asyncio.to_thread(resolver_captcha_imagen_sync, captcha_path)
                print(f"[Intento {intento_global}] Captcha resuelto: {captcha_texto}")
                try: os.remove(captcha_path)
                except: pass
                await pagina.fill('input[id="ctl00_cntContenido_txtCatpchaConfirmation"]', captcha_texto)

                # Click en verificar
                try:
                    async with pagina.expect_event("dialog", timeout=5000) as dialog_info:
                        await pagina.evaluate("document.getElementById('ctl00_cntContenido_btnVerificarIdentificacion').click()")
                    dialog = await dialog_info.value
                    print(f"[Intento {intento_global}] Alerta detectada: {dialog.message}")
                    await dialog.accept()
                    await navegador.close()
                    continue
                except PlaywrightTimeoutError:
                    pass

                # Esperar div resultado
                await pagina.wait_for_selector('#ctl00_cntContenido_pnlResultado', timeout=20000)

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                # 1) Pantallazo del contenedor
                screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
                absolute_path = os.path.join(absolute_folder, screenshot_name)
                relative_path = os.path.join(relative_folder, screenshot_name)
                await pagina.locator('#ctl00_cntContenido_pnlResultado').screenshot(path=absolute_path)
                print(f"[Intento {intento_global}] Captura del contenedor guardada en: {absolute_path}")

                # 2) Pantallazo del viewport completo
                viewport_screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}_full.png"
                viewport_absolute = os.path.join(absolute_folder, viewport_screenshot_name)
                await pagina.screenshot(path=viewport_absolute, full_page=True)
                print(f"[Intento {intento_global}] Captura completa guardada en: {viewport_absolute}")

                # Revisar mensaje
                span_selector = 'span[id="ctl00_cntContenido_LblResultado"]'
                existe_span = await pagina.locator(span_selector).count()
                if existe_span:
                    mensaje = (await pagina.locator(span_selector).inner_text()).strip()
                    score = 0
                else:
                    mensaje = "Se encontró un registro en ReTHUS"
                    score = 10

                # Guardar en BD (archivo principal: contenedor)
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validado",
                    mensaje=mensaje,
                    archivo=relative_path
                )

                await navegador.close()
                return

            except Exception as e:
                error_screenshot = ""
                if navegador:
                    try:
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        error_screenshot = os.path.join(absolute_folder, f"{nombre_sitio}_{cedula}_{timestamp}_error.png")
                        await pagina.screenshot(path=error_screenshot, full_page=True)
                    except:
                        pass
                    await navegador.close()

                print(f"[Intento {intento_global}] Error: {e}")

                if intento_global == MAX_INTENTOS:
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=0,
                        estado="Sin validar",
                        mensaje="Ocurrió un problema al obtener la información de la fuente",
                        archivo=error_screenshot if error_screenshot else ""
                    )
