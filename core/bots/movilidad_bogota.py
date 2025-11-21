import os
import re
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente
from core.resolver.captcha_v2 import resolver_captcha_v2

url = "https://webfenix.movilidadbogota.gov.co/#/consulta-pagos"
nombre_sitio = "movilidad_bogota"

TIPO_DOC_MAP = {
    'CC': 'Cédula de ciudadanía',
    'CE': 'CE',
    'NIT': 'NIT',
    'NUIP': 'NUIP',
    'PAS': 'PAS',
    "PEP": "PEP"
}

# Mensajes / selectores clave
NO_RESULTS_TEXT = re.compile(r"no\s*se\s*encontraron\s*registros", re.IGNORECASE)
NO_RESULTS_SEL  = "text=No se encontraron Registros"
# Modal Angular Material
DIALOG_CONTAINER = ".cdk-overlay-container .mat-mdc-dialog-container, .cdk-overlay-container .mat-dialog-container"

RESULT_HINTS = [
    "table", "mat-table", ".mat-table", ".mat-mdc-table",
    ".resultados", ".resultado", ".table-responsive", ".ng-star-inserted .mat-row",
    ".mat-mdc-paginator"
]

async def _resolver_recaptcha(page):
    """
    Si existe reCAPTCHA v2, toma la sitekey del iframe y resuelve con resolver_captcha_v2.
    Inyecta el token en #g-recaptcha-response.
    """
    try:
        iframe = page.locator("iframe[src*='recaptcha/api2/anchor']").first
        if await iframe.count() == 0:
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
                let el = document.getElementById('g-recaptcha-response');
                if (!el) {
                    el = document.createElement('textarea');
                    el.id = 'g-recaptcha-response';
                    el.name = 'g-recaptcha-response';
                    el.style.display = 'none';
                    document.body.appendChild(el);
                }
                el.value = token;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            }""",
            token
        )
        # pequeña pausa para que el front valide el token
        await page.wait_for_timeout(800)
        return True
    except Exception:
        return False

async def _resolver_captcha_aritmetico(page):
    """
    Localiza la tarjeta 'Ingrese la respuesta correcta 8 + 5' y llena el input 'Respuesta'.
    """
    try:
        card = page.locator("text=Ingrese la respuesta correcta").first
        if await card.count() == 0:
            return False

        # leer la expresión entera del contenedor
        cont = card.locator("xpath=ancestor::*[self::div or self::mat-card][1]")
        texto = (await cont.inner_text()) if await cont.count() else (await page.inner_text("body"))
        m = re.search(r"(\d+)\s*\+\s*(\d+)", texto or "")
        if not m:
            return False
        a, b = int(m.group(1)), int(m.group(2))
        r = str(a + b)

        # ubicar input de respuesta (placeholders típicos)
        inp = None
        for sel in [
            "input[placeholder='Respuesta']",
            "input[aria-label='Respuesta']",
            "input.mat-input-element",
            "input.mdc-text-field__input",
        ]:
            loc = cont.locator(sel) if await cont.count() else page.locator(sel)
            if await loc.count() > 0 and await loc.first.is_enabled():
                inp = loc.first
                break

        if not inp:
            return False

        await inp.click()
        await inp.fill("")
        await inp.type(r, delay=20)
        # blur para disparar validaciones
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(300)
        return True
    except Exception:
        return False

async def seleccionar_opcion_mat_select(page, label_text, opcion_text):
    await page.get_by_label(label_text).click()
    await page.locator(f"mat-option >> text={opcion_text}").click()

async def consultar_movilidad_bogota(consulta_id: int, cedula: str, tipo_doc: str):
    navegador = None
    fuente_obj = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{nombre_sitio}': {e}", archivo=""
        )
        return

    try:
        tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())
        if not tipo_doc_val:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj, score=0,
                estado="Sin Validar", mensaje=f"Tipo de documento no soportado: {tipo_doc}", archivo=""
            )
            return

        # Rutas
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
        absolute_path = os.path.join(absolute_folder, screenshot_name)
        relative_path = os.path.join(relative_folder, screenshot_name)

        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()
            await pagina.goto(url, wait_until="domcontentloaded")

            # dar tiempo a Angular
            await pagina.wait_for_timeout(1500)

            # Tipo de identificación
            await seleccionar_opcion_mat_select(pagina, "Tipo de identificación", tipo_doc_val)

            # Número de identificación (con tipeo lento para asegurar binding)
            num_inp = pagina.get_by_label("Número de identificación")
            await num_inp.click()
            await num_inp.fill("")
            await num_inp.type(str(cedula), delay=35)
            await pagina.wait_for_timeout(300)

            # Resolver reCAPTCHA (si está) y el aritmético (si está)
            _ = await _resolver_recaptcha(pagina)
            _ = await _resolver_captcha_aritmetico(pagina)

            # Click en Consultar una vez validados los captchas
            clicked = False
            for sel in [
                "button:has-text('Consultar')",
                "button.mat-raised-button:has-text('Consultar')",
                "button[type='submit']",
            ]:
                try:
                    btn = pagina.locator(sel).first
                    if await btn.count() > 0 and await btn.is_enabled():
                        await btn.click(timeout=2000)
                        clicked = True
                        break
                except Exception:
                    continue

            # Esperar a que aparezca modal o resultados
            await pagina.wait_for_timeout(1200)

            # ¿Modal “No se encontraron Registros”?
            score_final = 0
            mensaje_final = "¡ No se encontraron Registros !"
            try:
                # 1) modal
                modal = pagina.locator(DIALOG_CONTAINER)
                if await modal.count() > 0 and await modal.is_visible():
                    txt = (await modal.inner_text()).strip().lower()
                    if NOTXT := NO_RESULTS_TEXT.search(txt or ""):
                        score_final = 0
                        mensaje_final = '¡ No se encontraron Registros !'
                    else:
                        # Si por alguna razón el modal es informativo de otro tipo, intenta detectar tabla
                        pass
                else:
                    # 2) texto plano en la página
                    page_text = (await pagina.content()) or ""
                    if NO_RESULTS_TEXT.search(page_text):
                        score_final = 0
                        mensaje_final = '¡ No se encontraron Registros !'
                    else:
                        # 3) pistas de resultados
                        found_any = False
                        for hint in RESULT_HINTS:
                            try:
                                loc = pagina.locator(hint)
                                if await loc.count() > 0 and await loc.first.is_visible():
                                    found_any = True
                                    break
                            except Exception:
                                continue

                        if found_any:
                            score_final = 10
                            mensaje_final = "Se encontraron Registros"
            except Exception:
                pass

            # Evidencia con el estado visible (modal o tabla)
            await pagina.screenshot(path=absolute_path, full_page=True)
            await navegador.close()
            navegador = None

        # Registrar
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=relative_path
        )

    except Exception as e:
        absolute_path = locals().get('absolute_path', 'error_screenshot.png')
        relative_path = locals().get('relative_path', '')
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=str(e),
                archivo=relative_path
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
