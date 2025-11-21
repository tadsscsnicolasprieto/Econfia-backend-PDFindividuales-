# core/bots/cp_validar_certificado.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "cp_validar_certificado"
URL = "https://www.consejoprofesional.org.co/validar-certificado.php?p=8"

# Selectores
SEL_INPUT_CLAVE = "#clave"
SEL_BTN_VALIDAR = "input[name='validar-certificado'][type='submit']"

# Pistas de resultado
SEL_RESULT_HINTS = [
    ".alert", "[role='alert']", ".table", "table", ".panel", ".card",
    ".resultado", ".resultados", ".container"
]

WAIT_AFTER_NAV    = 15000
WAIT_AFTER_SUBMIT = 3000


async def consultar_cp_validar_certificado(
    consulta_id: int,
    numero: str,  # cédula (usada como 'clave' por ahora)
):
    """
    Consejo Profesional – Validar Certificado:
      - Escribe 'numero' en #clave
      - Click en 'Confirmar validez'
      - Espera 3s
      - Screenshot y registro en BD
    """
    browser = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
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

            # 4) Llenar campo y enviar
            await page.wait_for_selector(SEL_INPUT_CLAVE, state="visible", timeout=15000)
            await page.fill(SEL_INPUT_CLAVE, str(numero or ""))

            await page.locator(SEL_BTN_VALIDAR).click()
            await asyncio.sleep(WAIT_AFTER_SUBMIT / 1000)

            # 5) Centrar en resultados si existen
            try:
                result_loc = None
                for sel in SEL_RESULT_HINTS:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=1200)
                        result_loc = page.locator(sel).first
                        break
                    except Exception:
                        continue

                if result_loc:
                    el = await result_loc.element_handle()
                    if el:
                        await page.evaluate(
                            """(el)=>{
                                const r = el.getBoundingClientRect();
                                const y = r.top + window.scrollY - 160;
                                window.scrollTo({top:y, behavior:'instant'});
                            }""",
                            el
                        )
                        await asyncio.sleep(0.2)
            except Exception:
                pass

            # 6) Screenshot
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
