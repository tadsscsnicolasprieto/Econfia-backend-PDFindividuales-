# core/bots/departament_justice.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

nombre_sitio = "departament_justice"

NO_RESULT_SEL   = "p.grid-container.float-left"
RESULT_COUNT_SEL = "div.results-count"

async def consultar_departament_justice(consulta_id: int, nombre: str):
    """
    Busca en DOJ por 'nombre', toma screenshot y registra:
      - Sin resultados: score=0, mensaje = texto de <p.grid-container.float-left>
      - Con resultados: score=10, mensaje = texto de <div.results-count> (ej: '2 Results')
    """
    navegador = None
    nombre_q = (nombre or "").strip().replace(" ", "+")
    safe_nombre = (nombre or "").strip().replace(" ", "_") or "consulta"

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{nombre_sitio}': {e}",
            archivo="",
        )
        return

    # Carpeta resultados/<consulta_id>
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_name = f"{nombre_sitio}_{safe_nombre}_{timestamp}.png"
    absolute_path = os.path.join(absolute_folder, screenshot_name)
    relative_path = os.path.join(relative_folder, screenshot_name).replace("\\", "/")

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()

            url = (
                "https://www.justice.gov/news"
                f"?search_api_fulltext={nombre_q}"
                "&start_date=&end_date=&sort_by=search_api_relevance"
            )
            await pagina.goto(url, timeout=60000)

            # Deja hidratar la SERP
            try:
                await pagina.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(2)

            # Lógica de resultados
            score = 0
            mensaje = ""

            # 1) Sin resultados (mensaje específico solicitado)
            nores = pagina.locator(NO_RESULT_SEL).first
            if await nores.count() > 0 and await nores.is_visible():
                txt = (await nores.inner_text()).strip()
                # colapsa espacios/nuevas líneas
                mensaje = " ".join(txt.split())
                score = 0
            else:
                # 2) Conteo de resultados
                count_el = pagina.locator(RESULT_COUNT_SEL).first
                if await count_el.count() > 0 and await count_el.is_visible():
                    txt = (await count_el.inner_text()).strip()
                    mensaje = " ".join(txt.split())  # ej: "2 Results"
                    score = 10
                else:
                    # 3) Fallback: consideramos que hay hallazgos si no vimos el mensaje de vacío
                    mensaje = "se encontraron hallazgos"
                    score = 10

            # Screenshot
            await pagina.screenshot(path=absolute_path, full_page=True)
            await navegador.close()
            navegador = None

        # Registrar OK
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score,
            estado="Validada",
            mensaje=mensaje,
            archivo=relative_path,
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=str(e),
                archivo="",
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
