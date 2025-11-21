import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

# 🌍 URL principal
BASE_URL = "https://www.opensanctions.org/datasets/default/"

# 🔎 Diccionario de fuentes: clave (texto en la web) -> valor (nombre en BD)
FUENTES = {
    "Austria Public Officials": "autria_public_officials",
    "ADB Sanctions List": "adb_sanctions",
    # agrega todas las que quieras...
}


async def consultar_fuentes(consulta_id: int, cedula: str, nombre_persona: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(BASE_URL)

        # Recorremos las fuentes
        for clave_web, nombre_bd in FUENTES.items():
            try:
                # Buscar el link por el texto (ej: "Austria Public Officials")
                link = await page.query_selector(f'a:has-text("{clave_web}")')
                if not link:
                    print(f"❌ No encontré el link de {clave_web}")
                    continue

                # Abrir en nueva pestaña
                href = await link.get_attribute("href")
                new_page = await browser.new_page()
                await new_page.goto(f"https://www.opensanctions.org{href}")

                # Buscar input de búsqueda y escribir el nombre
                await new_page.fill("input[name='q']", nombre_persona)
                await new_page.click("button[type='submit']")

                # Esperar respuesta
                await new_page.wait_for_load_state("networkidle")

                # Verificar resultados
                no_match = await new_page.query_selector("div.alert-heading.h4")
                if no_match:
                    mensaje = await no_match.inner_text()
                    score = 1
                else:
                    # Contar coincidencias exactas en toda la página
                    matches = await new_page.query_selector_all(f"text={nombre_persona}")
                    num_matches = len(matches)
                    if num_matches >= 2:
                        mensaje = f"Encontradas {num_matches} coincidencias para {nombre_persona}"
                        score = 5
                    else:
                        mensaje = f"Solo {num_matches} coincidencia(s) encontradas."
                        score = 1

                # Guardar pantallazo
                relative_folder = os.path.join("resultados", str(consulta_id))
                absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
                os.makedirs(absolute_folder, exist_ok=True)

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                png_name = f"{nombre_bd}_{consulta_id}_{cedula}_{timestamp}.png"
                absolute_png = os.path.join(absolute_folder, png_name)
                relative_png = os.path.join(relative_folder, png_name)

                await new_page.screenshot(path=absolute_png, full_page=True)

                # Guardar en BD
                fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_bd)
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validado",
                    mensaje=mensaje,
                    archivo=relative_png,
                )

                print(f"✅ Guardado resultado de {clave_web} - {mensaje}")

                # Cerrar pestaña
                await new_page.close()

            except Exception as e:
                print(f"⚠️ Error en {clave_web}: {e}")

        await browser.close()
