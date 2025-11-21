import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "cpnt_vigencia_externa_form"
URL = "https://www.tramite.cpnt.gov.co:8443/solicitud/vigenciaexternaform"

# Selectores
SEL_DOCTYPE   = "#doctype"          # select tipo de documento
SEL_NDOC      = "#ndoc"             # input del número de documento
# pistas de “resultado” (modal/popup)
RESULT_HINTS = [
    ".swal2-popup", ".swal2-container",  # SweetAlert
    ".modal.show", ".modal-dialog",      # bootstrap modal
    ".ui-dialog",                        # jQuery UI dialog
    ".alert", ".card", ".card-body"
]

WAIT_AFTER_NAV     = 15000
WAIT_AFTER_TYPE    = 300    # tras TAB breve espera
WAIT_FOR_RESULT    = 4000   # espera a que aparezca el popup

def _norm_tipo(val: str) -> str:
    """
    Normaliza el tipo de documento a los valores del <select id='doctype'>:
      1 Cédula de Ciudadania
      2 Cédula de Extranjería
      3 Pasaporte
      4 Tarjeta de Identidad
    """
    v = (str(val) or "").strip().lower()
    if v in ("1","cc","cedula","cédula","cedula de ciudadania","cédula de ciudadanía"):
        return "1"
    if v in ("2","ce","cedula de extranjeria","cédula de extranjería"):
        return "2"
    if v in ("3","pasaporte","passport"):
        return "3"
    if v in ("4","ti","tarjeta de identidad"):
        return "4"
    return "1"  # por defecto CC


async def consultar_cpnt_vigencia_externa_form(
    consulta_id: int,
    tipo_doc: str,    # "1|2|3|4" o "CC|CE|Pasaporte|TI"
    numero: str,
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

            # 3) Ir a la página
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # 4) Seleccionar tipo de documento
            await page.wait_for_selector(SEL_DOCTYPE, state="visible", timeout=20000)
            await page.select_option(SEL_DOCTYPE, value=_norm_tipo(tipo_doc))

            # 5) Ingresar número y simular TAB (dispara categoryChanged/usuarioident/etc.)
            await page.wait_for_selector(SEL_NDOC, state="visible", timeout=20000)
            ndoc = page.locator(SEL_NDOC)
            await ndoc.fill("")  # limpiar
            await ndoc.type(str(numero or ""), delay=20)
            await page.keyboard.press("Tab")
            await asyncio.sleep(WAIT_AFTER_TYPE / 1000)

            # 6) Esperar popup/resultado
            result_loc = None
            for sel in RESULT_HINTS:
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=WAIT_FOR_RESULT)
                    result_loc = page.locator(sel).first
                    break
                except Exception:
                    continue

            # 7) Centrar la vista (si detectamos modal/alerta)
            try:
                el = await (result_loc or ndoc).element_handle()
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

            # 8) Screenshot
            await page.screenshot(path=abs_png, full_page=False)

            await ctx.close()
            await browser.close()
            browser = None

        # 9) Registrar OK
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
