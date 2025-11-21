# bots/defunciones.py
import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente

DEFUNCIONES_URL = "https://defunciones.registraduria.gov.co/"
NOMBRE_SITIO = "defunciones"


async def consultar_defunciones(consulta_id: int, cedula: str):
    """
    Consulta en el portal de Defunciones de la Registraduría:
      - Ingresa la cédula en el campo de búsqueda
      - Hace clic en "Buscar"
      - Obtiene el mensaje final (Vigente / Fallecido)
      - Guarda pantallazo de **página completa** y resultado en la BD
    """
    navegador = None
    contexto = None

    # Buscar la fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    # Carpeta resultados/<consulta_id>
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_name = f"defunciones_{cedula}_{ts}.png"
    absolute_path = os.path.join(absolute_folder, screenshot_name)
    relative_path = os.path.join(relative_folder, screenshot_name).replace("\\", "/")

    score_final = 0
    mensaje_final = "No se pudo obtener el estado."

    try:
        async with async_playwright() as p:
            # viewport amplio; full_page ignora la altura, pero esto ayuda con anchos responsivos
            navegador = await p.chromium.launch(headless=False)
            contexto = await navegador.new_context(
                viewport={"width": 1600, "height": 1000},
                device_scale_factor=1,
                locale="es-ES"
            )
            page = await contexto.new_page()

            # 1) Abrir página
            await page.goto(DEFUNCIONES_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            # 2) Llenar la cédula
            await page.fill('input[id="nuip"]', str(cedula).strip())

            # 3) Clic en "Buscar"
            await page.click("button.btn.btn-primary")

            # Esperar que aparezca el resultado
            result_locator = page.locator("div.card-footer")
            await result_locator.wait_for(timeout=15000)

            # 4) Extraer mensaje
            mensaje_final = (await result_locator.inner_text()).strip()
            if "Vigente" in mensaje_final:
                score_final = 0
            elif "Fallecido" in mensaje_final or "Defuncion" in mensaje_final or "Defunción" in mensaje_final:
                score_final = 10
            else:
                score_final = 0

            # 5) Pantallazo **de página completa**
            try:
                # subir al tope por si quedó scrolleado y dar un respiro al layout
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(400)
                await page.screenshot(path=absolute_path, full_page=True)
            except Exception:
                # último recurso: intentarlo sin full_page
                await page.screenshot(path=absolute_path, full_page=False)

            await contexto.close()
            await navegador.close()
            navegador = None
            contexto = None

        # 6) Guardar en BD
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=relative_path,
        )

    except Exception as e:
        # Cierre defensivo
        try:
            if contexto:
                await contexto.close()
        except Exception:
            pass
        try:
            if navegador:
                await navegador.close()
        except Exception:
            pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje=f"Error en bot: {e}",
            archivo="",
        )