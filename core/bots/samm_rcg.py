import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://samm.dsca.mil/search/rcg_search?search_api_fulltext="
NOMBRE_SITIO = "samm_rcg"
MAX_INTENTOS = 3

async def consultar_samm_rcg(consulta_id: int, nombre: str, cedula):
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
                await page.goto(URL + nombre_limpio, timeout=60000)
                await page.wait_for_timeout(5000)

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_name = f"{NOMBRE_SITIO}_{cedula}_{timestamp}.png"
                absolute_path = os.path.join(absolute_folder, screenshot_name)
                relative_path = os.path.join(relative_folder, screenshot_name)

                await page.screenshot(path=absolute_path)

                # Revisar si existe el div de "no results"
                no_results_count = await page.locator(".view-empty").count()
                if no_results_count > 0:
                    score = 0
                    mensaje = "No matching search results"
                else:
                    score = 10
                    mensaje = "Se encontraron hallazgos"

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
            if browser:
                try:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    screenshot_name = f"{NOMBRE_SITIO}_{cedula}_error_{timestamp}.png"
                    absolute_path = os.path.join(absolute_folder, screenshot_name)
                    relative_path = os.path.join(relative_folder, screenshot_name)
                    await page.screenshot(path=absolute_path)
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
                    mensaje=str(e),
                    archivo=relative_path if os.path.exists(absolute_path) else ""
                )
