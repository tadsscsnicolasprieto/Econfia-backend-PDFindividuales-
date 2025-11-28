# bots/tyba_consulta_retry_submit.py
import os
import re
import asyncio
import random
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente
from core.resolver.captcha_v2 import resolver_captcha_v2

URL = "https://procesojudicial.ramajudicial.gov.co/Justicia21/Administracion/Ciudadanos/frmConsulta.aspx"
NOMBRE_SITIO = "tyba"

TIPO_DOC_MAP = {
    'CC': '1',
    'CE': '3',
    'NIT': '2',
    'PAS': '5',
}

SEL_TAB_CIUDAD     = "li#tabCiudadano a[href='#Ciudadano'], a[href='#Ciudadano']"
SEL_TIPO_DOC       = 'select[name="ctl00$MainContent$ddlTipoDocumento"]'
SEL_NUMERO         = 'input[id="MainContent_txtNumeroIdentificacion"]'
SEL_BTN_CONSULTAR  = 'input[id="MainContent_btnConsultar"]'

SEL_ALERT_WRAP     = "#MainContent_UC_MensajeInformativo_divAdvertencia"
SEL_ALERT_TEXT     = "#MainContent_UC_MensajeInformativo_lblMensajes"

NO_ROWS_RE = re.compile(r"\bno\s+se\s+encontraron\s+registros\b\.?", re.I)


@sync_to_async
def _get_fuente(nombre):
    return Fuente.objects.filter(nombre=nombre).first()


@sync_to_async
def _crear_resultado(consulta_id, fuente, score, estado, mensaje, archivo):
    return Resultado.objects.create(
        consulta_id=consulta_id,
        fuente=fuente,
        score=score,
        estado=estado,
        mensaje=mensaje,
        archivo=archivo,
    )


async def _solve_recaptcha_invisible(page, max_attempts=1):
    """
    Detecta iframe de reCAPTCHA v2 (anchor), extrae sitekey y pide token al resolver.
    Inserta token en textarea(s) g-recaptcha-response. Devuelve token o None.
    """
    try:
        iframe = await page.query_selector("iframe[src*='recaptcha/api2/anchor']")
        if not iframe:
            return None
        src = await iframe.get_attribute("src")
        if not src:
            return None
        sitekey = (parse_qs(urlparse(src).query).get("k") or [None])[0]
        if not sitekey:
            return None

        # resolver captcha (puede tardar)
        token = await resolver_captcha_v2(page.url, sitekey)
        if not token:
            return None

        # insertar token en textareas del formulario
        await page.evaluate(
            """(token) => {
                const form = document.querySelector('#aspnetForm') || document.querySelector('form');
                if (!form) return;
                const ensure = (id) => {
                  let t = form.querySelector('#'+id);
                  if (!t) {
                    t = document.createElement('textarea');
                    t.id = id; t.name = id; t.style.display = 'none';
                    form.appendChild(t);
                  }
                  t.value = token;
                };
                ensure('g-recaptcha-response');
                ensure('g-recaptcha-response-100000');
                // disparar eventos para que el front procese el valor
                const el = form.querySelector('#g-recaptcha-response') || form.querySelector('textarea[name=\"g-recaptcha-response\"]');
                if (el) {
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                }
            }""",
            token
        )
        # dar margen para que el front procese el token
        await asyncio.sleep(0.6 + random.random() * 0.6)
        return token
    except Exception:
        return None


async def _wait_result_or_alert(page, timeout_ms=20000):
    loop = asyncio.get_running_loop()
    end = loop.time() + (timeout_ms / 1000)
    while loop.time() < end:
        # alerta informativa
        try:
            wrap = page.locator(SEL_ALERT_WRAP)
            if await wrap.count() > 0 and await wrap.first.is_visible():
                txt = ""
                try:
                    txt = (await page.locator(SEL_ALERT_TEXT).first.inner_text() or "").strip()
                except Exception:
                    pass
                return ("alert", txt)
        except Exception:
            pass

        # tablas de resultado
        try:
            tables = page.locator("table[id*='gv'], table[id*='Grid'], .table")
            if await tables.count() > 0:
                rows = await tables.first.locator("tr").count()
                if rows > 1:
                    return ("results", None)
        except Exception:
            pass

        await asyncio.sleep(0.35)
    return ("unknown", None)


