# core/bots/bancoproveedores_quien_consulto.py
import os
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from core.models import Resultado, Fuente
from core.resolver.captcha_v2 import resolver_captcha_v2

# Página home con el iframe que tapa todo
BASE_URL = "https://bancoproveedores.serviciocivil.gov.co/index.html"

# URL final del módulo (RECOMENDADO: ir directo)
FINAL_URL = "https://sideap.serviciocivil.gov.co:4443/sideap/publico/bogotaTieneTalento/consultaEstados/index.xhtml"

# Si no se pudiera leer dinámicamente el sitekey, usa este fallback
SITEKEY_FALLBACK = "6LfAjSYTAAAAAFuFHLr7vBAF9zK0Y2USc5DkoVqR"

NOMBRE_SITIO = "banco_proveedores_consulta_estados"

TIPO_DOC_LABEL = {
    "CC":  "CÉDULA DE CIUDADANÍA",
    "CE":  "CÉDULA DE EXTRANJERÍA",
    "PEP": "PERMISO ESPECIAL DE PERMANENCIA - PEP",
    "PPT": "PERMISO POR PROTECCIÓN TEMPORAL - PPT",
}

async def _crear_resultado(consulta_id, fuente, estado, mensaje, archivo, score=1):
    rel = archivo.replace("\\", "/") if archivo else ""
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente,
        estado=estado,
        mensaje=mensaje,
        archivo=rel,
        score=score,
    )

