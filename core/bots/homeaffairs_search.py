# bots/homeaffairs_search.py
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

NOMBRE_SITIO = "homeaffairs_search"
URL_SEARCH = "https://www.homeaffairs.gov.au/sitesearch?k={q}"
GOTO_TIMEOUT_MS = 180_000

# Selectores (UI fallback)
SEL_UI_INPUT = "input[name='search']"
SEL_UI_BUTTON = "button.search-submit"

# Selectores de resultado
SEL_NORES_WRAPPER = "div.search-results-list"
SEL_NORES_H4      = f"{SEL_NORES_WRAPPER} > h4"
SEL_RESULT_ITEM   = "ha-result-item"
SEL_RESULT_TITLE  = "ha-result-item a"   # fallback, el primer <a> dentro del item

def _norm(s: str) -> str:
    """Normaliza para comparación exacta 'humana': minúsculas, sin diacríticos, espacios comprimidos."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def _goto_with_retries(page, url, attempts=3, base_delay=1.0, timeout=GOTO_TIMEOUT_MS):
    """Intentar page.goto con reintentos exponenciales; devuelve response o lanza."""
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            resp = await page.goto(url, timeout=timeout)
            return resp
        except Exception as e:
            last_exc = e
            print(f"[RGM][WARN] goto intento {i} falló: {e}")
            if i < attempts:
                await asyncio.sleep(base_delay * (2 ** (i - 1)))
    raise last_exc

async def _wait_for_networkidle_with_retries(page, retries=3, base_delay=1.0, timeout=45000):
    for attempt in range(1, retries + 1):
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
            return True
        except Exception as e:
            print(f"[RGM][WARN] networkidle intento {attempt} falló: {e}")
            if attempt < retries:
                await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
    return False

async def _save_debug_artifacts(page, absolute_folder, prefix):
    """Guarda HTML y screenshot con prefijo y timestamp; devuelve rutas."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    html_path = os.path.join(absolute_folder, f"{prefix}_{ts}.html")
    png_path = os.path.join(absolute_folder, f"{prefix}_{ts}.png")
    try:
        content = await page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[RGM] DEBUG: HTML guardado en: {html_path}")
    except Exception as e:
        print(f"[RGM][WARN] No se pudo guardar HTML debug: {e}")
        html_path = ""
    try:
        await page.screenshot(path=png_path, full_page=True)
        print(f"[RGM] DEBUG: Screenshot guardado en: {png_path}")
    except Exception as e:
        print(f"[RGM][WARN] No se pudo guardar screenshot debug: {e}")
        png_path = ""
    return html_path, png_path

