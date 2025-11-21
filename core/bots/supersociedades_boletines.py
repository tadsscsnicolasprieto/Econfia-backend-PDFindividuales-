import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright
from PIL import Image, ImageDraw

from core.models import Resultado, Fuente

NOMBRE_SITIO = "supersociedades_boletines_conceptos"
URL = "https://www.supersociedades.gov.co/boletines-conceptos-juridicos"
SEL_INPUT = "#BuscadorBolConJuri"  # buscador live

async def consultar_supersociedades_boletines(
    consulta_id: int,
    nombre: str,
    apellido: str,
    mostrar_navegador: bool = True,
    slow_ms: int = 150,
):
    max_intentos = 3
    intentos = 0
    error_final = None
    browser = None

    query = f"{nombre} {apellido}".strip()
    safe_query = re.sub(r"\s+", "_", query) or "consulta"

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="error",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo="",
        )
        return

    while intentos < max_intentos:
        try:
            intentos += 1

            # Carpeta
            relative_folder = os.path.join("resultados", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            png_name = f"{NOMBRE_SITIO}_{safe_query}_{ts}.png"
            abs_png = os.path.join(absolute_folder, png_name)
            rel_png = os.path.join(relative_folder, png_name)

            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=False,
                    slow_mo=slow_ms,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--start-maximized",
                    ],
                )
                ctx = await browser.new_context(viewport=None, locale="es-CO")
                page = await ctx.new_page()
                await page.bring_to_front()

                # Ir a la página
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass

                # Foco al buscador y escribir
                await page.wait_for_selector(SEL_INPUT, state="visible", timeout=15000)
                await page.click(SEL_INPUT)
                await page.fill(SEL_INPUT, "")
                await page.type(SEL_INPUT, query)
                await asyncio.sleep(2.5)  # esperar filtro live

                # Screenshot
                await page.screenshot(path=abs_png, full_page=True)

                # Buscar mensaje de "no hay resultados"
                score = 10
                mensaje = "Se encontraron hallazgos"
                try:
                    locator = page.locator(
                        "div#buscarBolConJuriGroup li#ConincidenciasPorTitulo"
                    )
                    if await locator.count() > 0:
                        text = (await locator.inner_text()).strip()
                        if "NO existen búsquedas relacionadas" in text:
                            score = 0
                            mensaje = text
                except Exception:
                    pass

                await ctx.close()
                await browser.close()
                browser = None

            # Guardar resultado OK
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validada",
                mensaje=mensaje,
                archivo=rel_png,
            )
            return

        except Exception as e:
            error_final = e
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if intentos < max_intentos:
                continue  # reintenta

    # Si fallaron todos los intentos, guardar error con pantallazo "dummy"
    try:
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        png_name = f"{NOMBRE_SITIO}_error_{safe_query}_{ts}.png"
        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name)

        img = Image.new("RGB", (800, 600), color=(255, 255, 255))
        d = ImageDraw.Draw(img)
        d.text((10, 10), f"Error en consulta Supersociedades: {str(error_final)}", fill=(0, 0, 0))
        img.save(abs_png)

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje="Ocurrió un problema al obtener la información de la fuente",
            archivo=rel_png,
        )
    except Exception as e2:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="sin validar",
            mensaje=f"Error guardando fallo: {e2}",
            archivo=""
        )
