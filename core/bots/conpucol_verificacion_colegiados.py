# core/bots/conpucol_verificacion_colegiados.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "conpucol_verificacion_colegiados"
URL = "https://intranet.conpucol.org/verificacion-colegiados"

# Selectores robustos
SEL_INPUT_ID   = "#id_number, input[id='id_number'], input[placeholder='10157893645']"
SEL_BTN_SUBMIT = "button:has-text('Verificar')"

# Pistas de resultado (por si el sitio pinta una tarjeta/tabla/alerta)
SEL_RESULT_HINTS = [
    ".card", ".alert", ".grid", ".space-y-4", "table", "section", "article"
]

WAIT_AFTER_NAV     = 15000
WAIT_AFTER_CLICK   = 1500
EXTRA_RESULT_SLEEP = 2000   # 2 s como pediste


async def consultar_conpucol_verificacion_colegiados(
    consulta_id: int,
    numero: str,   # cédula a consultar
):
    """
    CONPUCOL – Verificación de Colegiados:
      - Abre la página
      - Escribe la cédula
      - Clic en 'Verificar'
      - Espera 2s y toma screenshot de resultados
      - Guarda archivo en resultados/<consulta_id> y registra en BD
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

            # 3) Ir al sitio
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # 4) Completar el campo de cédula
            await page.wait_for_selector(SEL_INPUT_ID, state="visible", timeout=15000)
            id_input = page.locator(SEL_INPUT_ID).first
            await id_input.click()
            try:
                await id_input.fill("")  # limpia si hay algo
            except Exception:
                pass
            # tipeo pausado por si hay validaciones livewire/alpine
            await id_input.type(str(numero or ""), delay=30)

            # 5) Clic en "Verificar"
            await page.locator(SEL_BTN_SUBMIT).first.click()
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_CLICK)
            except Exception:
                pass

            # 6) Esperar que pinte resultados y centrar si es posible
            await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)
            try:
                result_loc = None
                for sel in SEL_RESULT_HINTS:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=1200)
                        result_loc = page.locator(sel).first
                        break
                    except Exception:
                        continue

                center_target = result_loc or page.locator(SEL_BTN_SUBMIT).first
                el = await center_target.element_handle()
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
            mensaje="Consulta ejecutada en CONPUCOL – Verificación de colegiados.",
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
