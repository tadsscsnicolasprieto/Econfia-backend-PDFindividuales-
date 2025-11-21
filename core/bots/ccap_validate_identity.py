# core/bots/ccap_validate_identity.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente
from core.resolver.captcha_v2 import resolver_captcha_v2  # tu helper (capsolver)

NOMBRE_SITIO = "ccap_validate_identity"
URL = "https://app.ccap.org.co/Account/ValidateIdentityCardCertificate"
SITEKEY = "6LfDGawZAAAAAEVnlq41I8B7ZmzpyLv8Kvw830Tw"

# Selectores
SEL_INPUT_CC        = "#CommonFieldsGraduateDto_IdentityCard"
SEL_RECAP_TXT       = "#txtRecaptcha"
SEL_GRESP           = "#g-recaptcha-response"
SEL_BTN_VALID       = "button.btn.btn-primary.btn-block"

# Señales de certificado
SEL_BTN_IMPRIMIR    = "a.btn.btn-default.pull-right[onclick*='PrintFile']"
SEL_CERT_CONTAINER  = "#page-container"

# Mensaje cuando NO hay certificado
SEL_CALLOUT_INFO_P  = ".callout.callout-info p"

# Timings
NAV_TIMEOUT         = 120_000
WAIT_AFTER_NAV_MS   = 12_000
WAIT_AFTER_CLICK_MS = 1_500
EXTRA_RESULT_MS     = 3_000
CERT_TIMEOUT_MS     = 15_000

UA_DESKTOP = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

async def consultar_ccap_validate_identity(consulta_id: int, numero: str):
    """
    CCAP - Validar certificado por cédula:
      - Ingresa cédula
      - Resuelve reCAPTCHA v2 (capsolver)
      - Valida
      - Si hay certificado: screenshot SOLO del #page-container
      - Si no hay: screenshot full page y mensaje del callout de la fuente.
    """
    browser = None
    context = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    try:
        # 2) Carpeta evidencias
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_num = re.sub(r"\s+", "_", (numero or "").strip()) or "consulta"
        png_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"
        abs_png  = os.path.join(absolute_folder, png_name)
        rel_png  = os.path.join(relative_folder, png_name).replace("\\", "/")

        async with async_playwright() as p:
            # 3) Navegador HEADLESS robusto
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1440,1000",
                ],
            )
            context = await browser.new_context(
                accept_downloads=False,
                viewport={"width": 1440, "height": 1000},
                locale="es-CO",
                user_agent=UA_DESKTOP,
            )
            page = await context.new_page()

            # 4) Ir a la página y esperar estable
            await page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV_MS)
            except Exception:
                pass
            await page.wait_for_timeout(800)

            # 5) Escribir número
            await page.locator(SEL_INPUT_CC).wait_for(state="visible", timeout=20_000)
            await page.fill(SEL_INPUT_CC, "")
            await page.type(SEL_INPUT_CC, str(numero or ""), delay=20)

            # 6) Resolver reCAPTCHA v2 (proxyless) e inyectar token con eventos
            token = await resolver_captcha_v2(URL, SITEKEY)
            inject_js = """
                (tok) => {
                  const fire = (el) => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                  };
                  const a = document.querySelector('#txtRecaptcha');
                  if (a) { a.value = tok; fire(a); }
                  let b = document.querySelector('#g-recaptcha-response');
                  if (!b) {
                    b = document.createElement('textarea');
                    b.id = 'g-recaptcha-response';
                    b.name = 'g-recaptcha-response';
                    b.style.display = 'none';
                    (document.body || document.documentElement).appendChild(b);
                  }
                  b.value = tok; fire(b);
                }
            """
            try:
                await page.evaluate(inject_js, token)
            except Exception:
                # segundo intento, por si el DOM aún no tiene el hidden
                await page.wait_for_timeout(600)
                try:
                    await page.evaluate(inject_js, token)
                except Exception:
                    pass

            # 7) Validar
            await page.click(SEL_BTN_VALID)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_CLICK_MS)
            except Exception:
                pass
            await page.wait_for_timeout(EXTRA_RESULT_MS)

            # 8) ¿Hay certificado? (botón Imprimir y/o contenedor del certificado)
            hay_certificado = False
            try:
                btn = page.locator(SEL_BTN_IMPRIMIR).first
                cont = page.locator(SEL_CERT_CONTAINER).first
                btn_visible = await btn.is_visible(timeout=CERT_TIMEOUT_MS)
                cont_visible = await cont.is_visible(timeout=1000) if not btn_visible else True
                hay_certificado = bool(btn_visible or cont_visible)
            except Exception:
                hay_certificado = False

            # 9) Evidencia + mensaje
            if hay_certificado:
                # SOLO el certificado
                try:
                    cont = page.locator(SEL_CERT_CONTAINER).first
                    await cont.wait_for(state="visible", timeout=CERT_TIMEOUT_MS)
                    await cont.scroll_into_view_if_needed()
                    await page.wait_for_timeout(300)
                    await cont.screenshot(path=abs_png)
                    mensaje_final = "Certificado Generado Exitosamente"
                except Exception:
                    # fallback: screenshot normal
                    await page.screenshot(path=abs_png, full_page=False)
                    mensaje_final = "Certificado Generado Exitosamente"
                score_final = 1
            else:
                # Full page + mensaje textual de la fuente (callout)
                await page.evaluate("window.scrollTo(0,0)")
                await page.wait_for_timeout(400)
                await page.screenshot(path=abs_png, full_page=True)
                try:
                    callout = page.locator(SEL_CALLOUT_INFO_P).first
                    if await callout.is_visible(timeout=3000):
                        txt = await callout.inner_text()
                        mensaje_final = (txt or "").strip()
                    else:
                        mensaje_final = "No se encontró certificado"
                except Exception:
                    mensaje_final = "No se encontró certificado"
                score_final = 1  # si prefieres 0 cuando no hay, cámbialo a 0

            # 10) Cerrar
            await context.close()
            await browser.close()
            browser = None
            context = None

        # 11) Guardar resultado
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=rel_png,
        )

    except Exception as e:
        # Evidencia opcional del error (si la página estaba abierta)
        try:
            if context and context.pages:
                pg = context.pages[-1]
                err_png = os.path.join(
                    absolute_folder,
                    f"{NOMBRE_SITIO}_{safe_num}_{ts}_error.png"
                )
                try:
                    await pg.screenshot(path=err_png, full_page=True)
                except Exception:
                    pass
        except Exception:
            pass

        try:
            if context:
                await context.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje=str(e),
            archivo="",
        )
