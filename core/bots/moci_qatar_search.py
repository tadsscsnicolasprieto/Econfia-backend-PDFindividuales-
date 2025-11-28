# bots/moci_qatar_search.py
import os
import re
import urllib.parse
import unicodedata
import asyncio
import json
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from core.models import Resultado, Fuente

NOMBRE_SITIO = "moci_qatar_search"
URL_SEARCH = "https://www.moci.gov.qa/en/?s={q}"
GOTO_TIMEOUT_MS = 180_000

# Selectores (WordPress típico)
SEL_NORES_H1 = "h1.page-title"
SEL_RESULTS_CT = "main, #main, .site-main"
SEL_ARTICLE = "article[id^='post-']"
SEL_TITLE_A = f"{SEL_ARTICLE} h2 a, {SEL_ARTICLE} .entry-title a"

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def _goto_with_retries(page, url, attempts=3, base_delay=1.0, timeout=GOTO_TIMEOUT_MS):
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            resp = await page.goto(url, timeout=timeout)
            return resp
        except Exception as e:
            last_exc = e
            if i < attempts:
                await asyncio.sleep(base_delay * (2 ** (i - 1)))
    raise last_exc

async def _wait_for_networkidle_with_retries(page, retries=3, base_delay=1.0, timeout=30000):
    for attempt in range(1, retries + 1):
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
            return True
        except Exception:
            if attempt < retries:
                await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
    return False

async def _save_screenshot(page, folder, prefix, full_page=True):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_path = os.path.join(folder, f"{prefix}_{ts}.png")
    try:
        await page.screenshot(path=png_path, full_page=full_page)
        return png_path
    except Exception:
        return ""

