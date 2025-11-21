# core/bots/tnem_certificados.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "tnem_certificados"  # Tribunal Nacional de Ética Médica
URL = "https://www.tribunalnacionaldeeticamedica.org/certificados/generar/"

# Selectores
SEL_DOCUMENTO = "#id_document_number"
SEL_BTN_ENVIAR = "button.certificates_button[type='submit']"

# Pistas de “resultado” para encuadrar el pantallazo
SEL_RESULT_HINTS = [
    "div.alert", ".alert-success", ".alert-danger",
    "div#results", ".results", "table", ".table", ".card", "div.row",
    "text=El certificado", "text=No se encontraron", "text=No se encuentra",
]

# Tiempos
WAIT_AFTER_NAV     = 15000
WAIT_AFTER_ACTION  = 2500
EXTRA_RESULT_SLEEP = 3000  # dejar respirar 3s como pediste


async def consultar_tnem_certificados(
    consulta_id: int,
    numero: str,
):
    """
    TNEM – Certificados:
      - Abre la página
      - Ingresa número de documento
      - Click en 'Enviar'
      - Espera ~3s y toma screenshot (encuadrando el resultado)
      - Registra en BD (Resultado)
    """
    browser = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="error", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
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

            # 4) Completar documento
            await page.wait_for_selector(SEL_DOCUMENTO, state="visible", timeout=15000)
            inp = page.locator(SEL_DOCUMENTO)
            await inp.click(force=True)
            try:
                await inp.fill("")
            except Exception:
                pass
            await inp.type(str(numero or ""), delay=20)

            # 5) Enviar
            btn = page.locator(SEL_BTN_ENVIAR)
            await btn.click()
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_ACTION)
            except Exception:
                pass

            # 6) Esperar a que salgan resultados (3s)
            await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)

            # 7) Encadrar y screenshot
            try:
                result_loc = None
                for sel in SEL_RESULT_HINTS:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=1200)
                        result_loc = page.locator(sel).first
                        break
                    except Exception:
                        continue

                # Si encontramos un área de resultado, centramos scroll sobre esa zona; si no, el form
                center_target = result_loc or page.locator("form").first
                handle = await center_target.element_handle()
                if handle:
                    await page.evaluate(
                        """(el) => {
                            const r = el.getBoundingClientRect();
                            const y = r.top + window.scrollY - 160;
                            window.scrollTo({ top: y, behavior: 'instant' });
                        }""",
                        handle
                    )
                    await asyncio.sleep(0.2)
            except Exception:
                pass

            await page.screenshot(path=abs_png, full_page=False)

            await ctx.close()
            await browser.close()
            browser = None

        # 8) Registro OK
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="ok",
            mensaje="",
            archivo=rel_png,
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="error",
                mensaje=str(e),
                archivo="",
            )
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
