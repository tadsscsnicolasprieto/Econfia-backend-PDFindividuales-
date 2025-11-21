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
import re

def parsear_policia(html: str, tipo_doc: str = "CC"):
    """
    Extrae datos básicos del HTML de la Policía Nacional.
    Retorna un dict listo para JSON.
    """
    # Cédula
    cedula_match = re.search(r"Cédula de Ciudadanía Nº <b>(\d+)</b>", html)
    cedula = cedula_match.group(1).strip() if cedula_match else None

    # Nombre completo
    nombre_match = re.search(r"Apellidos y Nombres:\s*<b>(.*?)</b>", html)
    nombre_completo = nombre_match.group(1).strip() if nombre_match else ""

    # Separa en apellido(s) y nombre(s)
    partes = nombre_completo.split()
    apellido = " ".join(partes[0:2]) if len(partes) >= 2 else ""
    nombre = " ".join(partes[2:]) if len(partes) > 2 else ""

    datos = {
        "cedula": cedula,
        "tipo_doc": tipo_doc,
        "nombre": nombre,
        "apellido": apellido,
        "fecha_nacimiento": None,   # ❌ no viene en el HTML
        "fecha_expedicion": None,   # ❌ no viene en el HTML
        "tipo_persona": "natural",  # fijo
        "sexo": None                # ❌ no viene en el HTML
    }

    return datos

# ====================== BOT: crea Resultado en la BD ======================

async def consultar_policia_nacional(cedula, tipo_doc, max_intentos: int = 20):
    async with async_playwright() as pw:
        navegador = await pw.chromium.launch(headless=True)
        contexto = await navegador.new_context()
        pagina = None

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

                    # Esperar resultado y parsear
                    html = await _esperar_resultado(pagina, timeout_ms=60000)
                    datos = parsear_policia(html, tipo_doc=tipo_doc)

                    return datos  # ✅ JSON con la estructura pedida

                except Exception:
                    if intento < max_intentos - 1:
                        await pagina.reload()
                        await pagina.wait_for_timeout(2000)
                        continue
                    else:
                        raise   # deja caer al except general

        except Exception:
            return {
                "cedula": str(cedula),
                "tipo_doc": tipo_doc,
                "nombre": None,
                "apellido": None,
                "fecha_nacimiento": None,
                "fecha_expedicion": None,
                "tipo_persona": "natural",
                "sexo": None
            }

        finally:
            try:
                await navegador.close()
            except Exception:
                pass