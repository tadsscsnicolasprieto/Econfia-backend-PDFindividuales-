# bots/interpol_just_screenshot.py
import os
import asyncio
import random
import json
from datetime import datetime
from typing import Optional, List

from playwright.async_api import async_playwright, Page, BrowserContext
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente

INTERPOL_URL = "https://www.interpol.int/es/Como-trabajamos/Notificaciones/Notificaciones-rojas/Ver-las-notificaciones-rojas"
NOMBRE_SITIO = "interpol"

# Configuración por defecto (puedes sobreescribir con variables de entorno)
MAX_ATTEMPTS = int(os.getenv("INTERPOL_MAX_ATTEMPTS", "4"))
HEADLESS_DEFAULT = os.getenv("INTERPOL_HEADLESS", "false").lower() == "true"
STORAGE_STATE_PATH = os.getenv("INTERPOL_STORAGE_STATE")  # ruta opcional a storageState.json
PROXY_LIST = os.getenv("INTERPOL_PROXY_LIST")  # opcional: JSON array string o CSV "http://u:p@host:port,..." 
USER_AGENT_LIST = os.getenv("INTERPOL_UA_LIST")  # opcional: JSON array string or CSV

# Selectores
SEL_FORNAME = "#forename"
SEL_NAME = "#name"
SEL_SUBMIT = "#submit"
SEL_RESULTS_BLOCK = ".search__resultsBlock--results.js-gallery"
SEL_RESULTS_TEXT = ".search__resultsBlock--results.js-gallery p.lightText"
SEL_COOKIE_BTN = "button#onetrust-accept-btn-handler, button:has-text('Aceptar'), button:has-text('Accept')"

# DB helpers
@sync_to_async
def _get_fuente(nombre: str) -> Optional[Fuente]:
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

# Utilidades
def _parse_list_env(value: Optional[str]) -> List[str]:
    if not value:
        return []
    value = value.strip()
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x]
    except Exception:
        pass
    return [p.strip() for p in value.split(",") if p.strip()]

def _default_user_agents() -> List[str]:
    return [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    ]

