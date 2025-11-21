# bots/cpiq_validacion_matricula.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "cpiq_validacion_matricula"

# Selectores (según la página)
SEL_CEDULA = '#id_number'  # <input type="text" name="id_number" id="id_number" ...>
SEL_RESULT_HINTS = [
    "text=Resultado de la validación",
    "text=no se encuentra registrado",
    "text=se encuentra registrado",
    "div.alert", "div.card", "section", "article"
]

# Tiempos
WAIT_AFTER_NAV     = 15000
WAIT_AFTER_ENTER   = 2500
EXTRA_RESULT_SLEEP = 1500


async def consultar_cpiq_validacion_matricula(
    consulta_id: int,
    numero: str  # cédula
):
    """
    CPIQ – Validación de la matrícula profesional:
      - Abre https://www.cpiq.gov.co/validacion_matricula_profesional.php
      - Ingresa C.C. en 'id_number' y presiona Enter
      - Espera render de resultados
      - Reescribe el número para que salga visible en el screenshot
      - Toma pantallazo y registra en BD
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
            await page.goto("https://www.cpiq.gov.co/validacion_matricula_profesional.php",
                            wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # Llenar cédula y simular Enter
            await page.wait_for_selector(SEL_CEDULA, state="visible", timeout=15000)
            inp = page.locator(SEL_CEDULA)
            await inp.click(force=True)
            try:
                await inp.fill("")
            except Exception:
                pass
            await inp.type(numero or "", delay=25)
            await inp.press("Enter")

            # Esperar render tras Enter
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_ENTER)
            except Exception:
                pass

            # Señales de resultado
            found = False
            for sel in SEL_RESULT_HINTS:
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=2500)
                    found = True
                    break
                except Exception:
                    continue
            await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)

            # Reescribir número para que quede visible en el pantallazo
            try:
                await inp.fill("")
                await inp.type(numero or "", delay=10)
                await asyncio.sleep(0.2)
            except Exception:
                pass

            # Screenshot (full page para capturar el bloque de resultados)
            await page.screenshot(path=abs_png, full_page=True)

            await ctx.close()
            await browser.close()
            browser = None

        # Registro OK
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
