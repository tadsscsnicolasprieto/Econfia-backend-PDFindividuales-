# core/bots/policia_nacional.py
import os
import re
import asyncio
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente
from core.resolver.captcha_v2 import resolver_captcha_v2

URL = "https://antecedentes.policia.gov.co:7005/WebJudicial/index.xhtml"
nombre_sitio = "policia_nacional"

# Selectores de resultado/loader
SEL_MSG   = "#form\\:mensajeCiudadano"
SEL_PANEL = "#form\\:j_idt8_content"
OVERLAYS  = [".ui-widget-overlay", ".ui-blockui", ".blockUI", ".ui-overlay-visible", ".loader", ".loading"]

# ====================== Helpers de parsing/score ======================

def _extraer_frase_clave(html: str) -> str | None:
    """ Devuelve la <b>...</b> más útil (preferencia a 'TIENE'/'NO TIENE'). """
    if not html:
        return None
    bolds = re.findall(r"<b>(.*?)</b>", html, flags=re.IGNORECASE | re.DOTALL)
    if not bolds:
        return None
    cand = [b.strip() for b in bolds if re.search(r"\bTIENE\b", b, re.I) or re.search(r"\bNO\s+TIENE\b", b, re.I)]
    return (cand[-1] if cand else bolds[-1]).strip()

def _score_y_msg(texto: str) -> tuple[int, str]:
    """ 0 si NO TIENE…, 10 si TIENE…, 2 si ambiguo. """
    t = (texto or "").lower()
    if "no tiene asuntos pendientes" in t:
        return 0, "NO TIENE ASUNTOS PENDIENTES"
    if "tiene asuntos pendientes" in t:
        return 10, "TIENE ASUNTOS PENDIENTES"
    return 2, "Resultado obtenido (revisar captura)."

EXTRA_RENDER_SLEEP_MS = 2500   # ⬅️ tiempo extra antes de devolver el HTML
STABLE_MS = 1200               # ⬅️ texto estable por 1.2s

async def _esperar_resultado(page, timeout_ms: int = 60000) -> str:
    # 0) si podemos, esperar a que se asienten las requests
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    # 1) overlays/loader fuera de escena
    for sel in OVERLAYS:
        try:
            await page.wait_for_selector(sel, state="hidden", timeout=5000)
        except Exception:
            pass

    # 2) contenedor con texto visible
    try:
        await page.wait_for_function(
            """() => {
                const ok = (el) => el && el.innerText && el.innerText.trim().length > 0;
                const msg   = document.querySelector('#form\\\\:mensajeCiudadano');
                const panel = document.querySelector('#form\\\\:j_idt8_content');
                return ok(msg) || ok(panel);
            }""",
            timeout=timeout_ms
        )
    except Exception:
        pass

    # 3) esperar texto ESTABLE por STABLE_MS (evita capturar mientras sigue renderizando)
    try:
        await page.wait_for_function(
            f"""(ms) => new Promise(res => {{
                const start = Date.now();
                let last = '';
                const id = setInterval(() => {{
                    const pick = (sel) => {{
                        const el = document.querySelector(sel);
                        return el ? (el.innerText || '').trim() : '';
                    }};
                    const t = pick('#form\\\\:mensajeCiudadano') || pick('#form\\\\:j_idt8_content');
                    if (t && t === last && (Date.now() - start) >= ms) {{
                        clearInterval(id); res(true);
                    }} else {{
                        last = t;
                    }}
                }}, 200);
            }})""",
            STABLE_MS,
            timeout=timeout_ms
        )
    except Exception:
        pass

    # 4) un pequeño “colchón” extra
    await asyncio.sleep(EXTRA_RENDER_SLEEP_MS / 1000.0)

    # 5) devolver HTML
    try:
        if await page.locator(SEL_MSG).count() > 0:
            return await page.locator(SEL_MSG).inner_html()
    except Exception:
        pass
    try:
        if await page.locator(SEL_PANEL).count() > 0:
            return await page.locator(SEL_PANEL).inner_html()
    except Exception:
        pass
    try:
        return await page.content()
    except Exception:
        return ""

# ====================== Flujo de navegación (igual que tenías) ======================

