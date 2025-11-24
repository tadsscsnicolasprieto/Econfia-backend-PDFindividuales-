import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente
from core.resolver.captcha_img2 import resolver_captcha_imagen  # async

url = "https://mat.inpec.gov.co/consultasWeb/faces/index.xhtml"
nombre_sitio = "inpec"


async def consultar_inpec(consulta_id: int, cedula: str, apellidos: str):
    navegador = None
    fuente_obj = None

    # Buscar la fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{nombre_sitio}': {e}",
            archivo=""
        )
        return

    # Carpeta de resultados
    relative_folder = os.path.join('resultados', str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
    absolute_path = os.path.join(absolute_folder, screenshot_name)
    relative_path = os.path.join(relative_folder, screenshot_name)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()

            # Intentar cargar la página
            try:
                await pagina.goto(url, timeout=15000)  # 15s timeout
            except Exception:
                # Tomar pantallazo si la página no carga
                await pagina.screenshot(path=absolute_path, full_page=True)
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin Validar",
                    mensaje="La página no cargó, puede estar caída",
                    archivo=relative_path
                )
                return

            # === CONTENIDO NORMAL DE LA FUNCIÓN ===
            # Esperar formulario
            await pagina.wait_for_selector('form[id="solicitudTurno"]', timeout=10000)

            # Llenar datos
            primer_apellido = (apellidos or "").strip().split()[0].upper()
            await pagina.fill('input[id="solicitudTurno:identificacion"]', str(cedula))
            await pagina.fill('input[id="solicitudTurno:apellido"]', primer_apellido)

            # Capturar captcha
            captcha_locator = pagina.locator("img[id='solicitudTurno:im']")
            await captcha_locator.wait_for(timeout=5000)
            captcha_name = f"captcha_{nombre_sitio}_{cedula}_{timestamp}.png"
            captcha_abs_path = os.path.join(absolute_folder, captcha_name)
            await captcha_locator.screenshot(path=captcha_abs_path)

            captcha_resultado = await resolver_captcha_imagen(captcha_abs_path)
            await pagina.fill('input[id="solicitudTurno:catpcha"]', captcha_resultado)

            # Click botón "Consultar" robusto
            btn = pagina.locator('button:has-text("Consultar")').first
            await btn.scroll_into_view_if_needed()
            await btn.click()

            # Esperar tabla o mensaje
            await pagina.wait_for_selector("#solicitudTurno\\:tablainterno, #solicitudTurno\\:msg", timeout=10000)
            await pagina.wait_for_timeout(1200)

            # Determinar mensaje y score
            mensaje_final = "No se encontraron registros con los datos suministrados"
            score_final = 0
            msg_loc = pagina.locator("#solicitudTurno\\:msg li span").first
            if await msg_loc.count() > 0:
                texto = (await msg_loc.text_content() or "").strip().lower()
                if "no se encontraron registros" not in texto:
                    mensaje_final = "Se encontraron registros con los datos suministrados"
                    score_final = 10

            # Pantallazo final
            await pagina.screenshot(path=absolute_path, full_page=True)

            # Guardar resultado en BD
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score_final,
                estado="Validada",
                mensaje=mensaje_final,
                archivo=relative_path
            )

            await navegador.close()
            navegador = None

    except Exception as e:
        # Inicializar rutas para evitar errores si ocurre excepción antes de su definición
        absolute_path = locals().get('absolute_path', 'error_screenshot.png')
        relative_path = locals().get('relative_path', '')
        # Capturar pantallazo en caso de error
        try:
            if navegador is not None:
                await pagina.screenshot(path=absolute_path, full_page=True)
                await navegador.close()
        except:
            pass
        # Guardar error en BD
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje="La fuente no se encuentra en funcionamiento en este momento, por favor intente más tarde",
            archivo=relative_path if os.path.exists(absolute_path) else ""
        )
