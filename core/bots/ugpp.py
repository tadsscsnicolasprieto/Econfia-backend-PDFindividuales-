import os
from datetime import datetime
from django.conf import settings
from core.resolver.captcha_v2 import resolver_captcha_v2
from playwright.async_api import async_playwright
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

url = "https://www.ugpp.gov.co/consulte-con-cedula"
nombre_sitio = "ugpp"

TIPO_DOC_MAP = {
    "CC": "CC", "TI": "TI", "CE": "CE", "PEP": "PEP", "PAS": "PAS", "RC": "RC",
    "DNI": "DNI", "PR": "PR", "PPT": "PPT", "SCP": "SCP", "NIS": "NIS", "RUI": "RUI",
    "NCS": "NCS", "NIT": "NIT", "PEPFF": "PEPFF", "CUR": "CUR"
}


async def consultar_ugpp(cedula, tipo_doc, consulta_id):
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    intento = 0
    error_final = None
    screenshot_path = ""

    while intento < 3:  # máximo 3 intentos
        try:
            tipo_doc_val = TIPO_DOC_MAP.get(tipo_doc.upper())
            async with async_playwright() as p:
                navegador = await p.chromium.launch(headless=True)
                pagina = await navegador.new_page()
                await pagina.goto(url)

                # llenar formulario
                await pagina.select_option('select[id="TipoDocumento"]', tipo_doc_val)
                await pagina.fill('#noidentificacion', cedula)
                await pagina.press('#noidentificacion', "Tab")

                await pagina.click('input[id="acepto"]')
                await pagina.click('input[id="aceptoDos"]')

                # resolver captcha
                sitekey = await pagina.locator('.g-recaptcha').get_attribute('data-sitekey')
                token = resolver_captcha_v2(pagina.url, sitekey)
                await pagina.evaluate(f"""
                    document.getElementById('g-recaptcha-response').innerHTML = '{token}';
                """)

                await pagina.click('button[type="submit"]')
                await pagina.wait_for_timeout(4000)

                # mensaje por defecto
                mensaje_final = "Se encontró una coincidencia"
                score = 10

                try:
                    # validar si existe div con mensaje de "no resultados"
                    if await pagina.locator("#consultaVacia").count() > 0:
                        texto_div = await pagina.inner_text("#consultaVacia")
                        if "no arrojan resultados" in texto_div.lower():
                            mensaje_final = "Los datos suministrados no arrojan resultados en la consulta."
                            score = 0
                except:
                    pass  # si no existe el div, seguimos con coincidencia

                # guardar pantallazo
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
                absolute_path = os.path.join(absolute_folder, screenshot_name)
                relative_path = os.path.join(relative_folder, screenshot_name)

                # Obtener el elemento
                consulta_vacia = pagina.locator("#consultaVacia").first
                if await consulta_vacia.count() > 0:
                    # Centrar el div en el viewport
                    await pagina.evaluate("""
                        element => {
                            const rect = element.getBoundingClientRect();
                            const viewportHeight = window.innerHeight;
                            const viewportWidth = window.innerWidth;
                            const scrollY = window.scrollY + rect.top - (viewportHeight/2 - rect.height/2);
                            const scrollX = window.scrollX + rect.left - (viewportWidth/2 - rect.width/2);
                            window.scrollTo(scrollX, scrollY);
                        }
                    """, await consulta_vacia.element_handle())
                    
                # Tomar el pantallazo completo del viewport
                await pagina.screenshot(path=absolute_path, full_page=False)
                screenshot_path = relative_path
                # guardar en BD
                fuente_obj = await sync_to_async(Fuente.objects.filter(nombre=nombre_sitio).first)()
                if fuente_obj:
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=score,
                        estado="Validado",
                        mensaje=mensaje_final,
                        archivo=screenshot_path
                    )
                return  # éxito → salimos
        except Exception as e:
            intento += 1
            error_final = str(e)

            # siempre guardar pantallazo del intento fallido
            if screenshot_path == "":
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}_error.png"
                absolute_path = os.path.join(absolute_folder, screenshot_name)
                relative_path = os.path.join(relative_folder, screenshot_name)
                screenshot_path = relative_path
                try:
                    await pagina.screenshot(path=absolute_path)
                except:
                    pass

    # si falló los 3 intentos → guardar error en BD
    fuente_obj = await sync_to_async(Fuente.objects.filter(nombre=nombre_sitio).first)()
    if fuente_obj:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="error",
            mensaje=error_final or "Error desconocido",
            archivo=screenshot_path if screenshot_path else ""
        )
