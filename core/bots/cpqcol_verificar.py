# bots/cpqcol_verificar.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "cpqcol_verificar"

# Selectores
SEL_INPUT_NUM   = "#number"
# botón no es necesario (usamos Enter), pero dejo fallback por si quieres click
SEL_BTN_CONSULTAR = "button[type='submit'], button:has-text('CONSULTAR'), button:has-text('Consultar')"

# Señales de resultado (banner de error o contenedor de resultado)
SEL_RESULT_HINTS = [
    "text=CONSULTA DE MATRÍCULAS",
    "text=El valor enviado no coincide",
    "div[role='alert']",
    "div.toast, .alert, .text-red-600",
]

# Tiempos
WAIT_AFTER_NAV     = 12000
WAIT_AFTER_ENTER   = 2000
EXTRA_RESULT_SLEEP = 1200


async def consultar_cpqcol_verificar(
    consulta_id: int,
    numero: str,   # cédula o código a consultar
):
    """
    CPQCOL – Verificar matrícula:
      - Abre https://tramites.cpqcol.gov.co/verificar
      - Ingresa el número en #number y presiona Enter
      - Espera render y toma screenshot (full page)
      - Registra OK/ERROR en BD
    """
    browser = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    try:
        # Carpeta resultados/<consulta_id>
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
                viewport={"width": 1440, "height": 1200},
                locale="es-CO"
            )
            page = await ctx.new_page()

            # Navegar
            await page.goto("https://tramites.cpqcol.gov.co/verificar",
                            wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # Llenar número y simular Enter
            await page.wait_for_selector(SEL_INPUT_NUM, state="visible", timeout=15000)
            inp = page.locator(SEL_INPUT_NUM)
            await inp.click(force=True)
            try:
                await inp.fill("")
            except Exception:
                pass
            await inp.type(str(numero or ""), delay=25)
            await inp.press("Enter")

            # Esperar respuesta
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_ENTER)
            except Exception:
                pass

            found = False
            for sel in SEL_RESULT_HINTS:
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=2500)
                    found = True
                    break
                except Exception:
                    continue

            await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)

            # Reescribir número para que se vea en el pantallazo
            try:
                await inp.fill("")
                await inp.type(str(numero or ""), delay=10)
                await asyncio.sleep(0.2)
            except Exception:
                pass

            # Screenshot full page
            await page.screenshot(path=abs_png, full_page=True)

            await ctx.close()
            await browser.close()
            browser = None

        # Registro OK (aunque sea “no coincide”, si no hubo excepción es ok)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Validada",
            mensaje="" if found else "No se detectaron señales explícitas de resultados (se guardó screenshot).",
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
            try:
                if browser is not None:
                    await browser.close()
            except Exception:
                pass
