import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente
from PIL import Image

nombre_sitio = "rues"
MAX_INTENTOS = 3

async def consultar_rues(cedula, consulta_id):
    url = f"https://www.rues.org.co/buscar/RM/{cedula}"
    
    # Carpeta de resultados
    relative_folder = os.path.join('resultados', str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshots_paths = []
    hallazgos_count = 0
    mensaje_acumulado = ""

    fuente_obj = await sync_to_async(Fuente.objects.filter(nombre=nombre_sitio).first)()

    for intento in range(1, MAX_INTENTOS + 1):
        try:
            async with async_playwright() as p:
                navegador = await p.chromium.launch(headless=True)
                pagina = await navegador.new_page()
                await pagina.goto(url)

                # 1) Obtener todas las opciones del select
                select = pagina.locator("select.select-type-index").first
                opciones = await select.locator("option").all_text_contents()

                for opcion in opciones[:-1]:  # excluye la última
                    await select.select_option(label=opcion)
                    await pagina.wait_for_timeout(500)
                    
                    btn_buscar = pagina.locator("button.btn-busqueda:visible")
                    await btn_buscar.scroll_into_view_if_needed()
                    await btn_buscar.click()
                    await pagina.wait_for_timeout(3000)

                    # Verificar si hay mensaje de "No se encontraron resultados"
                    no_result = await pagina.locator(
                        "div.alert.alert-info:has-text('No se encontraron resultados')"
                    ).count()

                    if no_result:
                        mensaje_acumulado += f"No se encontraron hallazgos en {opcion}. "
                    else:
                        mensaje_acumulado += f"Se encontraron hallazgos en {opcion}. "
                        hallazgos_count += 1

                    # Tomar pantallazo individual
                    temp_name = f"{nombre_sitio}_{cedula}_{opcion}_{timestamp}.png"
                    temp_path = os.path.join(absolute_folder, temp_name)
                    await pagina.screenshot(path=temp_path)
                    screenshots_paths.append(temp_path)

                # Combinar pantallazos en una sola imagen
                images = [Image.open(p) for p in screenshots_paths]
                widths, heights = zip(*(i.size for i in images))
                col_count = 3
                max_width = max(widths)
                total_rows = (len(images) + col_count - 1) // col_count
                max_height = max(heights)
                combined_img = Image.new(
                    "RGB", (max_width * col_count, max_height * total_rows), (255, 255, 255)
                )
                for idx, img in enumerate(images):
                    row = idx // col_count
                    col = idx % col_count
                    combined_img.paste(img, (col * max_width, row * max_height))

                screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}_combined.png"
                absolute_path = os.path.join(absolute_folder, screenshot_name)
                relative_path = os.path.join(relative_folder, screenshot_name)
                combined_img.save(absolute_path)

                await navegador.close()

                # Calcular score
                total_opts = len(opciones) - 1  # porque excluimos la última
                if hallazgos_count == total_opts:
                    score = 10
                elif hallazgos_count >= total_opts * 0.6:
                    score = 6
                elif hallazgos_count >= total_opts * 0.3:
                    score = 2
                else:
                    score = 0

                # Guardar en la base de datos
                if fuente_obj:
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=score,
                        estado="Validado",
                        mensaje=mensaje_acumulado.strip(),
                        archivo=relative_path
                    )
                break  # si tuvo éxito, salimos del bucle de intentos

        except Exception as e:
            # Guardar pantallazo del error
            error_name = f"{nombre_sitio}_{cedula}_{timestamp}_error.png"
            error_path = os.path.join(absolute_folder, error_name)
            if 'pagina' in locals():
                try:
                    await pagina.screenshot(path=error_path)
                except Exception:
                    error_path = ""
            if intento == MAX_INTENTOS:
                if fuente_obj:
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=0,
                        estado="Sin validar",
                        mensaje=f"Fallo después de {MAX_INTENTOS} intentos: {str(e)}",
                        archivo=error_path
                    )