async def consultar_quien_consulto(consulta_id: int, numero: str, tipo_doc: str):
    """
    Flujo robusto:
    - Intenta ir DIRECTO a FINAL_URL.
    - Si falla, abre BASE_URL y hace click en el botón "Consultar" dentro del iframe #sideap.
    - En el formulario: selecciona tipo doc, escribe número, resuelve reCAPTCHA v2 (sitekey dinámico),
      envía, captura mensaje (growl) o datos (codigoprint), y guarda screenshot full-page.
    - Score = 1 en todos los casos (según requerimiento del usuario).
    """
    fuente = await sync_to_async(lambda: Fuente.objects.filter(nombre=NOMBRE_SITIO).first())()
    if not fuente:
        await _crear_resultado(consulta_id, None, "Sin Validar",
                               f"No existe Fuente con nombre='{NOMBRE_SITIO}'", "", score=0)
        return

    # Rutas de salida
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_png = f"banco_proveedores_quien_consulto_{numero}_{ts}.png"
    abs_png = os.path.join(absolute_folder, base_png)
    rel_png = os.path.join(relative_folder, base_png)

    browser = context = page = None
    try:
        if tipo_doc not in TIPO_DOC_LABEL:
            await _crear_resultado(consulta_id, fuente, "Sin Validar",
                                   f"Tipo de documento no soportado: {tipo_doc!r}", "", score=0)
            return

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                locale="es-CO",
                viewport={"width": 1366, "height": 1000}
            )
            page = await context.new_page()

            # --- 1) Intento: ir directo al módulo final ---
            try:
                await page.goto(FINAL_URL, wait_until="domcontentloaded", timeout=60000)
                # Si carga bien, debería contener los elementos del formulario
                await page.wait_for_selector("#consultarEstados\\:inputNumeroDocumento", timeout=8000)
            except Exception:
                # --- 2) Fallback: navegar por el home y el iframe ---
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
                # Cierra modal si existe
                try:
                    await page.locator('button[data-dismiss="modal"]').click(timeout=4000)
                except Exception:
                    pass

                # El iframe tapa todo. Haz click dentro del iframe en el botón "Consultar"
                try:
                    sideap_frame = page.frame_locator("#sideap")
                    # Opción A: botón tipo span
                    boton = sideap_frame.locator("span.btn_home_talento")
                    await boton.click(timeout=12000)
                except PWTimeout:
                    # Opción B: link de texto (por si cambió el botón)
                    try:
                        link = page.frame_locator("#sideap").locator('a[href="consultaEstados/index.xhtml"]')
                        await link.click(timeout=12000)
                    except Exception as e:
                        raise RuntimeError(f"No se pudo interactuar con el iframe #sideap: {e}")

                # Esperar navegación al módulo
                await page.wait_for_url("**/consultaEstados/**", timeout=20000)
                await page.wait_for_selector("#consultarEstados\\:inputNumeroDocumento", timeout=10000)

            # --- 3) Formulario: seleccionar tipo doc y llenar número ---
            label = TIPO_DOC_LABEL[tipo_doc]

            # Abre dropdown de PrimeFaces (label) o el propio select
            try:
                await page.click('#consultarEstados\\:selectTipoDocumento_label', timeout=12000)
            except Exception:
                try:
                    await page.click('#consultarEstados\\:selectTipoDocumento', timeout=12000)
                except Exception:
                    pass

            item = page.locator("li.ui-selectonemenu-item", has_text=label).first
            await item.wait_for(state="visible", timeout=8000)
            await item.click()

            await page.fill('#consultarEstados\\:inputNumeroDocumento', str(numero))

            # --- 4) reCAPTCHA v2: detectar sitekey dinámicamente ---
            sitekey = None
            try:
                sitekey = await page.get_attribute(".g-recaptcha", "data-sitekey")
            except Exception:
                pass
            if not sitekey:
                sitekey = SITEKEY_FALLBACK

            token = await resolver_captcha_v2(page.url, sitekey)

            # Inyectar token en g-recaptcha-response
            await page.evaluate(
                """(tok) => {
                    let el = document.getElementById('g-recaptcha-response');
                    if (!el) {
                        el = document.createElement('textarea');
                        el.id = 'g-recaptcha-response';
                        el.name = 'g-recaptcha-response';
                        el.style.display = 'none';
                        document.body.appendChild(el);
                    }
                    el.value = tok;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                token,
            )

            # --- 5) Enviar (varios selectores por si cambian ids/clases) ---
            clicked = False
            for sel in [
                '#consultarEstados\\:btnRegistrod',
                "button:has-text('Consultar')",
                "a:has-text('Consultar')",
                "span:has-text('Consultar')"
            ]:
                try:
                    await page.locator(sel).first.click(timeout=4000)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                raise RuntimeError("No se encontró el botón de 'Consultar' en el formulario.")

            # Deja que renderice growl/panel
            await page.wait_for_timeout(1200)

            # --- 6) Leer mensaje/resultado ---
            mensaje = ""

            # a) Growl PrimeFaces (varía la clase según versión)
            try:
                growl = page.locator(".ui-growl-message, .ui-growl-item-container").first
                if await growl.is_visible():
                    title_el = growl.locator(".ui-growl-title").first
                    detail_el = growl.locator("p, .ui-growl-message p").first
                    title = (await title_el.inner_text()).strip() if await title_el.count() else "Mensaje"
                    detail = (await detail_el.inner_text()).strip() if await detail_el.count() else ""
                    mensaje = f"{title}: {detail}".strip()
            except Exception:
                pass

            # b) Panel de resultados con .codigoprint
            if not mensaje:
                try:
                    await page.locator(".codigoprint").first.wait_for(state="visible", timeout=10000)
                    numeros = await page.locator(".codigoprint .numero").all_inner_texts()
                    numeros = [n.strip() for n in numeros if n.strip()]
                    num_doc = numeros[0] if len(numeros) > 0 else ""
                    num_ins = numeros[1] if len(numeros) > 1 else ""
                    mensaje = f"NÚMERO DOCUMENTO: {num_doc} | NÚMERO INSCRIPCIÓN: {num_ins}".strip()
                except Exception:
                    pass

            if not mensaje:
                mensaje = "No se pudo determinar el estado de la consulta (revise la evidencia)."

            # --- 7) Evidencia ---
            await page.wait_for_timeout(500)
            await page.screenshot(path=abs_png, full_page=True)

            await context.close()
            await browser.close()
            context = browser = None

        # --- 8) Persistencia ---
        await _crear_resultado(consulta_id, fuente, "Validada", mensaje, rel_png, score=1)

    except Exception as e:
        # Evidencia de error
        try:
            if page:
                try:
                    await page.screenshot(path=abs_png, full_page=True)
                except Exception:
                    pass
        except Exception:
            pass

        await _crear_resultado(
            consulta_id, fuente, "Sin Validar",
            f"{type(e).__name__}: {e}",
            rel_png if os.path.exists(abs_png) else "",
            score=0
        )
    finally:
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
