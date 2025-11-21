import os
from datetime import datetime
from playwright.sync_api import sync_playwright
from django.conf import settings


url="https://samm.dsca.mil/search/samm?search_api_fulltext="

nombre_sitio="samm"

def consultar_samm(nombre):
    """
    Plantilla general para bots que automatizan una página, capturan pantallazo y devuelven JSON.
        La funcion recibira los parametros que necesite insertar en los inputs
    """
    nombre = nombre.strip().replace(" ", "+")
    try:
        with sync_playwright() as p:
                        
            navegador = p.chromium.launch(headless=False)
            pagina = navegador.new_page()
            pagina.goto(url+nombre)
                        
            pagina.wait_for_timeout(5000)
            
            relative_folder = os.path.join('resultados', nombre)
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            screenshot_name = f"{nombre_sitio}_{nombre}_{timestamp}.png"
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
