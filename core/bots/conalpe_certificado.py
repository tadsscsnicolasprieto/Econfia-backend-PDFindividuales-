# bots/conalpe_certificado.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente
from core.resolver.captcha_v2 import resolver_captcha_v2

NOMBRE_SITIO = "conalpe_certificado"
URL = "https://www.conalpe.gov.co/tramitesyservicios/certificado"
SITEKEY = "6LfxFFMaAAAAADxENUTb-3ZiVBFbmb9gcsznZgg5"

SEL_INPUT_DOC   = 'input[formcontrolname="id"]'
SEL_BTN_SUBMIT  = "button[type='submit']:has-text('Solicitar')"   # <- importante
SEL_POPUP_CLOSE_IMG = "img[src*='cerrar_modal']"

# señales (incluye el snackbar del ejemplo)
SEL_RESULT_HINTS = [
    "div.mat-snack-bar-container",
    "div.mat-mdc-snack-bar-label",
    "text=No se encontro el registro",
    "text=No se encontró el registro",
    "mat-card", ".mat-card"
]

WAIT_AFTER_NAV     = 15000
WAIT_AFTER_ACTION  = 2500
EXTRA_RESULT_SLEEP = 1200


async def _inject_recaptcha_token(page, token: str):
    await page.evaluate(
        """(tok) => {
            let ta = document.querySelector('textarea#g-recaptcha-response');
            if (!ta) {
                ta = document.createElement('textarea');
                ta.id = 'g-recaptcha-response';
                ta.name = 'g-recaptcha-response';
                ta.style = 'display:none';
                document.body.appendChild(ta);
            }
            ta.value = tok;
            ta.dispatchEvent(new Event('input', { bubbles: true }));
            ta.dispatchEvent(new Event('change', { bubbles: true }));

            const ta2 = document.querySelector('textarea[name="g-recaptcha-response-100000"]');
            if (ta2) {
                ta2.value = tok;
                ta2.dispatchEvent(new Event('input', { bubbles: true }));
                ta2.dispatchEvent(new Event('change', { bubbles: true }));
            }

            const hidden = document.querySelector('input[name="recaptcha"], input[formcontrolname="recaptcha"], input[name="g-recaptcha-response"]');
            if (hidden) {
                hidden.value = tok;
                hidden.dispatchEvent(new Event('input', { bubbles: true }));
                hidden.dispatchEvent(new Event('change', { bubbles: true }));
            }

            // fallback: habilitar botón si el form no reaccionó
            const btn = document.querySelector("button[type='submit']");
            if (btn) {
                btn.disabled = false;
                btn.classList.remove('mat-button-disabled');
                btn.setAttribute('aria-disabled', 'false');
            }
        }""",
        token
    )


async def consultar_conalpe_certificado(consulta_id: int, numero: str):
    browser = None
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    try:
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
                viewport={"width": 1440, "height": 1200},
                locale="es-CO"
            )
            page = await ctx.new_page()

            # 1) Página
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # 2) Cerrar popup
            try:
                await page.keyboard.press("Escape"); await asyncio.sleep(0.3)
            except Exception:
                pass
            try:
                if await page.locator(SEL_POPUP_CLOSE_IMG).count() > 0:
                    await page.locator(SEL_POPUP_CLOSE_IMG).first.click()
                    await asyncio.sleep(0.2)
                await page.locator("button[aria-label='Close'], button.mat-dialog-close").first.click()
            except Exception:
                pass

            # 3) Documento
            await page.wait_for_selector(SEL_INPUT_DOC, state="visible", timeout=15000)
            inp = page.locator(SEL_INPUT_DOC)
            await inp.click(force=True)
            try: await inp.fill("")
            except Exception: pass
            await inp.type(str(numero or ""), delay=25)

            # 4) reCAPTCHA
            token = await resolver_captcha_v2(URL, SITEKEY)
            await _inject_recaptcha_token(page, token)

            # 5) Solicitar
            await page.locator(SEL_BTN_SUBMIT).click()
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_ACTION)
            except Exception:
                pass
            await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)

            # 6) Esperar snackbar / resultado y encuadrar para el pantallazo
            snackbar = page.locator("div.mat-snack-bar-container, div.mat-mdc-snack-bar-label")
            form = page.locator("form").first
            try:
                await snackbar.wait_for(state="visible", timeout=2500)
            except Exception:
                pass

            try:
                handle = await (form.element_handle() or page.locator(SEL_BTN_SUBMIT).element_handle())
                if handle:
                    await page.evaluate(
                        """(el) => {
                            const r = el.getBoundingClientRect();
                            const y = r.top + window.scrollY - 180; // queda como tu ejemplo
                            window.scrollTo({ top: y, behavior: 'instant' });
                        }""",
                        handle
                    )
                    await asyncio.sleep(0.2)
            except Exception:
                pass

            try:
                if await snackbar.count() > 0:
                    await snackbar.scroll_into_view_if_needed()
                    await asyncio.sleep(0.2)
            except Exception:
                pass

            # 7) Screenshot (vista actual)
            await page.screenshot(path=abs_png, full_page=False)

            await ctx.close()
            await browser.close()
            browser = None

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Validada",
            mensaje="",  # ya queda visible el snackbar si apareció
            archivo=rel_png
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj, score=0,
                estado="Sin Validar", mensaje=str(e), archivo=""
            )
        finally:
            if browser:
                try: await browser.close()
                except Exception: pass