async def aceptar_terminos(contexto, max_intentos: int = 10):
    """
    Acepta términos, recreando página/contexto en cada intento para limpiar cache/cookies.
    Devuelve la 'pagina' ya ubicada en el formulario o None si falla.
    """
    for intento in range(max_intentos):
        pagina = await contexto.new_page()
        await pagina.goto(URL)
        try:
            await pagina.wait_for_selector("#aceptaOption\\:0", timeout=5000)
            await pagina.click("#aceptaOption\\:0")

            await pagina.wait_for_selector("#continuarBtn", timeout=3000)
            estado = await pagina.get_attribute("#continuarBtn", "aria-disabled")
            if estado == "true":
                await pagina.close()
                continue

            await pagina.click("#continuarBtn")
            await pagina.wait_for_selector("select#cedulaTipo", timeout=5000)
            return pagina
        except PlaywrightTimeoutError:
            await pagina.close()
            continue
        except Exception:
            try:
                await pagina.close()
            except Exception:
                pass
            continue
    return None

async def llenar_formulario(pagina, tipo_doc, cedula):
    """ Llena tipo y número. """
    await pagina.wait_for_selector("#cedulaTipo", timeout=10000)
    await pagina.select_option("#cedulaTipo", (tipo_doc or "CC").lower())
    await pagina.fill("#cedulaInput", str(cedula))

async def resolver_captcha(pagina):
    """ Resuelve reCAPTCHA v2 e inyecta token. """
    await pagina.wait_for_selector("iframe[src*='recaptcha']", timeout=10000)
    iframe = await pagina.query_selector("iframe[src*='recaptcha']")
    src = await iframe.get_attribute("src")
    from urllib.parse import urlparse, parse_qs
    sitekey = (parse_qs(urlparse(src).query).get("k") or [None])[0]
    if not sitekey:
        raise Exception("No se pudo extraer sitekey del captcha")

    token = await resolver_captcha_v2(pagina.url, sitekey)
    if not token:
        raise Exception("CapSolver no devolvió token")

    await pagina.evaluate(
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
    await asyncio.sleep(0.4)

# ====================== BOT: crea Resultado en la BD ======================

async def consultar_policia_nacional(cedula, tipo_doc, consulta_id, max_intentos: int = 20):
    async with async_playwright() as pw:
        navegador = await pw.chromium.launch(headless=True)
        contexto = await navegador.new_context()
        pagina = None
        exito = False
        relative_path = ""

        try:
            pagina = await aceptar_terminos(contexto)
            if not pagina:
                raise Exception("No se pudo acceder al formulario")

            print("✅ Se accedió al formulario correctamente")

            for intento in range(max_intentos):
                try:
                    print(f"🔄 Intento {intento+1} de {max_intentos}...")
                    await llenar_formulario(pagina, tipo_doc, cedula)
                    await resolver_captcha(pagina)

                    # Click en consultar
                    clicked = False
                    for sel in ["#j_idt17", "button:has-text('Consultar')",
                                "input[type='submit'][value*='Consultar']",
                                "input[type='button'][value*='Consultar']"]:
                        try:
                            btn = pagina.locator(sel).first
                            if await btn.count() > 0 and await btn.is_enabled():
                                await btn.click(timeout=1500)
                                clicked = True
                                break
                        except Exception:
                            continue
                    if not clicked:
                        try:
                            await pagina.keyboard.press("Enter")
                        except Exception:
                            pass

                    # Esperar resultado
                    html = await _esperar_resultado(pagina, timeout_ms=60000)
                    frase = _extraer_frase_clave(html) or ""
                    score_calculado, msg_auto = _score_y_msg(frase or html)
                    mensaje_final = frase or msg_auto

                    # Captura
                    relative_folder = os.path.join('resultados', str(consulta_id))
                    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
                    os.makedirs(absolute_folder, exist_ok=True)

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    img_name = f'{nombre_sitio}_{cedula}_{timestamp}.png'
                    absolute_path = os.path.join(absolute_folder, img_name)
                    relative_path = os.path.join(relative_folder, img_name)

                    await pagina.screenshot(path=absolute_path, full_page=True)

                    # Guardar resultado correcto
                    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=score_calculado,
                        estado="Validada",
                        mensaje=mensaje_final,
                        archivo=relative_path
                    )

                    exito = True
                    break

                except Exception:
                    # Si falla un intento, recargar para volver a probar
                    if intento < max_intentos - 1:
                        await pagina.reload()
                        await pagina.wait_for_timeout(2000)
                        continue
                    else:
                        raise   # que caiga en el except general

        except Exception:
            # 🚨 Error general → captura + guardar como "Sin Validar"
            relative_folder = os.path.join('resultados', str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            img_name = f'error_{cedula}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.png'
            absolute_path = os.path.join(absolute_folder, img_name)
            relative_path = os.path.join(relative_folder, img_name)

            try:
                if pagina:
                    await pagina.screenshot(path=absolute_path, full_page=True)
            except Exception:
                relative_path = ""

            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje="No se pudo realizar la consulta en el momento",
                archivo=relative_path
            )

        finally:
            try:
                await navegador.close()
            except Exception:
                pass
