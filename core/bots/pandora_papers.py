import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

nombre_sitio = "pandora_papers"

async def consultar_pandora_papers(consulta_id: int, nombre: str, apellido: str, cedula):
    # Crear carpeta de resultados
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"{nombre_sitio}_{cedula}_{ts}.png"
    absolute_path = os.path.join(absolute_folder, file_name)
    relative_path = os.path.join(relative_folder, file_name)

    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)

    intentos = 0
    exito = False
    last_exception = None

    while intentos < 3 and not exito:
        intentos += 1
        try:
            nombre_busqueda = (nombre or "").strip().replace(" ", "+") + (apellido or "").strip().replace(" ", "+")
            url = f"https://offshoreleaks.icij.org/investigations/pandora-papers?c=&cat=1&j=&q={nombre_busqueda}"

            async with async_playwright() as p:
                navegador = await p.chromium.launch(headless=True)
                pagina = await navegador.new_page()

                # Navegar a la página
                await pagina.goto(url, wait_until="domcontentloaded", timeout=60000)

                # Aceptar cookies si aparecen
                try:
                    await pagina.click('input[id="accept"]', timeout=5000)
                    await pagina.click('button.btn.btn-primary.btn-block.btn-lg', timeout=5000)
                except:
                    pass  # Si no hay cookies, sigue normal

                # Esperar resultados
                try:
                    await pagina.wait_for_selector("div.container.search", timeout=10000)
                except:
                    pass
                await pagina.wait_for_timeout(3000)

                # Guardar captura de pantalla
                await pagina.screenshot(path=absolute_path, full_page=True)
                await navegador.close()

            # Validación de tamaño
            if not os.path.exists(absolute_path) or os.path.getsize(absolute_path) < 10_000:
                raise Exception("El pantallazo parece vacío o muy pequeño.")

            # Guardar en BD como éxito
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Validado",
                mensaje="No se encuentran coincidencias con el criterio de busqueda",
                archivo=relative_path
            )
            exito = True

        except Exception as e:
            last_exception = e
            try:
                if "pagina" in locals():
                    await pagina.screenshot(path=absolute_path, full_page=True)
            except:
                pass
            try:
                if "navegador" in locals():
                    await navegador.close()
            except:
                pass

    # Si fallaron los 3 intentos
    if not exito:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje=str(last_exception),
            archivo=relative_path if os.path.exists(absolute_path) else ""
        )
