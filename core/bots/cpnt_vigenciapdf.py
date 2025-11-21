import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "cpnt_vigenciapdf"
URL = "https://www.tramite.cpnt.gov.co:8443/vigenciapdf/consultaf?"

# Selectores
SEL_INPUT_RADICADO = "#radicado"  # <input type="number" id="radicado">
SEL_BTN_CONSULTAR  = "#consignacion_guardar"  # botón “Consultar”

# Pistas de resultado
RESULT_HINTS = [
    "embed[type='application/pdf']",
    "iframe[src*='pdf']",
    "object[type='application/pdf']",
    ".alert", ".card-body", ".resultado", "table"
]

WAIT_AFTER_NAV     = 15000
WAIT_AFTER_CLICK   = 1500
WAIT_AFTER_RESULT  = 3000


async def consultar_cpnt_vigenciapdf(
    consulta_id: int,
    numero: str,  # usamos la cédula como “radicado” temporalmente
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

            # 4) Escribir “radicado” (usando cédula)
            await page.wait_for_selector(SEL_INPUT_RADICADO, state="visible", timeout=20000)
            # Aunque el input es type=number, rellenamos como string para evitar truncamientos
            await page.locator(SEL_INPUT_RADICADO).fill(str(numero or ""))

            # 5) Consultar
            await page.locator(SEL_BTN_CONSULTAR).click()
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_CLICK)
            except Exception:
                pass

            # 6) Esperar que se dibujen resultados
            await asyncio.sleep(WAIT_AFTER_RESULT / 1000)

            # 7) Centrar en resultados si se detecta algo
            try:
                res_loc = None
                for sel in RESULT_HINTS:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=1200)
                        res_loc = page.locator(sel).first
                        break
                    except Exception:
                        continue
                if res_loc:
                    el = await res_loc.element_handle()
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