async def consultar_moci_qatar_search(consulta_id: int, nombre: str, apellido: str, headless=True):
    """
    Versión limpia:
    - No guarda HTML ni JSON.
    - No imprime logs.
    - Solo guarda capturas (screenshots) y persiste Resultado.
    - Visita la home antes de la búsqueda para obtener cookies/estado.
    """
    # Normalizar headless
    if isinstance(headless, str):
        headless = headless.strip().lower() in ("1", "true", "yes", "y", "t")
    else:
        headless = bool(headless)

    navegador = None
    full_name = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=1,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    if not full_name:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=1,
            estado="Sin Validar", mensaje="Nombre y/o apellido vacíos para la consulta.", archivo=""
        )
        return

    # Carpeta / archivo
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^\w\.-]+", "_", full_name)
    png_name = f"{NOMBRE_SITIO}_{safe_name}_{ts}.png"
    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    mensaje_final = "No hay coincidencias."
    score_final = 1
    success = False
    last_error = None
    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="en-US",
                timezone_id="America/Bogota",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.moci.gov.qa/en/"
                }
            )
            page = await context.new_page()

            # Visitar la home primero para obtener cookies/estado
            home_url = "https://www.moci.gov.qa/en/"
            try:
                await _goto_with_retries(page, home_url, attempts=2, base_delay=1.0, timeout=60000)
            except Exception:
                pass

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
            except Exception:
                pass
            await _wait_for_networkidle_with_retries(page, retries=2, base_delay=0.5, timeout=15000)
            try:
                await page.evaluate("() => document.fonts.ready")
            except Exception:
                pass

            # Realizar la búsqueda en el mismo contexto
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)
            resp = None
            try:
                resp = await _goto_with_retries(page, search_url, attempts=3, base_delay=1.0, timeout=GOTO_TIMEOUT_MS)
            except Exception as e:
                last_error = f"Error navegando a la URL de búsqueda: {e}"

            # Esperar carga estable
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
            except Exception:
                pass
            await _wait_for_networkidle_with_retries(page, retries=2, base_delay=0.5, timeout=15000)

            # Ocultar overlays molestos antes de capturar
            try:
                await page.evaluate("""
                () => {
                  const hide = (sel) => document.querySelectorAll(sel).forEach(e => e.style.display = 'none');
                  ['.cookie-banner', '.site-footer', '.modal-backdrop', '.ads', '.promo', '#overlay', '.newsletter-popup'].forEach(hide);
                }
                """)
                await asyncio.sleep(0.2)
            except Exception:
                pass

            # Detectar bloqueo por status o por contenido (solo para decidir estado)
            blocked = False
            resp_status = None
            if resp:
                try:
                    resp_status = resp.status
                except Exception:
                    resp_status = None
            if resp_status == 403:
                blocked = True
            else:
                try:
                    content_lower = (await page.content()).lower()
                    indicators = ["access denied", "forbidden", "error 403", "cloudfront", "cloudflare", "request blocked", "web page blocked", "attack id"]
                    if any(ind in content_lower for ind in indicators):
                        blocked = True
                except Exception:
                    pass

            # Si está bloqueado en headless, ejecutar headful para evidencia (solo screenshot)
            if headless and blocked:
                try:
                    try:
                        await context.close()
                    except Exception:
                        pass
                    try:
                        await navegador.close()
                    except Exception:
                        pass

                    navegador = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
                    context = await navegador.new_context(viewport={"width": 1400, "height": 900}, locale="en-US", timezone_id="America/Bogota")
                    page = await context.new_page()
                    try:
                        await _goto_with_retries(page, search_url, attempts=2, base_delay=1.0, timeout=GOTO_TIMEOUT_MS)
                    except Exception:
                        pass
                    await _wait_for_networkidle_with_retries(page, retries=2, base_delay=0.5, timeout=15000)

                    # ocultar overlays y tomar screenshot de evidencia (solo PNG)
                    try:
                        await page.evaluate("""
                        () => {
                          const hide = (sel) => document.querySelectorAll(sel).forEach(e => e.style.display = 'none');
                          ['.cookie-banner', '.site-footer', '.modal-backdrop', '.ads', '.promo', '#overlay'].forEach(hide);
                        }
                        """)
                    except Exception:
                        pass

                    evidencia_png = await _save_screenshot(page, absolute_folder, "moci_evidence_headful", full_page=True)

                    mensaje_final = "Bloqueo detectado (headless)."
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id, fuente=fuente_obj,
                        score=1, estado="Sin Validar",
                        mensaje=mensaje_final,
                        archivo=os.path.join(relative_folder, os.path.basename(evidencia_png)).replace("\\", "/") if evidencia_png else ""
                    )
                    try:
                        await navegador.close()
                    except Exception:
                        pass
                    return
                except Exception:
                    pass

            # Si no está bloqueado, continuar con la lógica original
            nores_h1 = page.locator(SEL_NORES_H1, has_text="Nothing Found")
            if await nores_h1.count() > 0 and await nores_h1.first.is_visible():
                try:
                    mensaje_final = (await nores_h1.first.inner_text()).strip()
                except Exception:
                    mensaje_final = "Nothing Found"
                # captura full page
                _ = await _save_screenshot(page, absolute_folder, "moci_result", full_page=True)
                success = True
            else:
                articles = page.locator(SEL_ARTICLE)
                n = await articles.count()
                exact_hit = False
                for i in range(n):
                    art = articles.nth(i)
                    try:
                        title = (await art.locator(SEL_TITLE_A).first.inner_text(timeout=3000)).strip()
                    except Exception:
                        title = ""
                    if title and _norm(title) == norm_query:
                        exact_hit = True
                        break
                if exact_hit:
                    score_final = 5
                    mensaje_final = f"Coincidencia exacta con el nombre buscado: '{full_name}'."
                else:
                    score_final = 1
                    mensaje_final = "Se encontraron resultados, pero sin coincidencia exacta del nombre."
                # captura full page
                _ = await _save_screenshot(page, absolute_folder, "moci_result", full_page=True)
                success = True

            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # Persistencia
        if success:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=score_final, estado="Validada",
                mensaje=mensaje_final, archivo=relative_png
            )
        else:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1, estado="Sin Validar",
                mensaje=last_error or "No fue posible obtener resultados.", archivo=relative_png
            )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1, estado="Sin Validar",
                mensaje=str(e), archivo=""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
