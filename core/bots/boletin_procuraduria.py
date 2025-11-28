# core/bots/boletin_procuraduria.py
import os
import re
import unicodedata
import asyncio
import random
import json
from datetime import datetime
from typing import Optional, List
from urllib.parse import quote_plus, urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Fuente, Resultado

NOMBRE_SITIO = "boletin_procuraduria"

# Configuración por defecto (puedes sobreescribir con variables de entorno)
MAX_ATTEMPTS = int(os.getenv("BOLETIN_MAX_ATTEMPTS", "3"))
HEADLESS_DEFAULT = os.getenv("BOLETIN_HEADLESS", "true").lower() == "true"
PROXY_LIST_ENV = os.getenv("BOLETIN_PROXY_LIST")  # CSV or JSON array
UA_LIST_ENV = os.getenv("BOLETIN_UA_LIST")  # CSV or JSON array


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


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s


def _score_por_coincidencias(n: int) -> int:
    if n <= 0:
        return 0
    if n == 1:
        return 6
    return 10


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


def _is_unavailable_text(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    checks = [
        "página web no disponible",
        "page not available",
        "la página que consulta no se encuentra disponible",
        "access to this site was denied",
        "forbidden",
        "403",
    ]
    return any(c in low for c in checks)


async def _save_screenshot(page: Optional[Page], absolute_path: str):
    try:
        if page:
            await page.screenshot(path=absolute_path, full_page=True)
            return
    except Exception:
        pass
    try:
        if page:
            await page.screenshot(path=absolute_path)
            return
    except Exception:
        pass
    try:
        open(absolute_path, "wb").close()
    except Exception:
        pass


async def _create_browser_context(p: Browser, ua: str, proxy: Optional[str], storage_state: Optional[str]) -> BrowserContext:
    context_kwargs = {
        "viewport": {"width": 1366, "height": 768},
        "user_agent": ua,
        "locale": "es-ES",
        "ignore_https_errors": True,
        "extra_http_headers": {"Accept-Language": "es-ES,es;q=0.9,en;q=0.8", "Referer": "https://www.google.com/"},
    }
    if storage_state and os.path.exists(storage_state):
        context_kwargs["storage_state"] = storage_state
    context = await p.new_context(**context_kwargs)
    # stealth-ish init
    try:
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['es-ES','es'] });
            window.navigator.chrome = { runtime: {} };
            """
        )
    except Exception:
        pass
    return context


async def consultar_boletin_procuraduria(nombre: str, consulta_id: int, cedula: str, headless: Optional[bool] = None):
    """
    Bot para buscar en el buscador de la Procuraduría.
    Firma: (nombre, consulta_id, cedula, headless=True/False)
    - Detecta HTTP status (403) y reintenta con rotación de UA/proxy.
    - Guarda solo screenshot final en resultados/<consulta_id>/.
    - No guarda HTML.
    """
    headless = HEADLESS_DEFAULT if headless is None else bool(headless)
    ua_list = _parse_list_env(UA_LIST_ENV) or _default_user_agents()
    proxy_list = _parse_list_env(PROXY_LIST_ENV)

    nombre = (nombre or "").strip()
    cedula = (cedula or "").strip()

    # preparar carpeta de resultados
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    # rutas de búsqueda
    name_term = quote_plus(nombre) if nombre else ""
    cedula_term = quote_plus(cedula) if cedula else ""
    url_by_name = (
        "https://www.procuraduria.gov.co/_layouts/15/osssearchresults.aspx"
        f"?u=https%3A%2F%2Fwww%2Eprocuraduria%2Egov%2Eco&k={name_term}"
    )
    url_by_cedula = (
        "https://www.procuraduria.gov.co/_layouts/15/osssearchresults.aspx"
        f"?u=https%3A%2F%2Fwww%2Eprocuraduria%2Egov%2Eco&k={cedula_term}"
    )

    fuente_obj = await _get_fuente(NOMBRE_SITIO)

    last_exception = None
    attempt = 0

    # nombre base para png
    ts_base = datetime.now().strftime("%Y%m%d_%H%M%S")

    while attempt < MAX_ATTEMPTS:
        attempt += 1
        ua = random.choice(ua_list)
        proxy = random.choice(proxy_list) if proxy_list else None

        browser: Optional[Browser] = None
        context: Optional[BrowserContext] = None
        page: Optional[Page] = None

        try:
            async with async_playwright() as p:
                launch_kwargs = {"headless": headless, "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"]}
                if proxy:
                    # Playwright expects proxy as dict on launch
                    launch_kwargs["proxy"] = {"server": proxy}
                browser = await p.chromium.launch(**launch_kwargs)

                # create context (storage_state not used here by default)
                storage_state = os.getenv("BOLETIN_STORAGE_STATE")  # optional path
                context = await _create_browser_context(browser, ua, proxy, storage_state)
                page = await context.new_page()
                page.set_default_navigation_timeout(60000)
                page.set_default_timeout(30000)

                # Primero intentar por nombre (si existe), luego por cédula si detectamos 403 o indisponible
                for attempt_url, url in enumerate([url_by_name, url_by_cedula], start=1):
                    if not url or url.endswith("k="):
                        continue  # skip empty term

                    # navegar y capturar response
                    response = None
                    try:
                        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        # reintento corto
                        try:
                            await asyncio.sleep(1.0)
                            response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        except Exception:
                            response = None

                    status = None
                    try:
                        if response:
                            status = response.status
                        else:
                            # intentar inferir status por esperar una respuesta principal
                            try:
                                r = await page.wait_for_response(lambda r: urlparse(url).netloc in r.url or url in r.url, timeout=3000)
                                status = r.status
                            except Exception:
                                status = None
                    except Exception:
                        status = None

                    # esperar recursos JS
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        await asyncio.sleep(0.5)

                    # leer body text para detectar mensajes de indisponibilidad
                    body_text = ""
                    try:
                        body_text = await page.inner_text("body")
                    except Exception:
                        body_text = ""

                    # si recibimos 403 o detectamos texto de indisponibilidad, tomar evidencia y decidir reintento
                    if status == 403 or _is_unavailable_text(body_text):
                        png_name = f"{NOMBRE_SITIO}_{cedula}_attempt{attempt}_url{attempt_url}_{ts_base}.png"
                        abs_png = os.path.join(absolute_folder, png_name)
                        await _save_screenshot(page, abs_png)
                        rel_png = os.path.relpath(abs_png, settings.MEDIA_ROOT).replace("\\", "/")
                        mensaje = f"HTTP {status or 'N/A'} o página no disponible detectada en intento {attempt} (url index {attempt_url})."
                        # si es último intento, registrar y salir
                        if attempt >= MAX_ATTEMPTS:
                            await _crear_resultado(consulta_id, fuente_obj, 0, "Sin Validar", mensaje, rel_png)
                            try:
                                await context.close()
                            except Exception:
                                pass
                            try:
                                await browser.close()
                            except Exception:
                                pass
                            return
                        # cerrar contexto y reintentar con otro UA/proxy
                        try:
                            await context.close()
                        except Exception:
                            pass
                        try:
                            await browser.close()
                        except Exception:
                            pass
                        # jitter before next attempt
                        await asyncio.sleep(1.0 + random.random() * 2.0)
                        break  # break inner for to recreate browser/context and retry outer loop

                    # si llegamos aquí, la página cargó sin 403 ni mensaje de indisponibilidad
                    # tomar captura final y analizar resultados
                    png_name = f"{NOMBRE_SITIO}_{cedula}_attempt{attempt}_url{attempt_url}_final_{ts_base}.png"
                    abs_png = os.path.join(absolute_folder, png_name)
                    await _save_screenshot(page, abs_png)
                    rel_png = os.path.relpath(abs_png, settings.MEDIA_ROOT).replace("\\", "/")

                    # intentar extraer contenedor de resultados
                    result_text = ""
                    try:
                        result_div = await page.query_selector("#Result")
                        if result_div:
                            result_text = await result_div.inner_text()
                    except Exception:
                        result_text = ""

                    # si no hay result_div, fallback a body
                    if not result_text:
                        try:
                            result_text = await page.inner_text("body")
                        except Exception:
                            result_text = ""

                    # conteo de coincidencias: preferir nombre, si no usar cédula
                    coincidencias = 0
                    if result_text:
                        if nombre:
                            coincidencias = _norm(result_text).count(_norm(nombre))
                        if coincidencias == 0 and cedula:
                            coincidencias = _norm(result_text).count(_norm(cedula))

                    score = _score_por_coincidencias(coincidencias)
                    mensaje = "se encontraron hallazgos" if coincidencias > 0 else "no se encontraron hallazgos"
                    estado = "Validada"

                    await _crear_resultado(consulta_id, fuente_obj, score, estado, mensaje, rel_png)

                    try:
                        await context.close()
                    except Exception:
                        pass
                    try:
                        await browser.close()
                    except Exception:
                        pass

                    return  # éxito: salir de la función

        except Exception as e:
            last_exception = e
            # intentar guardar captura de excepción si hay page/context
            try:
                if page:
                    png_name = f"{NOMBRE_SITIO}_{cedula}_attempt{attempt}_exception_{ts_base}.png"
                    abs_png = os.path.join(absolute_folder, png_name)
                    await _save_screenshot(page, abs_png)
                    rel_png = os.path.relpath(abs_png, settings.MEDIA_ROOT).replace("\\", "/")
                else:
                    rel_png = os.path.join(relative_folder, f"attempt{attempt}_no_page.png")
            except Exception:
                rel_png = os.path.join(relative_folder, f"attempt{attempt}_no_page.png")

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
                await _crear_resultado(consulta_id, fuente_obj, 0, "Sin Validar", f"Error tras varios intentos: {str(last_exception)}", rel_png)
                return

            await asyncio.sleep(1.0 + random.random() * 2.0)
            continue

    # Si salimos del loop sin resultado, registrar fallo genérico
    await _crear_resultado(consulta_id, fuente_obj, 0, "Sin Validar", f"No fue posible completar la consulta tras {MAX_ATTEMPTS} intentos. Último error: {str(last_exception)}", os.path.join(relative_folder, "no_evidence.png"))
