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

                # Ajustar viewport a tamaño carta (aprox 850x1100 px) y aumentar legibilidad
                carta_width, carta_height = 850, 1100
                await page.set_viewport_size({"width": carta_width, "height": carta_height})
                # Aumentar el tamaño visual del contenido para que se vea grande en la captura
                try:
                    await page.evaluate("document.body.style.zoom='1.25'")
                except Exception:
                    pass

                # Capturar la página completa
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                img_path = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{nombre_limpio}_{timestamp}_full.png")
                await page.screenshot(path=img_path, full_page=True)

                # Componer en lienzo blanco tamaño carta con márgenes (salida única)
                pil_img = Image.open(img_path)
                canvas = Image.new("RGB", (carta_width, carta_height), color=(255, 255, 255))
                # Escalar manteniendo proporción para encajar dentro de carta con margen
                margin = 30
                max_w = carta_width - 2 * margin
                max_h = carta_height - 2 * margin
                scale = min(max_w / pil_img.width, max_h / pil_img.height)
                new_w = max(1, int(pil_img.width * scale))
                new_h = max(1, int(pil_img.height * scale))
                resized = pil_img.resize((new_w, new_h), Image.LANCZOS)
                # Centrar
                x = (carta_width - new_w) // 2
                y = (carta_height - new_h) // 2
                canvas.paste(resized, (x, y))
                # Guardar como carta final
                final_path = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{nombre_limpio}_{timestamp}_carta.png")
                canvas.save(final_path)
                img_path = final_path
                relative_path = os.path.join(relative_folder, os.path.basename(img_path))

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
            img_path = locals().get('img_path', 'error_screenshot.png')
            relative_path = locals().get('relative_path', '')
            if browser:
                try:
                    await page.screenshot(path=img_path)
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
                    archivo=relative_path if os.path.exists(img_path) else ""
                )
