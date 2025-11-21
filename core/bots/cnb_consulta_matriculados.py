# core/bots/cnb_consulta_matriculados.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "cnb_consulta_matriculados"
URL = "https://www.cnb.gov.co/index.php/servicios/prueba"

# Selectores (dentro del iframe)
IFRAME_SEL        = "iframe#blockrandom, iframe[name='iframe']"
SEL_INPUT_NUM     = "#num"                          # <input name="tarjeta" id="num" ...>
SEL_BTN_SUBMIT    = "input.buttons[value='Consultar']"

# Pistas de resultado (afuera o adentro del iframe)
SEL_RESULT_HINTS = [
    "table", ".contentpane", ".item-page", ".alert", "#system-message", "iframe"
]

WAIT_AFTER_NAV     = 15000
WAIT_AFTER_CLICK   = 1500
EXTRA_RESULT_SLEEP = 2000  # 2s como pediste


async def consultar_cnb_consulta_matriculados(
    consulta_id: int,
    numero: str,
):
    """
    CNB – Consulta de profesionales matriculados:
      - Abre la página
      - Entra al iframe y llena el formulario
      - Clic en 'Consultar'
      - Espera 2 segundos y toma un screenshot de la página
      - Guarda en resultados/<consulta_id> y registra en BD
    """
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

            # 4) Ubicar el iframe y obtener el frame
            await page.wait_for_selector(IFRAME_SEL, state="attached", timeout=15000)
            iframe_el = page.locator(IFRAME_SEL).first
            # Asegurarnos que el iframe esté en pantalla
            try:
                el = await iframe_el.element_handle()
                if el:
                    await page.evaluate(
                        """(el) => el.scrollIntoView({behavior:'instant', block:'center'})""",
                        el
                    )
            except Exception:
                pass

            frame = None
            # Priorizar por name, si no, por element_handle
            try:
                frame = page.frame(name="iframe")
            except Exception:
                frame = None

            if frame is None:
                # fallback: desde el element_handle
                handle = await iframe_el.element_handle()
                frame = await handle.content_frame() if handle else None

            if frame is None:
                raise RuntimeError("No se pudo acceder al contenido del iframe.")

            # 5) Completar formulario DENTRO del iframe
            await frame.wait_for_selector(SEL_INPUT_NUM, state="attached", timeout=10000)
            inp = frame.locator(SEL_INPUT_NUM)
            await inp.click(force=True)
            try:
                await inp.fill("")
            except Exception:
                try:
                    await inp.press("Control+A")
                    await inp.press("Delete")
                except Exception:
                    pass
            await inp.type(str(numero or ""), delay=20)

            # 6) Consultar
            await frame.locator(SEL_BTN_SUBMIT).first.click(force=True)
            try:
                await frame.wait_for_load_state("load", timeout=WAIT_AFTER_CLICK)
            except Exception:
                pass

            # 7) Esperar para que aparezcan resultados
            await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)

            # 8) Opcional: centrar zona de resultados (adentro del iframe o afuera)
            try:
                # Intentar adentro del iframe
                found = False
                for sel in SEL_RESULT_HINTS:
                    try:
                        await frame.wait_for_selector(sel, state="visible", timeout=1000)
                        loc = frame.locator(sel).first
                        el2 = await loc.element_handle()
                        if el2:
                            await frame.evaluate(
                                """(el) => el.scrollIntoView({behavior:'instant', block:'center'})""",
                                el2
                            )
                            found = True
                            break
                    except Exception:
                        continue

                # Si no, intentar afuera (en la página)
                if not found:
                    for sel in SEL_RESULT_HINTS:
                        try:
                            await page.wait_for_selector(sel, state="visible", timeout=1000)
                            loc2 = page.locator(sel).first
                            el3 = await loc2.element_handle()
                            if el3:
                                await page.evaluate(
                                    """(el) => el.scrollIntoView({behavior:'instant', block:'center'})""",
                                    el3
                                )
                                break
                        except Exception:
                            continue
            except Exception:
                pass

            # 9) Screenshot (página completa para capturar el iframe y mensajes)
            await page.screenshot(path=abs_png, full_page=True)

            await ctx.close()
            await browser.close()
            browser = None

        # 10) Registro OK
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
                estado="Sin validar",
                mensaje=str(e),
                archivo="",
            )
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
