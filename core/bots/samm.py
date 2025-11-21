# core/bots/samm.py
import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Fuente, Resultado
from PIL import Image, ImageDraw

URL = "https://samm.dsca.mil/search/site_search"
NOMBRE_SITIO = "samm"


async def consultar_samm(consulta_id: int, nombre_completo: str, cedula):
    max_intentos = 3
    intentos = 0
    error_final = None

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
            file_name = f"{NOMBRE_SITIO}_{consulta_id}_{ts}.png"
            absolute_path = os.path.join(absolute_folder, file_name)
            relative_path = os.path.join(relative_folder, file_name)

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                page = await context.new_page()

                # 1) Abrir página
                await page.goto(URL, timeout=60000, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                # 2) Campo de búsqueda
                await page.locator("#edit-search-api-fulltext").wait_for(state="visible", timeout=20000)
                await page.fill("#edit-search-api-fulltext", nombre_completo or "")
                await page.click("#edit-submit-search-view-all-site-content")

                # 3) Esperar resultados
                try:
                    await page.wait_for_selector("main, .view-content, .search-results, article", timeout=8000)
                except Exception:
                    pass
                await page.wait_for_timeout(3000)

                # 4) Verificar contenedor de resultados vacíos
                score = 10
                mensaje = "Se encontraron resultados."
                try:
                    if await page.locator("div.view-empty").count() > 0:
                        text_empty = await page.locator("div.view-empty").inner_text()
                        if "No matching search results." in text_empty:
                            score = 0
                            mensaje = "No se encuentran coincidencias."
                except Exception:
                    pass

                # 5) Captura pantalla
                await page.screenshot(path=absolute_path, full_page=True)
                await browser.close()

            # Guardar en BD (éxito)
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validada",
                mensaje=mensaje,
                archivo=relative_path
            )
            return  # ✅ éxito, salir de la función

        except Exception as e:
            error_final = e
            if intentos < max_intentos:
                continue  # volver a intentar

    # --- Si fallaron todos los intentos ---
    try:
        # Pantallazo "dummy" con el error
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = f"{NOMBRE_SITIO}_error_{consulta_id}_{ts}.png"
        absolute_path = os.path.join(absolute_folder, file_name)
        relative_path = os.path.join(relative_folder, file_name)

        img = Image.new("RGB", (800, 600), color=(255, 255, 255))
        d = ImageDraw.Draw(img)
        d.text((10, 10), f"Error en consulta SAMM:\n{str(error_final)}", fill=(0, 0, 0))
        img.save(absolute_path)

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje="Ocurrió un problema al obtener la información de la fuente",
            archivo=relative_path
        )
    except Exception as e2:
        # Fallback si hasta guardar el error falla
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje="Ocurrió un problema al obtener la información de la fuente",
            archivo=""
        )
