import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "paco_contratista"
URL = "https://portal.paco.gov.co/index.php?pagina=contratista&identificacion=0"

# Selectores
SEL_INPUT      = "input.input_contratistaContratista[placeholder='NIT / CC']"
SEL_BTN_SEARCH = "button.buscarIdContratista"

# Pistas para centrar vista
RESULT_HINTS = [
    "#tablaContratista", ".tablaContratista", ".table", ".resultado", ".result",
    ".content", ".panel", ".row .col-lg-12", ".col-lg-12"
]

WAIT_AFTER_NAV   = 15000
WAIT_AFTER_CLICK = 1500
WAIT_RESULTS     = 3000


async def consultar_paco_contratista(
    consulta_id: int,
    numero: str,   # NIT / CC
):
    """
    PACO – Reporte Contratista:
      - Abre la página
      - Escribe NIT/CC en el campo de búsqueda
      - Clic en el botón de buscar
      - Espera resultados y toma screenshot
      - Guarda y registra en BD
    """
    browser = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="error",
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
        safe_q = re.sub(r"\s+", "_", (numero or "").strip()) or "consulta"
        png_name = f"{NOMBRE_SITIO}_{safe_q}_{ts}.png"
        abs_png  = os.path.join(absolute_folder, png_name)
        rel_png  = os.path.join(relative_folder, png_name)

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

            # 4) Completar búsqueda
            await page.wait_for_selector(SEL_INPUT, state="visible", timeout=20000)
            await page.fill(SEL_INPUT, "")
            await page.type(SEL_INPUT, str(numero or ""), delay=15)

            # 5) Buscar
            await page.click(SEL_BTN_SEARCH)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_CLICK)
            except Exception:
                pass

            # 6) Esperar/centrar resultados
            await asyncio.sleep(WAIT_RESULTS / 1000)
            try:
                focus = None
                for sel in RESULT_HINTS:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=1200)
                        focus = page.locator(sel).first
                        break
                    except Exception:
                        continue
                if focus:
                    el = await focus.element_handle()
                    if el:
                        await page.evaluate(
                            """(el)=>{const r=el.getBoundingClientRect();
                                     const y=r.top+window.scrollY-160;
                                     window.scrollTo({top:y,behavior:'instant'});}""",
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
