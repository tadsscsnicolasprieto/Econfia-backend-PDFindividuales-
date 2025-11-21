import os
import re
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente
from PIL import Image

PAGE_URL = "https://www.state.gov/foreign-terrorist-organizations/"
NOMBRE_SITIO = "royal_canadian_mounted_police"  # Debe coincidir con el nombre en la BD de la fuente


def unir_imagenes_vertical(imagenes, salida):
    """Une varias imágenes en una sola verticalmente."""
    imgs = [Image.open(img) for img in imagenes]
    # Redimensionar todas a la misma anchura
    min_width = min(i.width for i in imgs)
    imgs = [i.resize((min_width, int(i.height * min_width / i.width))) for i in imgs]

    total_height = sum(i.height for i in imgs)
    result = Image.new("RGB", (min_width, total_height))

    y_offset = 0
    for im in imgs:
        result.paste(im, (0, y_offset))
        y_offset += im.height

    result.save(salida)


async def consultar_plantilla(consulta_id, cedula, nombre: str):
    # 📂 Crear carpeta de resultados
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    # 📝 Nombre del pantallazo final
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_name = f"{NOMBRE_SITIO}_{consulta_id}_{cedula}_{timestamp}.png"

    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name)

    # Fuente en BD
    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)

    intentos = 0
    exito = False
    last_exception = None

    while intentos < 3 and not exito:
        intentos += 1
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context()
                page = await context.new_page()

                print(f"[Intento {intentos}] Ingresando a {NOMBRE_SITIO}...")

                await page.goto(PAGE_URL, timeout=60000)
                await page.wait_for_selector("body")

                body_text = await page.inner_text("body")

                # 🔎 Contar coincidencias
                matches = re.findall(nombre, body_text, flags=re.IGNORECASE)
                total_matches = len(matches)

                if total_matches > 0:
                    score = 10
                    mensaje = f"Se encontró el nombre '{nombre}' en la página ({total_matches} coincidencia(s))"
                else:
                    score = 0
                    mensaje = f"No se encontró el nombre '{nombre}'"

                capturas = []

                # 📸 1) Captura del header
                header = await page.query_selector("header")
                if header:
                    temp_png1 = os.path.join(absolute_folder, "header.png")
                    color = "#188038" if total_matches > 0 else "#d93025"
                    await page.evaluate(f"""
                    var container = document.createElement('div');
                    container.style.position = 'absolute';
                    container.style.top = '0';
                    container.style.right = '0';
                    container.style.width = '500px';
                    container.style.height = '60px';
                    container.style.backgroundColor = '#f1f3f4';
                    container.style.border = '1px solid #ccc';
                    container.style.display = 'flex';
                    container.style.alignItems = 'center';
                    container.style.justifyContent = 'space-between';
                    container.style.padding = '0 20px';
                    container.style.fontFamily = 'Arial, sans-serif';
                    container.style.fontSize = '16px';
                    container.style.zIndex = 9999;
                    container.style.boxShadow = '0px 4px 8px rgba(0,0,0,0.3)';
                    container.style.borderRadius = '4px';

                    var text = document.createElement('span');
                    text.textContent = "Buscar: '{nombre}'";
                    text.style.flex = '1';
                    text.style.color = '#202124';

                    var count = document.createElement('span');
                    count.textContent = "{total_matches} coincidencia(s)";
                    count.style.marginLeft = '15px';
                    count.style.color = '{color}';
                    count.style.fontWeight = 'bold';

                    container.appendChild(text);
                    container.appendChild(count);

                    var target = document.querySelector("header");
                    if (target) target.prepend(container);
                """)

                    await header.screenshot(path=temp_png1)
                    capturas.append(temp_png1)

                # 📸 2) Captura del contenedor azul
                contenedores = await page.query_selector_all(".entry-content")
                contenedor2 = contenedores[0]
                # if len(contenedores) >= 34:   # índice 34 → el número 34 (empezando en 1)
                #     contenedor2 = contenedores[33]  # porque en Python los índices empiezan en 0
                # else:
                #     contenedor2 = None
                
                if contenedor2:
                    # 
                    # await page.wait_for_selector(".fr-col-12:nth-of-type(34)", state="visible")
                    # await page.evaluate("""
                    #     return Promise.all(
                    #         Array.from(document.querySelectorAll('.fr-col-12:nth-of-type(34) img'))
                    #             .map(img => img.complete ? Promise.resolve() : new Promise(r => img.onload = r))
                    #     )
                    # """)
                    # Insertamos el cuadro de búsqueda aquí
                    

                    temp_png2 = os.path.join(absolute_folder, "contenedor_azul.png")
                    await contenedor2.screenshot(path=temp_png2)
                    capturas.append(temp_png2)

                # 👉 Unir ambas capturas en una sola vertical
                if capturas:
                    unir_imagenes_vertical(capturas, absolute_png)

                    # 🗑️ Eliminar temporales
                    for c in capturas:
                        os.remove(c)

                await browser.close()

            # Guardar resultado en la BD
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
                    await page.screenshot(path=absolute_png, full_page=True)
            except Exception as ss_err:
                print(f"No se pudo tomar pantallazo del error: {ss_err}")
            finally:
                try:
                    await browser.close()
                except:
                    pass

    # ❌ Si falló tras 3 intentos
    if not exito:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje=f"Ocurrió un problema al consultar la fuente: {last_exception}",
            archivo=relative_png if os.path.exists(absolute_png) else ""
        )