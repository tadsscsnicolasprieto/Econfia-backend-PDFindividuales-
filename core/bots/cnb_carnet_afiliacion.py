# core/bots/cnb_carnet_afiliacion.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "cnb_carnet_afiliacion"
URL = "https://cnbcolombia.com/#/carnetafiliacioncnb/id"

# Selectores
SEL_TIPO   = "#inline-form-custom-select-pref"
SEL_NUMERO = "#input-1"
SEL_BTN    = "button:has-text('Consultar')"

# Hints de resultado (intento de anclar cerca de la zona donde aparece el PDF o mensaje)
SEL_RESULT_HINTS = [
    "object[type='application/pdf']",
    "embed[type='application/pdf']",
    "iframe[src*='.pdf']",
    ".card", ".alert", "a[href*='.pdf']",
]

WAIT_AFTER_NAV    = 15000
WAIT_AFTER_ACTION = 2500
EXTRA_RESULT_SLEEP = 3000  # 3 segundos para que carguen resultados

def _norm_tipo(tipo_doc: str) -> str:
    """
    Mapea tu tipo_doc a lo que acepta el select:
      3 = Cédula de Ciudadanía
      4 = Cédula de Extranjería
    """
    v = (str(tipo_doc) or "").strip().lower()
    if v in ("3", "cc", "cedula", "cédula", "cedula de ciudadania", "cédula de ciudadanía"):
        return "3"
    if v in ("4", "ce", "cedula de extranjeria", "cédula de extranjería"):
        return "4"
    # Por defecto a CC
    return "3"


async def consultar_cnb_carnet_afiliacion(
    consulta_id: int,
    tipo_doc: str,   # "3"|"4" o "CC"/"CE"
    numero: str,
):
    """
    CNB – Carné de Afiliación:
      - Abre la página
      - Selecciona tipo de identificación (3/4)
      - Ingresa número
      - Clic en Consultar
      - Espera 3s y hace screenshot (encuadrando el resultado si es posible)
      - Guarda en resultados/<consulta_id> y crea Resultado en BD
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

            # 4) Completar formulario
            await page.wait_for_selector(SEL_TIPO, state="visible", timeout=15000)
            await page.select_option(SEL_TIPO, value=_norm_tipo(tipo_doc))

            await page.wait_for_selector(SEL_NUMERO, state="visible", timeout=15000)
            inp = page.locator(SEL_NUMERO)
            await inp.click(force=True)
            try:
                await inp.fill("")
            except Exception:
                pass
            await inp.type(str(numero or ""), delay=20)

            # 5) Consultar
            await page.locator(SEL_BTN).click()
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_ACTION)
            except Exception:
                pass

            # 6) Esperar a que salgan resultados
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

                # centramos el scroll sobre el área con resultados (o sobre el botón si no hay pista)
                center_target = result_loc or page.locator(SEL_BTN).first
                el = await center_target.element_handle()
                if el:
                    await page.evaluate(
                        """(el) => {
                            const r = el.getBoundingClientRect();
                            const y = r.top + window.scrollY - 180;
                            window.scrollTo({ top: y, behavior: 'instant' });
                        }""",
                        el
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
