import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "sigep2_directorio"
URL = "https://www1.funcionpublica.gov.co/web/sigep2/directorio"

IFRAME_SEL = "iframe[id^='_com_liferay_iframe_web_portlet_IFramePortlet_'][src*='hvSigep/index']"
SEL_INPUT = "#query"
WAIT_NAV = 15000
WAIT_POST = 3000
MAX_INTENTOS = 3

async def consultar_sigep2_directorio(consulta_id: int, nombre: str, apellido: str):
    query = (f"{(nombre or '').strip()} {(apellido or '').strip()}").strip() or "consulta"
    safe_query = re.sub(r"\s+", "_", query)

    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_name = f"{NOMBRE_SITIO}_{safe_query}_{timestamp}.png"
    absolute_path = os.path.join(absolute_folder, png_name)
    relative_path = os.path.join(relative_folder, png_name)

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo="",
        )
        return

    intentos = 0
    while intentos < MAX_INTENTOS:
        intentos += 1
        browser = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
                ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="es-CO")
                page = await ctx.new_page()

                # Navegar
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=WAIT_NAV)
                except Exception:
                    pass

                # Iframe
                await page.wait_for_selector(IFRAME_SEL, state="visible", timeout=20000)
                iframe_el = await page.query_selector(IFRAME_SEL)
                frame = await iframe_el.content_frame()
                await iframe_el.scroll_into_view_if_needed()
                await asyncio.sleep(0.2)

                # Input y búsqueda
                await frame.wait_for_selector(SEL_INPUT, state="visible", timeout=15000)
                inp = frame.locator(SEL_INPUT)
                await inp.click()
                try:
                    await inp.fill("")
                except Exception:
                    pass
                await inp.type(query, delay=25)
                await inp.press("Enter")

                try:
                    await frame.wait_for_load_state("networkidle", timeout=WAIT_NAV)
                except Exception:
                    pass
                await asyncio.sleep(WAIT_POST / 1000)

                # Screenshot
                await page.screenshot(path=absolute_path, full_page=False)

                # Buscar coincidencia en div-resultados
                div_resultados = frame.locator("#div-resultados-busqueda")

                try:
                    texto = await div_resultados.inner_text()
                except Exception:
                    texto = ""

                # Validación
                if "0 resultados" in texto:
                    score = 0
                    mensaje = texto.strip()
                else:
                    score = 10
                    mensaje = "Se encontraron resultados en la búsqueda"


                if query.lower() in texto.lower():
                    score = 10
                    mensaje = "Se encontró una coincidencia en los resultados"
                else:
                    score = 0
                    mensaje = "La busqueda arrojo 0 coincidencias"

                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validado",
                    mensaje=mensaje,
                    archivo=relative_path,
                )

                await ctx.close()
                await browser.close()
                return

        except Exception as e:
            error_final = e
            if browser:
                try:
                    await page.screenshot(path=absolute_path, full_page=True)
                    await browser.close()
                except:
                    pass

            if intentos >= MAX_INTENTOS:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin validar",
                    mensaje=str(error_final),
                    archivo=relative_path if os.path.exists(absolute_path) else "",
                )
