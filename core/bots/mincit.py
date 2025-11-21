import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

# Ajusta a tu app real
from core.models import Resultado, Fuente

nombre_sitio = "mincit"
URL = "https://web.mincit.gov.co/disenoindustrial/index.php"


async def consultar_mintic(consulta_id: int, cedula: str):
    navegador = None
    fuente_obj = None

    # Buscar la fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{nombre_sitio}': {e}",
            archivo=""
        )
        return

    try:
        # Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
        absolute_path = os.path.join(absolute_folder, screenshot_name)
        relative_path = os.path.join(relative_folder, screenshot_name)

        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()
            await pagina.goto(URL)

            await pagina.fill('input[name="cedula"]', str(cedula))

            # Remover atributo 'required' del captcha
            await pagina.evaluate("""() => {
                const el = document.querySelector('input[name="captcha"]');
                if (el) el.removeAttribute('required');
            }""")

            await pagina.click('input[type="submit"]')

            # Captura pantalla
            await pagina.screenshot(path=absolute_path)
            await navegador.close()
            navegador = None

        # Registrar OK
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Validada",
            mensaje="",
            archivo=relative_path
        )

    except Exception as e:
        # Inicializar rutas para evitar errores si ocurre excepción antes de su definición
        absolute_path = locals().get('absolute_path', 'error_screenshot.png')
        relative_path = locals().get('relative_path', '')
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=str(e),
                archivo=relative_path
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
