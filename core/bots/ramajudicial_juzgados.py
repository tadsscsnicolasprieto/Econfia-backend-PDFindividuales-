import os
from datetime import datetime
from django.conf import settings
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente  # ajusta según tu proyecto
from PIL import Image, ImageDraw, ImageFont

url = "https://consultaprocesos.ramajudicial.gov.co/Procesos/JuezClaseProceso"
nombre_sitio = "rama_judicial"

# --- Diccionario de mapeo entre texto en la web y nombre en BD ---
city_mapping = {
    "ARMENIA-CALARCA": "juzgado_armenia_calarca",
    "BARRANQUILLA": "juzgado_barranquilla",
    "BOGOTA": "juzgado_bogota",
    "BUCARAMANGA": "juzgado_bucaramanga",
    "BUGA": "juzgado_buga",
    "CALI": "juzgado_cali",
    "CARTAGENA": "juzgado_cartagena",
    "FLORENCIA": "juzgado_florencia",
    "IBAGUE": "juzgado_ibague",
    "LA DORADA": "juzgado_la_dorada",
    "MANIZALES": "juzgado_manizales",
    "MEDELLIN": "juzgado_medellin",
    "MONTERIA": "juzgado_monteria",
    "NEIVA": "juzgado_neiva",
    "PALMIRA": "juzgado_palmira",
    "PASTO": "juzgado_pasto",
    "PEREIRA": "juzgado_pereira",
    "POPAYAN": "juzgado_popayan",
    "QUIBDO": "juzgado_quibdo",
    "SANTA MARTA": "juzgado_santa_marta",
}


def agregar_marca_agua(img_path, ciudad, url):
    """Agrega marca de agua con ciudad, hora y URL"""
    img = Image.open(img_path).convert("RGBA")
    txt_layer = Image.new("RGBA", img.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(txt_layer)

    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except:
        font = ImageFont.load_default()

    marca = (
        f"Juzgado: {ciudad}\n"
        f"Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"URL: {url}"
    )

    # Posicionar abajo a la izquierda
    x, y = 20, img.size[1] - 100
    draw.multiline_text((x, y), marca, font=font, fill=(255, 0, 0, 180))

    # Combinar capas y sobrescribir
    watermarked = Image.alpha_composite(img, txt_layer)
    watermarked = watermarked.convert("RGB")
    watermarked.save(img_path)


async def consultar_ramajudicial_juzgados(consulta_id, cedula):
    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            contexto = await navegador.new_context()
            pagina = await contexto.new_page()
            await pagina.goto(url)

            campo_ciudad = pagina.locator('#input-62')
            await campo_ciudad.click()
            await pagina.wait_for_selector('div[role="listbox"]')

            opciones = pagina.locator('div[role="listbox"] .v-list-item')
            total = await opciones.count()

            for i in range(total):
                try:
                    await campo_ciudad.click()
                    await pagina.wait_for_selector('div[role="listbox"]')
                    opciones = pagina.locator('div[role="listbox"] .v-list-item')

                    ciudad = (await opciones.nth(i).inner_text()).strip()
                    print(f"Seleccionando ciudad: {ciudad}")

                    fuente_nombre = city_mapping.get(ciudad)
                    if not fuente_nombre:
                        print(f"⚠️ Ciudad {ciudad} no está mapeada, se omite.")
                        continue

                    await opciones.nth(i).click()
                    boton_juzgado = pagina.locator("button:has-text('Ir a Juzgado')")

                    async with contexto.expect_page() as nueva_pagina_info:
                        await boton_juzgado.click()

                    pagina_juzgado = await nueva_pagina_info.value
                    print(f"Abierta página del juzgado para {ciudad}")

                    try:
                        await pagina_juzgado.wait_for_selector("select[name='cbadju']", timeout=10000)
                        await pagina_juzgado.select_option("select[name='cbadju']", "3")
                        await pagina_juzgado.fill('input[name="norad"]', cedula)
                        await pagina_juzgado.click('input[name="Buscar"]')

                        await pagina_juzgado.wait_for_selector("table[border='3']", timeout=10000)

                        relative_folder = os.path.join("resultados", str(consulta_id))
                        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
                        os.makedirs(absolute_folder, exist_ok=True)

                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        screenshot_name = f"screenshot_{fuente_nombre}_{cedula}_{timestamp}.png"
                        absolute_path = os.path.join(absolute_folder, screenshot_name)
                        relative_path = os.path.join(relative_folder, screenshot_name)

                        contenedor = await pagina_juzgado.locator("body").bounding_box()
                        await pagina_juzgado.screenshot(path=absolute_path, clip=contenedor)

                        # Agregar marca de agua
                        agregar_marca_agua(absolute_path, ciudad, pagina_juzgado.url)
                        print(f"📸 Captura con marca de agua guardada para {ciudad}: {relative_path}")

                        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=fuente_nombre)
                        await sync_to_async(Resultado.objects.create)(
                            consulta_id=consulta_id,
                            fuente=fuente_obj,
                            score=0,
                            estado="Validado",
                            mensaje=f"Consulta realizada en juzgado de {ciudad}",
                            archivo=relative_path
                        )

                    except PlaywrightTimeoutError:
                        print(f"⚠️ El juzgado de {ciudad} no cargó correctamente")
                    except Exception as inner_err:
                        print(f"⚠️ Error procesando juzgado {ciudad}: {inner_err}")
                    finally:
                        await pagina_juzgado.close()

                except Exception as loop_err:
                    print(f"⚠️ Error general con ciudad en índice {i}: {loop_err}")
                    continue

            await navegador.close()

    except Exception as e:
        print(f"❌ Error global en el bot: {e}")
