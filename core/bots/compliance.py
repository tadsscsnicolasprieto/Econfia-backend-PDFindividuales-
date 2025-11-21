import os
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Fuente, Resultado

nombre_sitio = "compliance"

async def consultar_compliance(nombre: str, consulta_id: int, cedula: str):
    """
    Bot que busca coincidencias exactas en el portal de Compliance.
    - Busca dentro de los div.col-md-8.col-sm-12
    - Ignora el contenedor que muestra "Searching for: ..."
    - Si encuentra el nombre exacto, guarda pantallazo + hallazgo en BD
    - Si no encuentra, guarda estado "sin coincidencias"
    """
    # Construir la URL de búsqueda
    nombre_encoded = nombre.replace(" ", "+")
    url = f"https://www.compliance.com.co/?s={nombre_encoded}"

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()
            await pagina.goto(url, timeout=60000)

            await pagina.wait_for_timeout(4000)

            # Crear carpeta resultados
            relative_folder = os.path.join("resultado", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            # Buscar contenedores de resultados
            contenedores = await pagina.query_selector_all("div.col-md-8.col-sm-12")
            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
            hallazgo = False

            for idx, cont in enumerate(contenedores, start=1):
                contenido = await cont.inner_text()

                # Omitir el bloque "Searching for: ..."
                if contenido.strip().lower().startswith("searching for:"):
                    continue

                if nombre.lower() in contenido.lower():  # coincidencia exacta ignorando mayúsculas
                    hallazgo = True

                    # Guardar pantallazo del contenedor
                    screenshot_name = f"{nombre_sitio}_{cedula}_result_{idx}.png"
                    screenshot_path = os.path.join(absolute_folder, screenshot_name)
                    await cont.screenshot(path=screenshot_path)

                    # Guardar hallazgo en BD
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=1,
                        estado="Validada",
                        mensaje=f"Nombre {nombre} encontrado en Compliance",
                        archivo=screenshot_path
                    )

            if not hallazgo:
                # Guardar pantallazo general de la página si no hubo coincidencias
                screenshot_name = f"{nombre_sitio}_{cedula}_sin_resultados.png"
                screenshot_path = os.path.join(absolute_folder, screenshot_name)
                await pagina.screenshot(path=screenshot_path)

                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin Validar",
                    mensaje="No se encontraron registros en Compliance",
                    archivo=screenshot_path
                )

            await navegador.close()

    except Exception as e:
        print(f"Error en consultar_compliance: {e}")
