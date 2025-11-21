# core/bots/antecedentes_fiscales.py
import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings

url = "https://www.contraloria.gov.co/web/guest/persona-natural"
urljur = "https://www.contraloria.gov.co/web/guest/persona-juridica"
nombre_sitio = "antecedentes_fiscales"

TIPO_DOC_MAP = {
    'CC': 'CC',
    'CE': 'CE',
    'NIT': '1',
    'PAS': 'PA',
    "PEP": "PEP",
    "TI": "TI",
}

async def consultar_antecedentes_fiscales(consulta_id, cedula: str, tipo_doc: str, tipo_persona: str):
    """
    Versión asíncrona. Mantiene la misma lógica que la sync:
      - navega a persona natural o jurídica
      - (si natural) selecciona el tipo de documento en el <select>
      - no guarda capturas (igual que el original; podemos activarlo luego)
    """
    try:
        tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())

        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            page = await navegador.new_page()

            if (tipo_persona or "").lower() == "natural":
                await page.goto(url)
                await page.wait_for_timeout(2000)
                if tipo_doc_val:
                    # select[name="ctl00$MainContent$ddlTipoDocumento"]
                    await page.select_option('select[name="ctl00$MainContent$ddlTipoDocumento"]', tipo_doc_val)
            elif (tipo_persona or "").lower() == "juridica":
                await page.goto(urljur)
                # (el original no hacía más acciones aquí)
            else:
                # comportamiento defensivo si llega algo fuera de "natural"/"juridica"
                await page.goto(url)

            relative_folder = os.path.join('resultados', str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
            absolute_path = os.path.join(absolute_folder, screenshot_name)
            relative_path = os.path.join(relative_folder, screenshot_name)
            await page.screenshot(path=absolute_path)

            await navegador.close()

            return {
                'sitio': nombre_sitio,
                'estado': 'Validada',
                'archivo': relative_path,
                'mensaje': ''
            }

    except Exception as e:
        return {
            'sitio': nombre_sitio,
            'estado': 'Sin validar',
            'archivo': '',
            'mensaje': str(e)
        }