async def _tomar_screenshot(page, consulta_id, numero, suffix=""):
    relative_folder = os.path.join('resultados', str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_num = re.sub(r"\s+", "_", (numero or "").strip()) or "doc"
    screenshot_name = f"{NOMBRE_SITIO}_{safe_num}_{timestamp}{suffix}.png"
    absolute_path = os.path.join(absolute_folder, screenshot_name)
    relative_path = os.path.join(relative_folder, screenshot_name)
    try:
        await page.screenshot(path=absolute_path, full_page=True)
    except Exception:
        try:
            await page.screenshot(path=absolute_path)
        except Exception:
            pass
    return relative_path


async def _submit_form_robust(page):
    """
    Intenta enviar el formulario de varias formas: click en botón, form.submit(), dispatchEvent('submit').
    """
    # 1) click en botón si existe
    try:
        if await page.locator(SEL_BTN_CONSULTAR).count() > 0:
            await page.locator(SEL_BTN_CONSULTAR).first.scroll_into_view_if_needed()
            await page.locator(SEL_BTN_CONSULTAR).first.click(timeout=4000)
            return True
    except Exception:
        pass

    # 2) intentar dispatch submit sobre el form
    try:
        await page.evaluate("""
            () => {
                const form = document.querySelector('#aspnetForm') || document.querySelector('form');
                if (form) {
                    try { form.dispatchEvent(new Event('submit', {bubbles:true, cancelable:true})); } catch(e) {}
                    try { form.submit(); } catch(e) {}
                }
            }
        """)
        return True
    except Exception:
        pass

    # 3) fallback: pulsar Enter en el campo número
    try:
        await page.keyboard.press("Enter")
        return True
    except Exception:
        pass

    return False


async def consultar_tyba(consulta_id: int, tipo_doc: str, numero: str):
    browser = None
    context = None
    page = None
    screenshot_rel = ""
    try:
        fuente_obj = await _get_fuente(NOMBRE_SITIO)
        tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper(), "1")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
            context = await browser.new_context(viewport={"width": 1366, "height": 900}, locale="es-CO")
            try:
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'languages', { get: () => ['es-CO','es'] });
                    window.navigator.chrome = { runtime: {} };
                """)
            except Exception:
                pass

            page = await context.new_page()
            await page.goto(URL, timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            # pestaña ciudadano
            try:
                if await page.locator(SEL_TAB_CIUDAD).count() > 0:
                    await page.locator(SEL_TAB_CIUDAD).first.click(timeout=4000)
                else:
                    if await page.locator("li#tabCiudadano").count() > 0:
                        await page.locator("li#tabCiudadano").first.click(timeout=1500)
            except Exception:
                pass
            await page.wait_for_timeout(300)

            # esperar campos
            await page.wait_for_selector(SEL_TIPO_DOC, timeout=8000)
            await page.wait_for_selector(SEL_NUMERO, timeout=8000)

            # llenar tipo y numero
            try:
                await page.select_option(SEL_TIPO_DOC, value=tipo_doc_val)
            except Exception:
                try:
                    await page.evaluate("(sel, v) => { const s = document.querySelector(sel); if (s) s.value = v; }", SEL_TIPO_DOC, tipo_doc_val)
                except Exception:
                    pass

            try:
                await page.fill(SEL_NUMERO, str(numero))
            except Exception:
                try:
                    await page.evaluate("(sel, v) => { const el = document.querySelector(sel); if (el) { el.value = v; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); } }", SEL_NUMERO, str(numero))
                except Exception:
                    pass

            # Intentar resolver reCAPTCHA invisible y enviar; si el sitio responde "captcha inválido" reintentar 1 vez
            max_solve_attempts = 2
            solved_and_submitted = False
            for solve_try in range(1, max_solve_attempts + 1):
                token = await _solve_recaptcha_invisible(page)
                # si no hay recaptcha, token puede ser None pero igual intentamos enviar
                # espera humana corta antes de enviar
                await asyncio.sleep(0.6 + random.random() * 0.8)

                submitted = await _submit_form_robust(page)
                if not submitted:
                    # guardar diagnóstico y abortar este intento
                    html = await page.content()
                    # intentar guardar html en la carpeta de resultados
                    try:
                        relative_folder = os.path.join('resultados', str(consulta_id))
                        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
                        os.makedirs(absolute_folder, exist_ok=True)
                        with open(os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{numero}_no_submit.html"), "w", encoding="utf-8") as fh:
                            fh.write(html)
                    except Exception:
                        pass
                    # intentar siguiente solve (si aplica) o salir
                    continue

                # esperar respuesta corta
                await asyncio.sleep(1.0)

                # comprobar si apareció mensaje de captcha inválido (swal o alert wrapper)
                captcha_rejected = False
                try:
                    if await page.locator(".swal2-popup .swal2-html-container").count() > 0:
                        texts = await page.locator(".swal2-popup .swal2-html-container").all_text_contents()
                        joined = " ".join(t.lower() for t in texts)
                        if "captcha" in joined or "no es válido" in joined or "no válido" in joined:
                            captcha_rejected = True
                            # cerrar popup si hay botón aceptar
                            try:
                                if await page.locator(".swal2-confirm").count() > 0:
                                    await page.locator(".swal2-confirm").first.click()
                            except Exception:
                                pass
                    # también revisar el alert wrapper textual
                    if not captcha_rejected:
                        try:
                            if await page.locator(SEL_ALERT_WRAP).count() > 0 and await page.locator(SEL_ALERT_WRAP).first.is_visible():
                                txt = (await page.locator(SEL_ALERT_TEXT).first.inner_text() or "").lower()
                                if "captcha" in txt or "no es válido" in txt or "no válido" in txt:
                                    captcha_rejected = True
                                # si es otro aviso, lo procesaremos luego
                        except Exception:
                            pass
                except Exception:
                    pass

                if captcha_rejected and solve_try < max_solve_attempts:
                    # refrescar la página parcialmente: forzar recarga del iframe captcha o recargar la página
                    try:
                        # intentar recargar solo la imagen del captcha si existe
                        await page.evaluate("""
                            () => {
                                const iframe = document.querySelector("iframe[src*='recaptcha']");
                                if (iframe) {
                                    const src = iframe.getAttribute('src') || '';
                                    iframe.setAttribute('src', src.split('?')[0] + '?_=' + Date.now());
                                } else {
                                    // fallback: recargar la página completa
                                    // location.reload();
                                }
                            }
                        """)
                    except Exception:
                        pass
                    # esperar un poco y reintentar resolver
                    await asyncio.sleep(1.0 + random.random() * 0.8)
                    continue  # siguiente intento de solve
                else:
                    solved_and_submitted = True
                    break

            # esperar resultado o alerta definitivo
            kind, payload = await _wait_result_or_alert(page, timeout_ms=22000)

            # centrar alerta si existe
            try:
                if kind == "alert" and await page.locator(SEL_ALERT_WRAP).count() > 0:
                    el = await page.locator(SEL_ALERT_WRAP).first.element_handle()
                    if el:
                        await page.evaluate("(el) => el.scrollIntoView({block:'center'})", el)
            except Exception:
                pass

            # tomar screenshot siempre antes de crear Resultado
            screenshot_rel = await _tomar_screenshot(page, consulta_id, numero)

            # guardar resultado según detección
            if fuente_obj is None:
                if kind == "alert":
                    msg = (payload or "").strip()
                    mensaje = "Aviso de la fuente: " + (msg or "Sin texto")
                    await _crear_resultado(consulta_id, None, 0, "Sin Validar", mensaje, screenshot_rel)
                elif kind == "results":
                    await _crear_resultado(consulta_id, None, 10, "Sin Validar", "Se han encontrado registros", screenshot_rel)
                else:
                    await _crear_resultado(consulta_id, None, 0, "Sin Validar", "No fue posible confirmar el resultado (timeout)", screenshot_rel)
            else:
                if kind == "alert":
                    msg = (payload or "").strip()
                    if NO_ROWS_RE.search(msg):
                        await _crear_resultado(consulta_id, fuente_obj, 0, "Validado", "No se encontraron registros.", screenshot_rel)
                    else:
                        await _crear_resultado(consulta_id, fuente_obj, 0, "Validado", msg or "Aviso mostrado por la fuente", screenshot_rel)
                elif kind == "results":
                    await _crear_resultado(consulta_id, fuente_obj, 10, "Validado", "Se han encontrado registros", screenshot_rel)
                else:
                    await _crear_resultado(consulta_id, fuente_obj, 0, "Sin validar", "No fue posible confirmar el resultado (timeout)", screenshot_rel)

            # cerrar contexto y navegador de forma segura
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

    except Exception as e:
        # en error, tomar evidencia si no existe y guardar resultado de error
        try:
            if page and not screenshot_rel:
                screenshot_rel = await _tomar_screenshot(page, consulta_id, numero, suffix="_error")
        except Exception:
            pass
        fuente_obj = await _get_fuente(NOMBRE_SITIO)
        await _crear_resultado(consulta_id, fuente_obj, 0, "Sin validar", f"Ocurrió un error al intentar validar la fuente: {e}", screenshot_rel or "")
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
