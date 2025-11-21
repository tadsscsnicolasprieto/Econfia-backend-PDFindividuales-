# core/bots/sisben.py
import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

url = "https://reportes.sisben.gov.co/dnp_sisbenconsulta"
nombre_sitio = "sisben"

TIPO_DOC_MAP = {
    'RC': '1',
    'CC': '3',
    'CE': '4',
    'DNI(Pais de origen)': '5',
    'DNI(Pasaporte)': '6',
    'Salvoconducto para refugiado': '7',
    'NIT': 'NIT',
    'NUIP': 'NUIP',
    'PAS': 'PAS',
    "PEP": "8"
}

@sync_to_async
def get_fuente(nombre):
    return Fuente.objects.filter(nombre=nombre).first()

@sync_to_async
def guardar_resultado(**kwargs):
    return Resultado.objects.create(**kwargs)

async def tomar_screenshot(pagina, consulta_id, cedula, suffix=""):
    """Crea carpetas y toma screenshot, devuelve ruta relativa"""
    relative_folder = os.path.join('resultados', str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}{suffix}.png"
    absolute_path = os.path.join(absolute_folder, screenshot_name)
    relative_path = os.path.join(relative_folder, screenshot_name)
    await pagina.screenshot(path=absolute_path, full_page=True)  # captura completa
    return relative_path

async def consultar_sisben(cedula, tipo_doc, consulta_id):
    intentos = 0
    error_final = None
    screenshot_path = ""

    while intentos < 3:  # máximo 3 intentos
        try:
            tipo_doc_val = TIPO_DOC_MAP.get(tipo_doc.upper())
            if not tipo_doc_val:
                raise ValueError(f"Tipo de documento no válido: {tipo_doc}")

            async with async_playwright() as p:
                navegador = await p.chromium.launch(headless=True)
                pagina = await navegador.new_page()
                await pagina.goto(url)

                await pagina.select_option('select[id="TipoID"]', tipo_doc_val)
                await pagina.fill('input[id="documento"]', cedula)
                await pagina.wait_for_timeout(1000)
                await pagina.click("input[type='submit']")
                await pagina.wait_for_timeout(4000)

                # Pantallazo de la respuesta
                screenshot_path = await tomar_screenshot(pagina, consulta_id, cedula)

                fuente_obj = await get_fuente(nombre_sitio)

                # Verificar si aparece el popup de NO encontrado
                popup = pagina.locator(".swal2-popup")
                if await popup.is_visible():
                    mensaje = await pagina.inner_text("#swal2-content span")
                    if fuente_obj:
                        await guardar_resultado(
                            consulta_id=consulta_id,
                            fuente=fuente_obj,
                            score=1,  # <- SIEMPRE 1 aunque no esté en Sisbén
                            estado="Validada",
                            mensaje=mensaje,
                            archivo=screenshot_path
                        )
                    await navegador.close()
                    return
                else:
                    # No hay popup → se asume que sí hay registros
                    if fuente_obj:
                        await guardar_resultado(
                            consulta_id=consulta_id,
                            fuente=fuente_obj,
                            score=1,  # <- SIEMPRE 1 aunque sí esté en Sisbén
                            estado="Validada",
                            mensaje="El aspirante se encuentra en la base de datos del sisben",
                            archivo=screenshot_path
                        )
                    await navegador.close()
                    return

        except Exception as e:
            intentos += 1
            error_final = str(e)
            try:
                if 'pagina' in locals():
                    screenshot_path = await tomar_screenshot(pagina, consulta_id, cedula, suffix="_error")
            except:
                pass

    # Si falló en los 3 intentos → guardar error
    fuente_obj = await get_fuente(nombre_sitio)
    if fuente_obj:
        await guardar_resultado(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,  # errores siguen en 0
            estado="Sin validar",
            mensaje="Ocurrió un problema al obtener la información de la fuente",
            archivo=screenshot_path if screenshot_path else ""
        )
