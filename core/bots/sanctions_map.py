import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente
from PIL import Image 
from urllib.parse import quote 
NOMBRE_SITIO = "sanctions_map"
MAX_INTENTOS = 3

async def consultar_sanctions_map(consulta_id: int, nombre: str):
    nombre_limpio = (nombre or "").strip()
    if not nombre_limpio:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje="El nombre llegó vacío",
            archivo=""
        )
        return

    # URL-encode del nombre
    nombre_encoded = quote(nombre_limpio)
    if not nombre_limpio:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje="El nombre llegó vacío",
            archivo=""
        )
        return

    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    intentos = 0
    while intentos < MAX_INTENTOS:
        intentos += 1
        browser = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()

                url = (
                    f"https://www.sanctionsmap.eu/#/main?"
                    f"search=%7B%22value%22:%22{nombre_encoded}%22,"
                    f"%22searchType%22:%7B%22id%22:1,%22title%22:"
                    f"%22regimes,%20persons,%20entities%22%7D%7D"
                )                
                await page.goto(url, timeout=60000)
                await page.wait_for_timeout(5000)

                # Capturar los contenedores por separado
                contenedores = ["#search-block-wrapper", "main"]
                images_paths = []
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

                for idx, selector in enumerate(contenedores):
                    try:
                        element = page.locator(selector).first
                        img_path = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{nombre_limpio}_{idx}_{timestamp}.png")
                        await element.screenshot(path=img_path)
                        images_paths.append(img_path)
                    except Exception as e:
                        print(f"No se pudo capturar {selector}: {e}")

                # Combinar verticalmente
                pil_images = [Image.open(p) for p in images_paths]
                total_height = sum(img.height for img in pil_images)
                max_width = max(img.width for img in pil_images)
                combined = Image.new("RGB", (max_width, total_height), color=(255,255,255))
                
                y_offset = 0
                for img in pil_images:
                    combined.paste(img, (0, y_offset))
                    y_offset += img.height

                combined_path = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{nombre_limpio}_{timestamp}_combined.png")
                combined.save(combined_path)
                relative_path = os.path.join(relative_folder, os.path.basename(combined_path))

                # Revisar si aparece el div de "no results"
                no_results = await page.locator(".not-found-message").count()
                if no_results > 0:
                    score = 0
                    mensaje = "No results found"
                else:
                    score = 10
                    mensaje = "Se encontró un resultado"

                fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validado",
                    mensaje=mensaje,
                    archivo=relative_path
                )

                await browser.close()
                return

        except Exception as e:
            # Inicializar variables para evitar errores si ocurre excepción antes de su definición
            combined_path = locals().get('combined_path', 'error_screenshot.png')
            relative_path = locals().get('relative_path', '')
            if browser:
                try:
                    await page.screenshot(path=combined_path)
                    await browser.close()
                except:
                    pass

            if intentos >= MAX_INTENTOS:
                fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin validar",
                    mensaje="Ocurrió un problema al obtener la información de la fuente",
                    archivo=relative_path if os.path.exists(combined_path) else ""
                )
