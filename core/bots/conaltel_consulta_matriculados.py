import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "conaltel_consulta_matriculados"
URL = "https://conaltel.org/consulta-de-matriculados/"

# Selectores
SEL_INPUT_CEDULA = "#cedula"           # <input name="cedula" type="tel" class="input_buscador_m" id="cedula">

WAIT_AFTER_NAV     = 12000
WAIT_AFTER_ENTER   = 2500
EXTRA_RESULT_SLEEP = 3000

async def consultar_conaltel_consulta_matriculados(
    consulta_id: int,
    numero: str,  # cédula
):
    browser = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    try:
        # 2) Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_num = re.sub(r"\s+", "_", (numero or "").strip()) or "consulta"
        png_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"
        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="es-CO"
            )
            page = await ctx.new_page()

            # 3) Navegar
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # 4) Ir al input, escribir y presionar Enter
            await page.wait_for_selector(SEL_INPUT_CEDULA, state="visible", timeout=20000)
            ced = page.locator(SEL_INPUT_CEDULA)
            await ced.scroll_into_view_if_needed()
            await ced.click()
            await ced.fill("")
            await ced.type(str(numero or ""), delay=20)
            await ced.press("Enter")

            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_ENTER)
            except Exception:
                pass

            # 5) Esperar que cargue el resultado
            await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)

            # 6) Screenshot (pantalla visible)
            await page.screenshot(path=abs_png, full_page=False)

            await ctx.close()
            await browser.close()
            browser = None

        # 7) Registro OK
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Validada",
            mensaje="",
            archivo=rel_png,
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=str(e),
                archivo="",
            )
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
