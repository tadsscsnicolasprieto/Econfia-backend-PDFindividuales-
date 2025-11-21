import os
from datetime import datetime
from playwright.sync_api import sync_playwright
from django.conf import settings

nombre_sitio="rues"

def consular_rues(cedula):
    """
    Plantilla general para bots que automatizan una página, capturan pantallazo y devuelven JSON.
        La funcion recibira los parametros que necesite insertar en los inputs
    """
    url=f"https://ruesfront.rues.org.co/buscar/RM/{cedula}"
    try:
        with sync_playwright() as p:
                        
            navegador = p.chromium.launch(headless=False)
            pagina = navegador.new_page()
            pagina.goto(url)
            
            
            relative_folder = os.path.join('resultados', cedula)
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
            absolute_path = os.path.join(absolute_folder, screenshot_name)
            relative_path = os.path.join(relative_folder, screenshot_name)
            
            pagina.screenshot(path=absolute_path)
            print("Captura guardada:", relative_path)
            
            return {
                'sitio': nombre_sitio,
                'estado': 'ok',
                'archivo': relative_path,
                'mensaje': ''
            }

    except Exception as e:
        return {
            'sitio': nombre_sitio,
            'estado': 'error',
            'archivo': '',
            'mensaje': str(e)
        }
        