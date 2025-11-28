import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

import asyncio, re
from urllib.parse import urlparse, parse_qs
from core.resolver.captcha_v2 import resolver_captcha_v2

url = "https://procesojudicial.ramajudicial.gov.co/Justicia21/Administracion/Ciudadanos/frmConsulta.aspx"
nombre_sitio = "tyba"

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

NO_ROWS_RE         = re.compile(r"\bno\s+se\s+encontraron\s+registros\b\.?", re.I)


async def _solve_recaptcha_invisible(page) -> bool:
    """Lee sitekey del iframe anchor y preinyecta token (reCAPTCHA v2 invisible)."""
    try:
        iframe = await page.query_selector("iframe[src*='recaptcha/api2/anchor']")
        if not iframe:
            return False
        src = await iframe.get_attribute("src")
        if not src:
            return False
        sitekey = (parse_qs(urlparse(src).query).get("k") or [None])[0]
        if not sitekey:
            return False

        token = await resolver_captcha_v2(page.url, sitekey)
        if not token:
            return False

        await page.evaluate(
            """(token) => {
                const form = document.querySelector('#aspnetForm') || document.querySelector('form');
                if (!form) return;
                const ensure = (id) => {
                  let t = form.querySelector('#'+id);
                  if (!t) {
                    t = document.createElement('textarea');
                    t.id=id; t.name=id; t.style.display='none';
                    form.appendChild(t);
                  }
                  t.value = token;
                };
                ensure('g-recaptcha-response');
                ensure('g-recaptcha-response-100000');
            }""",
            token
        )
        await asyncio.sleep(0.3)
        return True
    except Exception:
        return False


async def _wait_result_or_alert(page, timeout_ms=20000):
    """Espera alerta o tabla/filas de resultados."""
    loop = asyncio.get_running_loop()
    end = loop.time() + (timeout_ms / 1000)
    while loop.time() < end:
        try:
            if await page.locator(SEL_ALERT_WRAP).is_visible():
                txt = ""
                try:
                    txt = (await page.locator(SEL_ALERT_TEXT).inner_text() or "").strip()
                except Exception:
                    pass
                return ("alert", txt)
        except Exception:
            pass

        try:
            tables = page.locator("table[id*='gv'], table[id*='Grid'], .table")
            if await tables.count() > 0:
                rows = await tables.first.locator("tr").count()
                if rows > 1:
                    return ("results", None)
        except Exception:
            pass

        await asyncio.sleep(0.4)
    return ("unknown", None)


@sync_to_async
def get_fuente(nombre):
    return Fuente.objects.filter(nombre=nombre).first()

@sync_to_async
def guardar_resultado(**kwargs):
    return Resultado.objects.create(**kwargs)

async def tomar_screenshot(pagina, consulta_id, cedula, suffix=""):
    relative_folder = os.path.join('resultados', str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}{suffix}.png"
    absolute_path = os.path.join(absolute_folder, screenshot_name)
    relative_path = os.path.join(relative_folder, screenshot_name)
    await pagina.screenshot(path=absolute_path)
    return relative_path


# ============ SOLO 1 INTENTO, GUARDA SIEMPRE EVIDENCIA ============
async def consultar_tyba(cedula, tipo_doc, nombre: str, apellido: str, consulta_id):
    for intento in range(3):
        page = None
        nav = None
        screenshot_path = ""
        try:
            tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper(), "1")
            async with async_playwright() as p:
                nav = await p.chromium.launch(headless=True)
                page = await nav.new_page()
                await page.goto(url, timeout=60000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                # 1) Pestaña "Ciudadano"
                try:
                    await page.click(SEL_TAB_CIUDAD, timeout=4000)
                except Exception:
                    try:
                        await page.click("li#tabCiudadano", timeout=1500)
                    except Exception:
                        pass
                await page.wait_for_timeout(300)
                # 2) Campos
                await page.wait_for_selector(SEL_TIPO_DOC, timeout=8000)
                await page.wait_for_selector(SEL_NUMERO, timeout=8000)
                # 3) Llenar
                await page.select_option(SEL_TIPO_DOC, value=tipo_doc_val)
                await page.fill(SEL_NUMERO, str(cedula))
                # 4) reCAPTCHA invisible si existe
                await _solve_recaptcha_invisible(page)
                # 5) Submit: Enter y fallback botón
                try:
                    await page.focus(SEL_NUMERO)
                    await page.keyboard.press("Enter")
                except Exception:
                    pass
                try:
                    await page.click(SEL_BTN_CONSULTAR, timeout=2000)
                except Exception:
                    pass
                # 6) Espera de resultado
                kind, payload = await _wait_result_or_alert(page, timeout_ms=22000)
                # centrar alerta para el pantallazo
                if kind == "alert" and await page.locator(SEL_ALERT_WRAP).count():
                    try:
                        el = await page.locator(SEL_ALERT_WRAP).element_handle()
                        if el:
                            await page.evaluate("el => el.scrollIntoView({block:'center'})", el)
                    except Exception:
                        pass
                # 7) Evidencia y guardado
                screenshot_path = await tomar_screenshot(page, consulta_id, cedula)
                fuente_obj = await get_fuente(nombre_sitio)
                if kind == "alert":
                    msg = (payload or "").strip()
                    if NO_ROWS_RE.search(msg):
                        msg_out = "No se encontraron registros."
                        if fuente_obj:
                            await guardar_resultado(
                                consulta_id=consulta_id, fuente=fuente_obj,
                                score=0, estado="Validado",
                                mensaje=msg_out, archivo=screenshot_path
                            )
                    else:
                        if fuente_obj:
                            await guardar_resultado(
                                consulta_id=consulta_id, fuente=fuente_obj,
                                score=0, estado="Validado",
                                mensaje=msg or "Aviso mostrado por la fuente",
                                archivo=screenshot_path
                            )
                elif kind == "results":
                    if fuente_obj:
                        await guardar_resultado(
                            consulta_id=consulta_id, fuente=fuente_obj,
                            score=10, estado="Validado",
                            mensaje="Se han encontrado registros",
                            archivo=screenshot_path
                        )
                else:
                    # No pudimos confirmar nada
                    if fuente_obj:
                        await guardar_resultado(
                            consulta_id=consulta_id, fuente=fuente_obj,
                            score=0, estado="Sin validar",
                            mensaje="No fue posible confirmar el resultado (timeout)",
                            archivo=screenshot_path
                        )
            # Si todo sale bien, salimos del bucle
            break
        except Exception as e:
            # Evidencia en error
            try:
                if page and not screenshot_path:
                    screenshot_path = await tomar_screenshot(page, consulta_id, cedula, suffix="_error")
            except Exception:
                pass
            fuente_obj = await get_fuente(nombre_sitio)
            if fuente_obj:
                await guardar_resultado(
                    consulta_id=consulta_id, fuente=fuente_obj,
                    score=0, estado="Sin validar",
                    mensaje=f"Ocurrió un error al intentar validar la fuente: {e}",
                    archivo=screenshot_path or ""
                )
            # Si es el último intento, no hacemos nada más
            if intento == 2:
                break
        finally:
            try:
                if nav:
                    await nav.close()
            except Exception:
                pass
