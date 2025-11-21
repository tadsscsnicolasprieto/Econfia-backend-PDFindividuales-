# core/bots/conpucol_certificados.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "conpucol_certificados"
URL = "https://intranet.conpucol.org/certificados"

# Selectores
SEL_INPUT_DOC   = "#id_number"
SEL_INPUT_CODE  = "#verification_code"
SEL_BTN_CONSULTAR = "button:has-text('Consultar certificados')"
SEL_BTN_VALIDAR   = "button:has-text('Validar certificado')"

# Alguna señal de resultado
SEL_RESULT_HINTS = [
    "table", ".table", "[role='alert']", ".alert", ".grid", ".card", ".container"
]

WAIT_AFTER_NAV       = 15000
WAIT_AFTER_CLICK     = 1500
SLEEP_AFTER_CONSULTA = 1000   # 1s
SLEEP_AFTER_VALIDAR  = 3000   # 3s


async def consultar_conpucol_certificados(
    consulta_id: int,
    numero: str,   # cédula
):
    """
    Conpucol – Certificados:
      - Ingresa 'numero' en #id_number y #verification_code
      - Clic en 'Consultar certificados' (1s)
      - Clic en 'Validar certificado' (3s)
      - Screenshot y registro en BD
    """
    browser = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
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

            # 4) Inputs
            await page.wait_for_selector(SEL_INPUT_DOC, state="visible", timeout=15000)
            await page.fill(SEL_INPUT_DOC, str(numero or ""))

            await page.wait_for_selector(SEL_INPUT_CODE, state="visible", timeout=15000)
            await page.fill(SEL_INPUT_CODE, str(numero or ""))  # por ahora el mismo número

            # 5) Consultar certificados
            await page.locator(SEL_BTN_CONSULTAR).click()
            await asyncio.sleep(SLEEP_AFTER_CONSULTA / 1000)

            # 6) Validar certificado
            await page.locator(SEL_BTN_VALIDAR).click()
            await asyncio.sleep(SLEEP_AFTER_VALIDAR / 1000)

            # 7) Intento de centrar resultados
            try:
                result_loc = None
                for sel in SEL_RESULT_HINTS:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=1200)
                        result_loc = page.locator(sel).first
                        break
                    except Exception:
                        continue

                target = result_loc or page.locator(SEL_BTN_VALIDAR).first
                el = await target.element_handle()
                if el:
                    await page.evaluate(
                        """(el) => {
                            const r = el.getBoundingClientRect();
                            const y = r.top + window.scrollY - 160;
                            window.scrollTo({ top: y, behavior: 'instant' });
                        }""",
                        el
                    )
                    await asyncio.sleep(0.2)
            except Exception:
                pass

            # 8) Screenshot
            await page.screenshot(path=abs_png, full_page=False)

            await ctx.close()
            await browser.close()
            browser = None

        # 9) Registro OK
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
