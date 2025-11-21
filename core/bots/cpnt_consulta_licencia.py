import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "cpnt_consulta_licencia"
URL = "https://www.cpnt.gov.co/index.php/tramites-y-servicios/consulta-del-registro-de-la-licencia-profesional-en-el-cpnt"

# Selectores dentro del IFRAME
IFRAME_SEL     = "iframe[src*='consulta/licencia']"
SEL_INPUT_USER = "#user"
SEL_BTN_SUBMIT = "input[name='_action_licenciarta']"

RESULT_HINTS = ["table", ".alert", ".card", ".contentpane", ".item-page", ".modal"]

WAIT_AFTER_NAV   = 15000
WAIT_AFTER_CLICK = 2000


async def consultar_cpnt_consulta_licencia(consulta_id: int, numero: str):
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
        # 2) carpeta resultados/<consulta_id>
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

            # 4) Entrar al iframe
            await page.wait_for_selector(IFRAME_SEL, state="visible", timeout=15000)
            frame = page.frame_locator(IFRAME_SEL)

            # 5) Llenar campo y enviar
            await frame.locator(SEL_INPUT_USER).wait_for(state="visible", timeout=15000)
            await frame.locator(SEL_INPUT_USER).fill("")
            await frame.locator(SEL_INPUT_USER).type(str(numero or ""), delay=20)

            await frame.locator(SEL_BTN_SUBMIT).click()
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_CLICK)
            except Exception:
                pass

            # 6) Intentar centrar en resultados (dentro del iframe)
            try:
                result_loc = None
                for sel in RESULT_HINTS:
                    try:
                        await frame.locator(sel).first.wait_for(state="visible", timeout=2500)
                        result_loc = frame.locator(sel).first
                        break
                    except Exception:
                        continue

                el = await (result_loc or frame.locator(SEL_BTN_SUBMIT)).element_handle()
                if el:
                    # scroll de la página principal hasta el iframe
                    await page.evaluate(
                        """(sel) => {
                            const ifr = document.querySelector(sel);
                            if (ifr) {
                                const r = ifr.getBoundingClientRect();
                                const y = r.top + window.scrollY - 160;
                                window.scrollTo({ top: y, behavior: 'instant' });
                            }
                        }""",
                        IFRAME_SEL
                    )
                    await asyncio.sleep(0.2)
            except Exception:
                pass

            # 7) Screenshot
            await page.screenshot(path=abs_png, full_page=False)

            await ctx.close()
            await browser.close()
            browser = None

        # 8) Registro OK
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