async def consultar_homeaffairs_search(consulta_id: int, nombre: str, apellido: str, headless=True):
    """
    Flujo robusto para consultar homeaffairs.gov.au:
    - Intenta URL directa en modo headless (por defecto).
    - Si recibe bloqueo (403 / Access Denied) o resultado inesperado, ejecuta una comparación headful.
    - Guarda artefactos (HTML + screenshots) para diagnóstico.
    - No intenta evadir bloqueos; marca la consulta para revisión humana si está bloqueada.
    """
    navegador = None
    full_name = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()
    print(f"[RGM] Iniciando consulta HomeAffairs: consulta_id={consulta_id} nombre='{full_name}' headless={headless}")

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
        print(f"[RGM] Fuente encontrada id={getattr(fuente_obj, 'id', None)}")
    except Exception as e:
        print(f"[RGM][ERROR] No se encontró la Fuente '{NOMBRE_SITIO}': {e}")
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=1,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    if not full_name:
        print("[RGM][ERROR] Nombre y/o apellido vacíos")
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=1,
            estado="Sin Validar",
            mensaje="Nombre y/o apellido vacíos para la consulta.",
            archivo=""
        )
        return

    # 2) Carpeta / archivo
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^\w\.-]+", "_", full_name)
    png_name = f"{NOMBRE_SITIO}_{safe_name}_{ts}.png"
    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    mensaje_final = "No hay coincidencias."
    score_final = 1  # por defecto 1 (sólo sube a 5 si hay match exacto)
    success = False
    last_error = None

    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            # Lanzar navegador con la opción headless solicitada
            navegador = await p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="en-AU",
                timezone_id="Australia/Sydney",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
            )
            page = await context.new_page()

            # Registrar console y responses para diagnóstico
            page.on("console", lambda msg: print(f"[RGM][PAGE CONSOLE] {msg.type}: {msg.text}"))
            page.on("response", lambda resp: print(f"[RGM][RESPONSE] {resp.status} {resp.url}"))

            # Intento principal: URL directa
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)
            print(f"[RGM] Intentando URL directa: {search_url} (headless={headless})")

            resp = None
            try:
                resp = await _goto_with_retries(page, search_url, attempts=3, base_delay=1.0, timeout=GOTO_TIMEOUT_MS)
                print(f"[RGM] page.goto finalizó. page.url = {page.url}")
            except Exception as e:
                print(f"[RGM][WARN] Error en goto URL directa: {e}")
                last_error = f"Error navegando a la URL: {e}"

            # Registrar status y headers si hay response
            resp_status = None
            resp_headers = {}
            if resp:
                try:
                    resp_status = resp.status
                    try:
                        resp_headers = await resp.all_headers()
                    except Exception:
                        resp_headers = getattr(resp, "headers", {}) or {}
                    print(f"[RGM] response status = {resp_status}")
                    print(f"[RGM] response headers = {json.dumps(resp_headers)}")
                except Exception as e:
                    print(f"[RGM][WARN] No se pudieron leer headers/estado: {e}")

            # Esperar networkidle con retries
            await _wait_for_networkidle_with_retries(page, retries=3, base_delay=1.0, timeout=45000)

            # Guardar artefactos iniciales
            html_path, png_path = await _save_debug_artifacts(page, absolute_folder, "after_goto_headless" if headless else "after_goto_headful")

            # Si recibimos 403 o indicadores de bloqueo, no intentamos evadir: hacemos comparación headful
            blocked = False
            if resp_status == 403:
                print("[RGM][WARN] La respuesta fue 403 (Access Denied). No se intentará evadir; se realizará comparación headful para evidencia.")
                blocked = True
            else:
                # también detectar indicadores en HTML
                try:
                    content_lower = (await page.content()).lower()
                    indicators = ["access denied", "forbidden", "service unavailable", "error 403", "error 503", "cloudflare", "edge", "akamai"]
                    if any(ind in content_lower for ind in indicators):
                        print("[RGM][WARN] Indicadores de bloqueo detectados en HTML")
                        blocked = True
                except Exception:
                    pass

            # Si estamos en headless y detectamos bloqueo, ejecutar headful para comparar (solo diagnóstico)
            if headless and blocked:
                print("[RGM] Ejecutando comparación en modo headful para evidencias (headful run)...")
                try:
                    # Cerrar contexto actual y lanzar uno headful
                    try:
                        await context.close()
                    except Exception:
                        pass
                    try:
                        await navegador.close()
                    except Exception:
                        pass

                    navegador = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
                    context = await navegador.new_context(
                        viewport={"width": 1400, "height": 900},
                        locale="en-AU",
                        timezone_id="Australia/Sydney",
                        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
                    )
                    page = await context.new_page()
                    page.on("console", lambda msg: print(f"[RGM][PAGE CONSOLE headful] {msg.type}: {msg.text}"))
                    page.on("response", lambda resp: print(f"[RGM][RESPONSE headful] {resp.status} {resp.url}"))

                    # Navegar de nuevo en modo headful
                    try:
                        resp2 = await _goto_with_retries(page, search_url, attempts=2, base_delay=1.0, timeout=GOTO_TIMEOUT_MS)
                        status2 = resp2.status if resp2 else None
                        print(f"[RGM] headful page.goto finalizó. page.url = {page.url} status={status2}")
                    except Exception as e:
                        print(f"[RGM][WARN] headful goto falló: {e}")
                        status2 = None

                    await _wait_for_networkidle_with_retries(page, retries=2, base_delay=1.0, timeout=30000)
                    html_path_h, png_path_h = await _save_debug_artifacts(page, absolute_folder, "after_goto_headful")

                    # Guardar evidencia comparativa en mensaje final
                    mensaje_final = (
                        "Bloqueo detectado en modo headless. Se generaron artefactos para revisión humana. "
                        f"headless_status={resp_status} headful_status={status2}."
                    )
                    last_error = mensaje_final

                    # Registrar Resultado como Sin Validar y adjuntar captura headless (si existe)
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id, fuente=fuente_obj,
                        score=1,
                        estado="Sin Validar",
                        mensaje=mensaje_final,
                        archivo=relative_png if os.path.exists(absolute_png) else (png_path or png_path_h or "")
                    )
                    print(f"[RGM] Bloqueo registrado. Artefactos: {html_path}, {png_path}, {html_path_h}, {png_path_h}")
                    # Cerrar navegador headful
                    try:
                        await navegador.close()
                    except Exception:
                        pass
                    return
                except Exception as e:
                    print(f"[RGM][ERROR] Error durante comparación headful: {e}")
                    last_error = f"Error durante comparación headful: {e}"
                    # continuar y marcar Sin Validar abajo

            # Si no hubo bloqueo, continuar con la lógica de parseo de resultados
            if not blocked:
                try:
                    # 4) Detectar "No results"
                    nores_h4 = page.locator(SEL_NORES_H4, has_text="No results")
                    if await nores_h4.count() > 0 and await nores_h4.first.is_visible():
                        try:
                            wrapper = page.locator(SEL_NORES_WRAPPER).first
                            wrapper_txt = (await wrapper.inner_text()).strip()
                            if wrapper_txt:
                                mensaje_final = wrapper_txt
                            else:
                                mensaje_final = (
                                    "No results\n"
                                    f"Unfortunately there were no results for {full_name}\n"
                                    "Try refining your search with some different key words or looking under a different function"
                                )
                        except Exception:
                            mensaje_final = (
                                "No results\n"
                                f"Unfortunately there were no results for {full_name}\n"
                                "Try refining your search with some different key words or looking under a different function"
                            )
                        try:
                            await page.screenshot(path=absolute_png, full_page=True)
                        except Exception:
                            pass
                        success = True
                    else:
                        # 5) Hay resultados -> iterar <ha-result-item>
                        items = page.locator(SEL_RESULT_ITEM)
                        n = await items.count()
                        print(f"[RGM] Result items count: {n}")
                        exact_hit = False

                        for i in range(n):
                            item = items.nth(i)
                            title_text = ""
                            try:
                                if await item.locator(SEL_RESULT_TITLE).count() > 0:
                                    title_text = (await item.locator(SEL_RESULT_TITLE).first.inner_text(timeout=3_000)).strip()
                            except Exception:
                                title_text = ""
                            if not title_text:
                                try:
                                    title_text = (await item.inner_text(timeout=2_000)).strip()
                                except Exception:
                                    title_text = ""
                            print(f"[RGM] Item {i} title (trunc): {title_text[:120]!r}")
                            if title_text and _norm(title_text) == norm_query:
                                exact_hit = True
                                print(f"[RGM] Coincidencia exacta encontrada en item {i}")
                                break

                        if exact_hit:
                            score_final = 5
                            mensaje_final = f"Coincidencia exacta con el nombre buscado: '{full_name}'."
                        else:
                            score_final = 1
                            mensaje_final = "Se encontraron resultados, pero sin coincidencia exacta del nombre."

                        try:
                            await page.screenshot(path=absolute_png, full_page=True)
                        except Exception:
                            pass

                        success = True

                except Exception as e:
                    print(f"[RGM][WARN] Error procesando resultados: {e}")
                    last_error = str(e)

            # Cerrar navegador
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # Persistir Resultado final
        if success:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=score_final,
                estado="Validada",
                mensaje=mensaje_final,
                archivo=relative_png
            )
            print(f"[RGM] Resultado guardado: score={score_final} archivo={relative_png}")
        else:
            # Si no fue exitoso y no se hizo comparación headful, guardar como Sin Validar con artefactos
            msg = last_error or "No fue posible obtener resultados."
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1,
                estado="Sin Validar",
                mensaje=msg,
                archivo=relative_png if os.path.exists(absolute_png) else ""
            )
            print("[RGM] Resultado guardado como Sin Validar")

    except Exception as e:
        print(f"[RGM][ERROR] Excepción general: {e}")
        # guardar HTML de debug si es posible
        try:
            ts3 = datetime.now().strftime("%Y%m%d_%H%M%S")
            debug_name = os.path.join(absolute_folder, f"debug_exception_{ts3}.html")
            if 'page' in locals():
                content = await page.content()
                with open(debug_name, "w", encoding="utf-8") as f:
                    f.write(content)
                print(f"[RGM] HTML de debug guardado: {debug_name}")
        except Exception:
            pass

        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1,
                estado="Sin Validar",
                mensaje=str(e),
                archivo=""
            )
            print("[RGM] Resultado de error guardado en BD")
        except Exception as db_exc:
            print(f"[RGM][FATAL] No se pudo crear Resultado en BD: {db_exc}")
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
