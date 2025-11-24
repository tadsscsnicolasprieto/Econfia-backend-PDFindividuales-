import os
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from django.conf import settings
from asgiref.sync import sync_to_async
from core.resolver.captcha_img2 import resolver_captcha_imagen
from core.models import Resultado, Fuente

url = "https://consultasrc.registraduria.gov.co:28080/ProyectoSCCRC/"
nombre_sitio = "registro_civil"
MAX_INTENTOS = 3

async def consultar_registro_civil(cedula, consulta_id, sexo="SIN INFORMACION"):
    relative_folder = os.path.join('resultados', str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)
    
    fuente_obj = await sync_to_async(Fuente.objects.filter(nombre=nombre_sitio).first)()
    intento_global = 0

    while intento_global < MAX_INTENTOS:
        intento_global += 1
        navegador = None
        try:
            async with async_playwright() as p:
                navegador = await p.chromium.launch(headless=True)
                pagina = await navegador.new_page()
                await pagina.goto(url)

                # Selecciona consulta por documento
                await pagina.click('input[id="controlador:consultasId"]')
                await pagina.wait_for_timeout(2000)
                await pagina.select_option('select[id="searchForm:tiposBusqueda"]', 'DOCUMENTO (NUIP/NIP/Tarjeta de Identidad)')
                if sexo != "SIN INFORMACION":
                    await pagina.select_option('select#searchForm\\:sexo', sexo)

                # Llenar número de cédula
                await pagina.fill('input[id="searchForm:documento"]', cedula)
                await pagina.wait_for_timeout(1000)

                # --------------------------
                # Resolver captcha
                # --------------------------
                captcha = pagina.locator("img[src*='kaptcha.jpg']")
                await captcha.wait_for()
                captcha_src = await captcha.get_attribute("src")
                captcha_url = f"https://consultasrc.registraduria.gov.co:28080{captcha_src}"
                response = await pagina.request.get(captcha_url)
                image_bytes = await response.body()
                captcha_path = os.path.join(absolute_folder, f"captcha_{nombre_sitio}_{cedula}.png")
                with open(captcha_path, "wb") as f:
                    f.write(image_bytes)

                captcha_resultado = await resolver_captcha_imagen(captcha_path)
                await pagina.fill('input[id="searchForm:inCaptcha"]', captcha_resultado)
                await pagina.click('input[id="searchForm:busquedaRCX"]')
                await pagina.wait_for_timeout(5000)

                # --------------------------
                # Revisar mensajes de error de captcha
                # --------------------------
                div_captcha_error = pagina.locator("div[id='searchForm:j_idt76'] ul li")
                mensaje_error = ""
                if await div_captcha_error.count() > 0:
                    texto_error = (await div_captcha_error.inner_text()).strip()
                    if "no corresponde con la imagen de verificación" in texto_error.lower():
                        print(f"[Intento {intento_global}] Captcha incorrecto, reintentando...")
                        await navegador.close()
                        continue  # vuelve a reintentar el captcha
                    elif "no se han encontrado registros en la base de datos" in texto_error.lower():
                        mensaje_error = texto_error
                        score = 0
                    else:
                        mensaje_error = texto_error
                        score = 0
                else:
                    mensaje_error = "La persona se encuentra registrada"
                    score = 0

                # Tomar screenshot
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
                absolute_path = os.path.join(absolute_folder, screenshot_name)
                relative_path = os.path.join(relative_folder, screenshot_name)
                await pagina.screenshot(path=absolute_path, full_page=True)

                # Guardar en BD
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validado",
                    mensaje=mensaje_error,
                    archivo=relative_path
                )

                await navegador.close()
                return  # salió bien, termina la función

        except Exception as e:
            error_screenshot = ""
            if navegador:
                try:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    error_screenshot = os.path.join(absolute_folder, f"{nombre_sitio}_{cedula}_{timestamp}_error.png")
                    await pagina.screenshot(path=error_screenshot, full_page=True)
                    await navegador.close()
                except:
                    pass
            print(f"[Intento {intento_global}] Error: {e}")

            if intento_global == MAX_INTENTOS:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin validar",
                    mensaje=f"Error tras {MAX_INTENTOS} intentos: {str(e)}",
                    archivo=error_screenshot if error_screenshot else ""
                )
