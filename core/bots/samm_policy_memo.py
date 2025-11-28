import os
from datetime import datetime
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://samm.dsca.mil/search/policy_memo?search_api_fulltext"
NOMBRE_SITIO = "samm_policy_memo"
MAX_INTENTOS = 3

async def consultar_samm_policy_memo(consulta_id: int, nombre: str, cedula):
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
    # Variables de respaldo para evitar referencias antes de asignar
    absolute_path = ""
    relative_path = ""

    intentos = 0
    while intentos < MAX_INTENTOS:
        intentos += 1
        browser = None
        page = None
        try:
            async with async_playwright() as p:
                # Lanzar navegador (headless por defecto, configurable)
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()

                # Encodar query y construir URL correctamente (el endpoint espera '=')
                query = quote_plus(nombre_limpio)
                full_url = URL + "=" + query

                # Cabeceras y user-agent básicos para reducir detección
                await page.set_extra_http_headers({
                    "Accept-Language": "es-ES,es;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                })

                await page.goto(full_url, timeout=60000, wait_until="load")
                await page.wait_for_timeout(2500)

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

                # Obtener fuente de forma segura
                fuente_obj = await sync_to_async(lambda: Fuente.objects.filter(nombre=NOMBRE_SITIO).first())()
                if not fuente_obj:
                    fuente_obj = await sync_to_async(Fuente.objects.create)(nombre=NOMBRE_SITIO)

                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validado",
                    mensaje=mensaje,
                    archivo=relative_path
                )

                # Cerrar navegador antes de salir
                try:
                    await browser.close()
                except:
                    pass
                return

        except Exception as e:
            # Intentar guardar screenshot de error si es posible
            try:
                if page:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    screenshot_name = f"{NOMBRE_SITIO}_{cedula}_error_{timestamp}.png"
                    absolute_path = os.path.join(absolute_folder, screenshot_name)
                    relative_path = os.path.join(relative_folder, screenshot_name)
                    await page.screenshot(path=absolute_path)
            except Exception:
                pass

            # Si es el último intento, registrar Resultado con mensaje de error
            if intentos >= MAX_INTENTOS:
                fuente_obj = await sync_to_async(lambda: Fuente.objects.filter(nombre=NOMBRE_SITIO).first())()
                if not fuente_obj:
                    fuente_obj = await sync_to_async(Fuente.objects.create)(nombre=NOMBRE_SITIO)

                archivo_a_guardar = relative_path if absolute_path and os.path.exists(absolute_path) else ""

                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin validar",
                    mensaje=str(e),
                    archivo=archivo_a_guardar
                )
        finally:
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
