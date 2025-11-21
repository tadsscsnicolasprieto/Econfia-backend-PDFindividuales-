import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente
from core.resolver.captcha_v2 import resolver_captcha_v2  # tu helper

NOMBRE_SITIO = "conte_consulta_matricula"
URL = "https://solicitudmatricula.conte.org.co:8080/consulta-matricula"

# Selectores
SEL_INPUT_DOC       = "#input-vaadin-text-field-6"      # Documento
SEL_CHECK_TYC       = "#input-vaadin-checkbox-7"        # Acepto TyC
SEL_CAPTCHA_TEXTA   = "textarea#g-recaptcha-response"   # reCAPTCHA hidden textarea
SEL_BTN_CONSULTAR   = "vaadin-button:has-text('Consultar')"

# Sitekey reCAPTCHA (de la página)
SITEKEY = "6LddT-ElAAAAAEJWK99x4Ni9hp7yup2APq8Dm1Pi"

# Hints de resultados
RESULT_HINTS = ["vaadin-grid", "[role='grid']", "table", ".resultado", ".result"]

WAIT_AFTER_NAV      = 15000
WAIT_AFTER_CLICK    = 1500
WAIT_AFTER_RESULTS  = 3000


async def consultar_conte_consulta_matricula(
    consulta_id: int,
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

            # 3) Navegar
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # 4) Llenar documento
            await page.wait_for_selector(SEL_INPUT_DOC, state="visible", timeout=20000)
            await page.locator(SEL_INPUT_DOC).fill(str(numero or ""))

            # 5) Resolver reCAPTCHA v2 e inyectar token
            try:
                token = await resolver_captcha_v2(URL, SITEKEY)
                if token:
                    await page.wait_for_selector(SEL_CAPTCHA_TEXTA, state="attached", timeout=15000)
                    await page.evaluate(
                        """(sel, val) => {
                            const ta = document.querySelector(sel);
                            if (ta) {
                                ta.value = val;
                                ta.dispatchEvent(new Event('input', {bubbles:true}));
                                ta.dispatchEvent(new Event('change', {bubbles:true}));
                            }
                        }""",
                        SEL_CAPTCHA_TEXTA, token
                    )
            except Exception:
                # si falla el solver, intentamos igual; el backend podría rechazar sin token
                pass

            # 6) Aceptar Términos y Condiciones
            try:
                await page.locator(SEL_CHECK_TYC).check()
            except Exception:
                # algunos vaadin-checkbox no soportan .check(); hacemos click de respaldo
                await page.locator(SEL_CHECK_TYC).click()

            await asyncio.sleep(1.0)  # el botón se habilita tras marcar TyC

            # 7) Consultar
            await page.locator(SEL_BTN_CONSULTAR).click()
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_CLICK)
            except Exception:
                pass

            # 8) Esperar resultados
            await asyncio.sleep(WAIT_AFTER_RESULTS / 1000)

            # 9) Centrar cámara en resultados (si hay)
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
                                window.scrollTo({top: y, behavior: 'instant'});
                            }""",
                            el
                        )
                        await asyncio.sleep(0.2)
            except Exception:
                pass

            # 10) Screenshot
            await page.screenshot(path=abs_png, full_page=False)

            await ctx.close()
            await browser.close()
            browser = None

        # 11) Registro OK
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
