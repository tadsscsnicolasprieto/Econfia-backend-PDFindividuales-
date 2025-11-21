import os
import re
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from core.models import Resultado, Fuente
from asgiref.sync import sync_to_async

URL = "https://www.worldbank.org/en/projects-operations/procurement/debarred-firms"
NOMBRE_SITIO = "worldbank_debarred_pdf"


def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"


async def consultar_worldbank_debarred_pdf(consulta_id: int, nombre: str, cedula):
    max_intentos = 3
    intentos = 0
    error_final = None
    out_img_rel = ""  # lo guardamos para asignar siempre al resultado

    while intentos < max_intentos:
        try:
            intentos += 1

            relative_folder = os.path.join("resultados", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_img_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{cedula}_{ts}.png")
            out_img_rel = os.path.join(relative_folder, os.path.basename(out_img_abs))

            # Construir la URL con query
            query = nombre.replace(" ", "%20")
            final_url = f"https://www.worldbank.org/en/search?q={query}&Type=News"

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
                page = await context.new_page()

                await page.goto(final_url, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except:
                    pass

                mensaje = ""
                try:
                    contenedor = page.locator("div.all__listing_result[role='status']")
                    if await contenedor.count() > 0:
                        mensaje = await contenedor.inner_text()
                    else:
                        mensaje = "No se encontró el contenedor esperado."
                except Exception as e:
                    mensaje = f"Error buscando resultados: {str(e)}"

                # Guardar pantallazo real de la página
                await page.screenshot(path=out_img_abs, full_page=True)
                await browser.close()

            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="validado",
                mensaje=mensaje,
                archivo=out_img_rel
            )
            return

        except Exception as e:
            error_final = e
            if intentos < max_intentos:
                continue

            # Generar pantallazo en blanco con el mensaje del error
            relative_folder = os.path.join("resultados", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_img_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{cedula}_{ts}_error.png")
            out_img_rel = os.path.join(relative_folder, os.path.basename(out_img_abs))

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
                page = await context.new_page()

                await page.set_content(f"<html><body><h2 style='color:red;'>Error</h2><p>{str(error_final)}</p></body></html>")
                await page.screenshot(path=out_img_abs, full_page=True)
                await browser.close()

    # Registrar resultado con error + pantallazo
    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=0,
        estado="sin validar",
        mensaje=str(error_final),
        archivo=out_img_rel,
    )
