# core/bots/comprobador_derechos.py
import os
from datetime import datetime
from django.conf import settings
from playwright.async_api import async_playwright
from asgiref.sync import sync_to_async

from core.models import Consulta, Resultado, Fuente

url = "https://appb.saludcapital.gov.co/comprobadordederechos/Consulta.aspx"
nombre_sitio = "comprobador_derechos"

async def consultar_comprobador_derechos(consulta_id: int, cedula: str):
    async def _get_fuente():
        return await sync_to_async(lambda: Fuente.objects.filter(nombre=nombre_sitio).first())()

    async def _crear_resultado(estado: str, mensaje: str, score: int, archivo: str = ""):
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=await _get_fuente(),
            estado=estado,
            mensaje=mensaje,
            score=score,
            archivo=archivo
        )

    try:
        # Verifica que exista la consulta
        await sync_to_async(Consulta.objects.get)(id=consulta_id)

        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()
            await pagina.goto(url)

            # Completar y enviar
            await pagina.fill('input[id="MainContent_txtNoId"]', str(cedula))
            await pagina.click('input[type="submit"]')

            # Espera a que la respuesta se pinte
            try:
                # Espera específica al label de error o a un cambio de red
                await pagina.wait_for_selector('#MainContent_lblError', timeout=5000)
            except Exception:
                pass
            await pagina.wait_for_load_state("networkidle")
            await pagina.wait_for_timeout(1000)

            # Determinar mensaje/score
            no_resultado = False
            texto_error = ""

            try:
                lbl = pagina.locator('#MainContent_lblError')
                if await lbl.count() > 0 and await lbl.is_visible():
                    texto_error = (await lbl.inner_text() or "").strip()
                    if "no se encontró" in texto_error.lower() or "no se encontro" in texto_error.lower():
                        no_resultado = True
            except Exception:
                pass

            if not no_resultado:
                # Fallback por si el span existe pero no es visible y el texto está en el DOM
                html_low = (await pagina.content()).lower()
                if ("no se encontr" in html_low) and ("registro" in html_low):
                    no_resultado = True

            # Carpeta por consulta
            relative_folder = os.path.join('resultados', str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
            absolute_path = os.path.join(absolute_folder, screenshot_name)
            relative_path = os.path.join(relative_folder, screenshot_name).replace("\\", "/")

            # Evidencia
            await pagina.screenshot(path=absolute_path)  # sin full_page como tenías
            await navegador.close()

        if no_resultado:
            # Mensaje exacto solicitado cuando no hay registros
            msg = "No se encontró ningun registro con los parámetros ingresados."
            await _crear_resultado("Validada", msg, score=0, archivo=relative_path)
        else:
            msg = "Se encontraron registros con los parámetros ingresados."
            await _crear_resultado("Validada", msg, score=0, archivo=relative_path)

        # Retorno opcional para tu pipeline
        return {
            'sitio': nombre_sitio,
            'estado': 'Validada',
            'archivo': relative_path,
            'mensaje': msg
        }

    except Exception as e:
        await _crear_resultado("Sin Validar", str(e), score=0, archivo="")
        return {
            'sitio': nombre_sitio,
            'estado': 'Sin Validar',
            'archivo': '',
            'mensaje': str(e)
        }
