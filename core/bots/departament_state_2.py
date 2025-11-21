# core/bots/departament_state2.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

nombre_sitio = "departament_state2"

NO_RESULT_SEL = "div.no-result-error"
COUNT_SEL     = "div.results-count"

def _collapse(s: str) -> str:
    return " ".join((s or "").split())

async def consultar_departament_state2(consulta_id: int, nombre: str):
    navegador = None

    # 1) Obtener fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{nombre_sitio}': {e}",
            archivo=""
        )
        return

    try:
        nombre_q = (nombre or "").strip().replace(" ", "+")

        # 2) Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        # 3) Nombre de archivo
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_nombre = (nombre or "").strip().replace(" ", "_") or "consulta"
        screenshot_name = f"{nombre_sitio}_{safe_nombre}_{ts}.png"
        absolute_path = os.path.join(absolute_folder, screenshot_name)
        relative_path = os.path.join(relative_folder, screenshot_name).replace("\\", "/")

        # 4) Navegar y capturar
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()
            url = (
                "https://findit.state.gov/search"
                f"?query={nombre_q}&affiliate=dos_stategov"
            )
            await pagina.goto(url, wait_until="domcontentloaded", timeout=60000)
            # Dejar hidratar la SERP
            try:
                await pagina.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(2)

            score = 0
            mensaje = ""

            # A) Sin resultados (mensaje exacto solicitado)
            nores = pagina.locator(NO_RESULT_SEL).first
            if await nores.count() > 0 and await nores.is_visible():
                raw = await nores.inner_text()
                mensaje = _collapse(raw)
                score = 0
            else:
                # B) Con resultados: leer contador (ej. "105 results")
                count_el = pagina.locator(COUNT_SEL).first
                if await count_el.count() > 0 and await count_el.is_visible():
                    raw = await count_el.inner_text()
                    mensaje = _collapse(raw)  # usa el texto tal cual, p. ej. "105 results"
                    # si quieres validar que el número sea > 0:
                    m = re.search(r"\d+", mensaje.replace(",", ""))
                    score = 10 if (m and int(m.group(0)) > 0) else 0
                else:
                    # C) Fallback: si no vemos el mensaje de vacío ni el contador, asumimos hallazgos
                    mensaje = "se encontraron hallazgos"
                    score = 10

            # Screenshot
            await pagina.screenshot(path=absolute_path, full_page=True)
            await navegador.close()
            navegador = None

        # 5) Registrar OK en BD
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score,
            estado="Validada",
            mensaje=mensaje,
            archivo=relative_path
        )

    except Exception as e:
        # Registrar error y cerrar navegador si quedó abierto
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=str(e),
                archivo=""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