def _is_block_page_text(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    checks = [
        "access to this site was denied",
        "acceso a este sitio fue denegado",
        "refusé pour des raisons de sécurité",
        "denied for security reasons",
        "accesso a questo sito è stato negato",
    ]
    return any(c in low for c in checks)

async def _human_like_actions(page: Page):
    try:
        w, h = page.viewport_size["width"], page.viewport_size["height"]
        for _ in range(random.randint(1, 3)):
            await page.mouse.move(random.randint(50, w - 50), random.randint(50, h - 50), steps=random.randint(5, 15))
            await asyncio.sleep(random.uniform(0.15, 0.45))
        await page.evaluate("() => window.scrollBy(0, Math.floor(window.innerHeight * 0.2))")
        await asyncio.sleep(random.uniform(0.2, 0.6))
    except Exception:
        pass

async def _save_screenshot_only(absolute_folder: str, prefix: str, page: Optional[Page]):
    """
    Guarda únicamente un PNG final. No guarda HTML.
    Devuelve la ruta relativa (desde MEDIA_ROOT) del PNG.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_name = f"{prefix}_{ts}.png"
    absolute_png = os.path.join(absolute_folder, png_name)
    try:
        if page:
            await page.screenshot(path=absolute_png, full_page=True)
    except Exception:
        try:
            if page:
                await page.screenshot(path=absolute_png)
        except Exception:
            # crear archivo vacío para mantener consistencia
            try:
                open(absolute_png, "wb").close()
            except Exception:
                pass
    # devolver ruta relativa para almacenar en BD
    # relative folder se asume como last segment de absolute_folder dentro MEDIA_ROOT
    rel = os.path.relpath(absolute_png, settings.MEDIA_ROOT)
    return rel

# Función principal
async def consultar_interpol(consulta_id: int, nombre: str, apellido: str, cedula: str):
    """
    Versión que solo guarda capturas PNG al finalizar cada intento y no guarda HTML.
    Mantiene reintentos, rotación de UA/proxy y storageState opcional.
    """
    # preparar carpeta
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    # cargar configuraciones
    ua_list = _parse_list_env(USER_AGENT_LIST) or _default_user_agents()
    proxy_list = _parse_list_env(PROXY_LIST)
    storage_state = STORAGE_STATE_PATH if STORAGE_STATE_PATH and os.path.exists(STORAGE_STATE_PATH) else None
    headless_env = HEADLESS_DEFAULT

    fuente_obj = await _get_fuente(NOMBRE_SITIO)

    last_exception = None
    attempt = 0

    while attempt < MAX_ATTEMPTS:
        attempt += 1
        browser = None
        context: Optional[BrowserContext] = None
        page = None
        ua = random.choice(ua_list)
        proxy = random.choice(proxy_list) if proxy_list else None

        try:
            async with async_playwright() as p:
                launch_kwargs = {"headless": headless_env, "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"]}
                if proxy:
                    launch_kwargs["proxy"] = {"server": proxy}
                browser = await p.chromium.launch(**launch_kwargs)

                context_kwargs = {
                    "viewport": {"width": 1366, "height": 768},
                    "user_agent": ua,
                    "locale": "es-ES",
                    "ignore_https_errors": True,
                    "extra_http_headers": {"Accept-Language": "es-ES,es;q=0.9,en;q=0.8", "Referer": "https://www.google.com/"},
                }
                if storage_state:
                    context_kwargs["storage_state"] = storage_state

                context = await browser.new_context(**context_kwargs)

                # stealth init
                try:
                    await context.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                        Object.defineProperty(navigator, 'languages', { get: () => ['es-ES','es'] });
                        window.navigator.chrome = { runtime: {} };
                    """)
                except Exception:
                    pass

                page = await context.new_page()
                page.set_default_navigation_timeout(120000)
                page.set_default_timeout(30000)

                # navegar
                try:
                    await page.goto(INTERPOL_URL, wait_until="domcontentloaded", timeout=120000)
                except Exception:
                    await asyncio.sleep(1.0)
                    await page.goto(INTERPOL_URL, wait_until="domcontentloaded", timeout=120000)

                # esperar recursos
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    await asyncio.sleep(1.0)

                # detectar bloqueo por texto en body (sin guardar HTML)
                try:
                    body_text = await page.inner_text("body")
                except Exception:
                    body_text = ""

                if _is_block_page_text(body_text):
                    # tomar captura final de bloqueo y registrar
                    rel_png = await _save_screenshot_only(absolute_folder, f"attempt{attempt}_blocked", page)
                    await _crear_resultado(consulta_id, fuente_obj, 0, "Sin Validar",
                                           "Acceso denegado por la web (bloqueo anti-bot). Revisar captura.", rel_png)
                    try:
                        await context.close()
                    except Exception:
                        pass
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    return

                # acciones humanas
                await _human_like_actions(page)

                # aceptar cookies si aparece
                try:
                    for sel in SEL_COOKIE_BTN.split(","):
                        sel = sel.strip()
                        if await page.locator(sel).count() > 0:
                            await page.locator(sel).first.click(timeout=1200)
                            await asyncio.sleep(0.4)
                            break
                except Exception:
                    pass

                # rellenar nombre/apellido si existen
                try:
                    if nombre and await page.locator(SEL_FORNAME).count() > 0:
                        await page.locator(SEL_FORNAME).fill(nombre.strip())
                        await asyncio.sleep(random.uniform(0.2, 0.6))
                except Exception:
                    pass
                try:
                    if apellido and await page.locator(SEL_NAME).count() > 0:
                        await page.locator(SEL_NAME).fill(apellido.strip())
                        await asyncio.sleep(random.uniform(0.2, 0.6))
                except Exception:
                    pass

                # enviar búsqueda (robusto)
                submitted = False
                try:
                    if await page.locator(SEL_SUBMIT).count() > 0:
                        await page.locator(SEL_SUBMIT).first.scroll_into_view_if_needed()
                        await page.locator(SEL_SUBMIT).first.click(timeout=5000)
                        submitted = True
                    else:
                        if await page.locator(SEL_NAME).count() > 0:
                            await page.locator(SEL_NAME).press("Enter")
                            submitted = True
                except Exception:
                    submitted = False

                # esperar resultado o un tiempo razonable
                try:
                    await page.wait_for_selector(f"{SEL_RESULTS_BLOCK}, {SEL_RESULTS_TEXT}", timeout=10000)
                except Exception:
                    # no hay selector visible, continuar a análisis del body
                    await asyncio.sleep(0.5)

                # tomar captura final solo ahora (cuando ya se buscó)
                rel_png = await _save_screenshot_only(absolute_folder, f"attempt{attempt}_final", page)

                # analizar resultado
                texto = ""
                try:
                    if await page.locator(SEL_RESULTS_TEXT).count() > 0:
                        texto = (await page.locator(SEL_RESULTS_TEXT).first.inner_text() or "").strip()
                except Exception:
                    try:
                        texto = (await page.inner_text("body") or "").strip()[:1000]
                    except Exception:
                        texto = ""

                if texto:
                    if "no hay resultados" in texto.lower() or "no results" in texto.lower():
                        score = 0
                        mensaje = "No hay resultados para su búsqueda. Seleccione otros criterios."
                    else:
                        score = 10
                        mensaje = texto
                else:
                    score = 0
                    mensaje = "No se detectó texto de resultados. Revisar captura."

                # registrar resultado y cerrar
                await _crear_resultado(consulta_id, fuente_obj, score, "Validada", mensaje, rel_png)

                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass

                return  # éxito o resultado guardado; salir del loop

        except Exception as e:
            last_exception = e
            # intentar guardar captura de excepción si hay page
            try:
                if page:
                    rel_png = await _save_screenshot_only(absolute_folder, f"attempt{attempt}_exception", page)
            except Exception:
                rel_png = os.path.join(relative_folder, f"attempt{attempt}_exception.png")
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

            if attempt >= MAX_ATTEMPTS:
                await _crear_resultado(consulta_id, fuente_obj, 0, "Sin Validar",
                                       f"Ocurrió un error tras varios intentos: {str(last_exception)}", rel_png)
                return

            await asyncio.sleep(1.0 + random.random() * 2.0)

    # si salimos del loop sin retorno, registrar error genérico
    await _crear_resultado(consulta_id, fuente_obj, 0, "Sin Validar",
                           f"No fue posible completar la consulta tras {MAX_ATTEMPTS} intentos. Último error: {str(last_exception)}",
                           os.path.join(relative_folder, "no_evidence.png"))
