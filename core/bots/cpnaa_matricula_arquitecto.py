import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

# ← ajusta el import a donde tengas tu helper de captcha:
from core.resolver.captcha_v2 import resolver_captcha_v2  # async (url, sitekey)

NOMBRE_SITIO = "cpnaa_matricula_arquitecto"
URL = "https://www.cpnaa.gov.co/matricula-profesional-de-arquitecto-d69/"

# Sitekey que compartiste
RECAPTCHA_SITEKEY = "6Lf2UcMZAAAAAMBlukQO3XsknMUsIEnWI2GXuX0z"

# Selectores
SEL_TIPO_DOC   = "#doc_type"
SEL_NUMERO     = "#doc"
SEL_SUBMIT     = "#btn_verificar"
SEL_CLOSE_POP  = "#closeSvg"

# Pistas de resultado
RESULT_HINTS = [".alert", ".result", ".card", "table", ".modal", ".content"]

WAIT_AFTER_NAV     = 15000
WAIT_AFTER_CLICK   = 2000
EXTRA_RESULT_SLEEP = 3000


def _norm_tipo(tipo_doc: str) -> str:
    """
    CPNAA exige valores:
      1: CÉDULA DE CIUDADANÍA
      2: CÉDULA DE EXTRANJERÍA
      5: PASAPORTE
      25: PPT
    Aceptamos variantes comunes y devolvemos el value correcto.
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
    # por defecto CC
    return "1"


async def consultar_cpnaa_matricula_arquitecto(
    consulta_id: int,
    tipo_doc: str,  # "CC"/"CE"/"PASAPORTE"/"PPT" o directamente "1"/"2"/"5"/"25"
    numero: str
):
    """
    Flujo:
      - Abre la página de CPNAA
      - Selecciona tipo documento, llena número
      - Resuelve reCAPTCHA v2 con capsolver
      - Cierra popup emergente si aparece (#closeSvg)
      - Envía y toma screenshot
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

            # 3) Navegar
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # Quitar popup si estuviera abierto de entrada
            try:
                close_el = page.locator(SEL_CLOSE_POP)
                if await close_el.is_visible():
                    await close_el.click()
                    await asyncio.sleep(0.2)
            except Exception:
                pass

            # 4) Seleccionar tipo doc y llenar número
            await page.wait_for_selector(SEL_TIPO_DOC, state="visible", timeout=15000)
            await page.select_option(SEL_TIPO_DOC, value=_norm_tipo(tipo_doc))

            await page.wait_for_selector(SEL_NUMERO, state="visible", timeout=15000)
            await page.fill(SEL_NUMERO, "")
            await page.type(SEL_NUMERO, str(numero or ""), delay=20)

            # 5) Resolver reCAPTCHA v2
            try:
                token = await resolver_captcha_v2(URL, RECAPTCHA_SITEKEY)
                # Inyectamos el token en todos los textareas g-recaptcha-response visibles
                await page.evaluate(
                    """(tok) => {
                        const els = document.querySelectorAll('textarea[name="g-recaptcha-response"], #g-recaptcha-response');
                        els.forEach(el => {
                            el.style.display = 'block';
                            el.value = tok;
                        });
                    }""",
                    token
                )
            except Exception as e:
                # Si prefieres fallar duro, lanza el error; aquí seguimos e intentamos enviar
                print(f"[CPNAA] No se pudo resolver el captcha: {e}")

            # 6) Enviar (cerrar popup si aparece antes)
            try:
                close_el = page.locator(SEL_CLOSE_POP)
                if await close_el.is_visible():
                    await close_el.click()
                    await asyncio.sleep(0.2)
            except Exception:
                pass

            await page.locator(SEL_SUBMIT).click()
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_CLICK)
            except Exception:
                pass

            # 7) Esperar resultados y centrar
            await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)

            try:
                result_loc = None
                for sel in RESULT_HINTS:
                    try:
                        await page.locator(sel).first.wait_for(state="visible", timeout=2500)
                        result_loc = page.locator(sel).first
                        break
                    except Exception:
                        continue

                if result_loc:
                    el = await result_loc.element_handle()
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
