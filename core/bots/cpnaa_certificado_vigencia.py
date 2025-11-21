import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

# Usa tu helper existente de capsolver
from core.resolver.captcha_v2 import resolver_captcha_v2  # async (url, sitekey)

NOMBRE_SITIO = "cpnaa_certificado_vigencia"
URL = "https://www.cpnaa.gov.co/certificado-vigencia-profesional/"

# iframe del formulario real
IFRAME_SEL = 'iframe[src*="oficinavirtual.cpnaa.gov.co/tramites/certificado_de_vigencia"]'

# Sitekey que diste (misma del CPNAA)
RECAPTCHA_SITEKEY = "6Lf2UcMZAAAAAMBlukQO3XsknMUsIEnWI2GXuX0z"

# Selectores (DENTRO del iframe)
SEL_TIPO_DOC     = "#x_document_type"
SEL_DOCUMENTO    = "#x_document"
SEL_SUBMIT       = "#btn_enviar_vigencia"

# Selectores (FUERA, popup de bienvenida)
SEL_POP_CLOSE_BTN = "#dismissIcon, #closeSvg"

WAIT_AFTER_NAV     = 15000
WAIT_AFTER_CLICK   = 2000
EXTRA_RESULT_SLEEP = 3000

def _norm_tipo(tipo_doc: str) -> str:
    """
    Mapea a los values del select CPNAA:
      1: CC
      2: CE
      5: PASAPORTE
      25: PPT
    """
    v = (str(tipo_doc) or "").strip().lower()
    if v in ("1", "cc", "cédula de ciudadanía", "cedula de ciudadania", "cedula"):
        return "1"
    if v in ("2", "ce", "cédula de extranjería", "cedula de extranjeria"):
        return "2"
    if v in ("5", "pasaporte", "passport"):
        return "5"
    if v in ("25", "ppt", "permiso por protección temporal", "permiso por proteccion temporal"):
        return "25"
    return "1"


async def consultar_cpnaa_certificado_vigencia(
    consulta_id: int,
    tipo_doc: str,   # "CC"/"CE"/"PASAPORTE"/"PPT" o 1/2/5/25
    numero: str
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
        # 2) Carpeta resultados
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

            # 3) Abrir página contenedora
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # 4) Cerrar popup si aparece
            try:
                pop = page.locator(SEL_POP_CLOSE_BTN)
                if await pop.is_visible():
                    await pop.click()
                    await asyncio.sleep(0.2)
            except Exception:
                pass

            # 5) Entrar al IFRAME real del trámite
            await page.wait_for_selector(IFRAME_SEL, state="visible", timeout=20000)
            frame = await (await page.query_selector(IFRAME_SEL)).content_frame()

            # 6) Completar tipo y documento
            await frame.wait_for_selector(SEL_TIPO_DOC, state="visible", timeout=15000)
            await frame.select_option(SEL_TIPO_DOC, value=_norm_tipo(tipo_doc))

            await frame.wait_for_selector(SEL_DOCUMENTO, state="visible", timeout=15000)
            await frame.fill(SEL_DOCUMENTO, "")
            await frame.type(SEL_DOCUMENTO, str(numero or ""), delay=20)

            # 7) Resolver reCAPTCHA v2 e inyectar token
            try:
                token = await resolver_captcha_v2(URL, RECAPTCHA_SITEKEY)
                await frame.evaluate(
                    """(tok) => {
                        const els = document.querySelectorAll('textarea[name="g-recaptcha-response"], #g-recaptcha-response');
                        els.forEach(el => { el.style.display='block'; el.value = tok; });
                    }""",
                    token
                )
            except Exception as e:
                print(f"[CPNAA Vigencia] No se pudo resolver captcha: {e}")

            # 8) Enviar
            await frame.click(SEL_SUBMIT)
            try:
                await frame.wait_for_load_state("networkidle", timeout=WAIT_AFTER_CLICK)
            except Exception:
                pass

            # 9) Esperar que aparezcan resultados
            await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)

            # 10) Screenshot (desde la página contenedora para incluir contexto)
            await page.screenshot(path=abs_png, full_page=False)

            await ctx.close()
            await browser.close()
            browser = None

        # 11) Registrar OK
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
