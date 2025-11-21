import os
import re
import asyncio
from datetime import datetime
from PIL import Image, ImageDraw

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "superfinanciera_busqueda_pdf"
URL = "https://www.superfinanciera.gov.co/buscar/?q=designa&tk=fba5c5ce4c12e71db8908611a99a91cb"


async def consultar_superfinanciera_busqueda_pdf(consulta_id: int, nombre: str, apellido: str):
    max_intentos = 3
    intentos = 0
    error_final = None
    browser = None

    query = f"{nombre} {apellido}".strip()
    safe_name = re.sub(r"\s+", "_", query) or "consulta"

    # 1. Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="error",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    while intentos < max_intentos:
        try:
            intentos += 1

            # Carpetas
            relative_folder = os.path.join("resultados", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            png_name = f"{NOMBRE_SITIO}_{safe_name}_{ts}.png"
            abs_png = os.path.join(absolute_folder, png_name)
            rel_png = os.path.join(relative_folder, png_name)

            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                ctx = await browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    locale="es-CO"
                )
                page = await ctx.new_page()

                # Ir a la página
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)

                # Aceptar cookies si aparece
                try:
                    await page.locator("button.cookies__boton--si").click(timeout=5000)
                except Exception:
                    pass

                # Escribir búsqueda
                await page.fill("#gsc-i-id1", query)
                await page.keyboard.press("Enter")

                # Esperar resultados
                try:
                    await page.wait_for_selector(".gsc-results .gsc-webResult", timeout=15000)
                except Exception:
                    pass

                await asyncio.sleep(10)  # esperar carga completa

                # Captura de pantalla siempre
                await page.screenshot(path=abs_png)

                # Revisar coincidencia exacta en el contenedor
                score = 0
                mensaje = "No se encontró coincidencia"
                try:
                    resultados = page.locator(".gsc-results .gsc-webResult")
                    count = await resultados.count()
                    for i in range(count):
                        texto = await resultados.nth(i).inner_text()
                        # Verificar coincidencia exacta ignorando mayúsculas/minúsculas
                        if query.lower() == texto.strip().lower():
                            score = 10
                            mensaje = "Se encontró coincidencia exacta"
                            break
                except Exception:
                    pass

                await ctx.close()
                await browser.close()
                browser = None

            # Guardar resultado
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validada",
                mensaje=mensaje,
                archivo=rel_png
            )
            return  # éxito, salir

        except Exception as e:
            error_final = e
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if intentos < max_intentos:
                continue  # reintentar

    # Si fallaron todos los intentos, guardar error con pantallazo dummy
    try:
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        png_name = f"{NOMBRE_SITIO}_error_{safe_name}_{ts}.png"
        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name)

        img = Image.new("RGB", (800, 600), color=(255, 255, 255))
        d = ImageDraw.Draw(img)
        d.text((10, 10), f"Error en consulta Superfinanciera: {str(error_final)}", fill=(0, 0, 0))
        img.save(abs_png)

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje="Ocurrió un problema al obtener la información de la fuente",
            archivo=rel_png
        )
    except Exception as e2:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="error",
            mensaje=f"Error guardando fallo: {e2}",
            archivo=""
        )
