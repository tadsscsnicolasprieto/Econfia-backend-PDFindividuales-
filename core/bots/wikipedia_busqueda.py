import os
import re
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "wikipedia_busqueda"
URL = "https://es.wikipedia.org/wiki/Wikipedia:Portada"

SEL_INPUT_SEARCH = "#searchInput"
SEL_RESULTS_ANY = "div.mw-search-results, #mw-content-text"
SEL_RESULTS_UL = "ul.mw-search-results"

NAV_TIMEOUT_MS = 120000
WAIT_AFTER_NAV_MS = 2500
WAIT_RESULTS_MS = 15000


async def _guardar_resultado(consulta_id, fuente_obj, estado, mensaje, rel_path):
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=0,
        estado=estado,
        mensaje=mensaje,
        archivo=rel_path,
    )


async def consultar_wikipedia_busqueda(consulta_id: int, nombre: str, apellido: str):

    max_intentos = 3
    intentos = 0
    error_final = None
    browser = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="error", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    while intentos < max_intentos:
        try:
            intentos += 1

            # Carpeta resultados/<consulta_id>
            relative_folder = os.path.join("resultados", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_query = re.sub(r"\s+", "_", f"{(nombre or '').strip()}_{(apellido or '').strip()}").strip("_") or "consulta"
            img_name = f"{NOMBRE_SITIO}_{safe_query}_{ts}.png"
            abs_img_final = os.path.join(absolute_folder, img_name)
            rel_img_final = os.path.join(relative_folder, img_name)

            query_text = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
                ctx = await browser.new_context(
                    viewport={"width": 1366, "height": 1200},
                    device_scale_factor=1,
                    locale="es-ES"
                )
                page = await ctx.new_page()

                # Ir a Wikipedia y buscar
                await page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                await page.wait_for_timeout(WAIT_AFTER_NAV_MS)

                await page.wait_for_selector(SEL_INPUT_SEARCH, timeout=15000)
                await page.fill(SEL_INPUT_SEARCH, query_text)
                await page.keyboard.press("Enter")

                # Esperar resultados o contenido
                try:
                    await page.wait_for_selector(SEL_RESULTS_ANY, timeout=WAIT_RESULTS_MS)
                except Exception:
                    pass

                mensaje = "No se encuentran coincidencias"
                score = 0
                try:
                    if await page.locator(SEL_RESULTS_UL).count() > 0:
                        cont_text = await page.locator(SEL_RESULTS_UL).inner_text()
                        if query_text.lower() in cont_text.lower():
                            mensaje = "Se encontró una coincidencia, por favor validar"
                            score = 10
                except Exception:
                    # Si falla el selector, se mantiene el mensaje por defecto
                    pass

                # Screenshot de página completa
                await page.screenshot(path=abs_img_final, full_page=True)

                await ctx.close()
                await browser.close()
                browser = None

            # Guardar resultado OK
            await _guardar_resultado(
                consulta_id=consulta_id,
                fuente_obj=fuente_obj,
                estado="Validado",
                mensaje=mensaje,
                rel_path=rel_img_final,
            )
            return

        except Exception as e:
            error_final = e
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if intentos < max_intentos:
                continue  # reintentar

    # Si fallaron todos los intentos
    try:
        from PIL import Image, ImageDraw
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        img_name = f"{NOMBRE_SITIO}_error_{ts}.png"
        abs_img_final = os.path.join(absolute_folder, img_name)
        rel_img_final = os.path.join(relative_folder, img_name)

        img = Image.new("RGB", (800, 600), color=(255, 255, 255))
        d = ImageDraw.Draw(img)
        d.text((10, 10), f"Error en consulta Wikipedia: {str(error_final)}", fill=(0, 0, 0))
        img.save(abs_img_final)

        await _guardar_resultado(
            consulta_id=consulta_id,
            fuente_obj=fuente_obj,
            estado="error",
            mensaje=str(error_final),
            rel_path=rel_img_final,
        )
    except Exception as e2:
        await _guardar_resultado(
            consulta_id=consulta_id,
            fuente_obj=fuente_obj,
            estado="error",
            mensaje=f"Error guardando fallo: {e2}",
            rel_path=""
        )
