import os
import asyncio
from datetime import datetime
from asgiref.sync import sync_to_async
from django.conf import settings
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "state_designation_cartels"
URL_SEARCH = "https://findit.state.gov/search?affiliate=dos_stategov&query="

GOTO_TIMEOUT = 45000
WAIT_SELECTOR = 15000


async def consultar_state_designation_cartels_pdf(consulta_id: int, nombre: str, apellido: str, cedula: str):
    full_name = f"{nombre} {apellido}".strip()
    query = full_name.replace(" ", "+")
    url = URL_SEARCH + query

    # Ruta de resultados
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    screenshot_path = os.path.join(absolute_folder, "state_designations.png")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 2000}
        )
        page = await context.new_page()

        # 1. Ir a la URL
        await page.goto(url, timeout=GOTO_TIMEOUT)

        # 2. Esperar que cargue sección principal (DOM de resultados)
        try:
            await page.wait_for_selector("div.gsc-resultsbox-visible", timeout=WAIT_SELECTOR)
        except:
            pass  # Si no aparece igual tomamos captura

        # 3. Hacer captura TOTAL del scroll
        await page.screenshot(
            path=screenshot_path,
            full_page=True
        )

        # 4. Obtener cantidad de resultados encontrados
        try:
            items = await page.query_selector_all(".gsc-result")
            count = len(items)
        except:
            count = 0

        await browser.close()

    # Determinar resultado
    if count >= 1:
        mensaje = f"Se encontraron {count} coincidencias para el nombre '{nombre}'."
        score = 5
    else:
        mensaje = f"No se encontraron coincidencias para '{nombre}'."
        score = 1

    estado = "validado"

    # GUARDAR EN BD
    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)

    archivo_relativo = os.path.join(relative_folder, "state_designations.png")

    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=score,
        estado=estado,
        archivo=archivo_relativo,
        mensaje=mensaje
    )

    return {
        "estado": estado,
        "mensaje": mensaje,
        "archivo": archivo_relativo
    }
