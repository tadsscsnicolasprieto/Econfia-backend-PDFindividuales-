# consulta/eo_13224_findit.py
import os, re, urllib.parse, random, asyncio
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

HEADERS = {"Accept-Language": "en-US,en;q=0.9", "Upgrade-Insecure-Requests": "1"}

NOMBRE_SITIO = "eo_13224_findit"

GOTO_TIMEOUT_MS = 120_000
RETRIES_PER_URL = 2

async def _anti_automation(page):
    await page.add_init_script("""Object.defineProperty(navigator,'webdriver',{get:()=>undefined});""")

async def _es_403(page):
    try:
        txt = await page.inner_text("body", timeout=2000)
        return "403 ERROR" in txt and "cloudfront" in txt.lower()
    except Exception:
        return False

def _urls(cedula: str):
    q = urllib.parse.quote_plus(cedula)
    rid = random.randint(1, 10**9)
    return [
        f"https://findit.state.gov/search?query={q}&affiliate=dos_stategov&_={rid}",
        f"https://search.usa.gov/search?affiliate=state.gov&query={q}&_={rid}",
    ]

async def _goto_with_retries(page, url: str) -> bool:
    for i in range(RETRIES_PER_URL):
        try:
            await page.goto(url, timeout=GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            return True
        except Exception as e:
            if "ERR_NAME_NOT_RESOLVED" in str(e):
                return False
            await asyncio.sleep(2 * (i + 1))
    return False

def _no_results_text(cedula: str) -> str:
    return f"Sorry, no results found for '{cedula}'. Try entering fewer or more general search terms."

async def consultar_eo_13224_findit(consulta_id: int, cedula: str):
    navegador = None
    cedula = (cedula or "").strip()

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    if not cedula:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje="Cédula vacía para la consulta.", archivo=""
        )
        return

    # 2) Carpeta / archivo
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_ced = re.sub(r"[^\w\.-]+", "_", cedula) or "consulta"
    png_name = f"{NOMBRE_SITIO}_{safe_ced}_{ts}.png"
    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    success = False
    last_error = None
    score_final = 0
    mensaje_final = ""

    # Coincidencia exacta de frase/palabra (ignora mayúsculas; evita ser parte de otra palabra)
    exact_phrase_re = re.compile(rf"(?<!\w){re.escape(cedula)}(?!\w)", re.IGNORECASE)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                user_agent=UA,
                locale="en-US",
                timezone_id="America/New_York",
                extra_http_headers=HEADERS,
            )
            page = await context.new_page()
            await _anti_automation(page)

            for url in _urls(cedula):
                try:
                    ok = await _goto_with_retries(page, url)
                    if not ok:
                        continue
                    if await _es_403(page):
                        continue

                    # Espera de layout
                    try:
                        await page.wait_for_selector(
                            "#results, .no-result-error, div.results-count, main, body, div[data-testid='gridContainer']",
                            timeout=8000
                        )
                    except Exception:
                        pass

                    # 1) ¿No hay resultados?
                    nores = page.locator(".no-result-error").first
                    if await nores.count() > 0 and await nores.is_visible():
                        try:
                            mensaje_final = (await nores.inner_text()) or _no_results_text(cedula)
                        except Exception:
                            mensaje_final = _no_results_text(cedula)
                        score_final = 1
                        try:
                            await page.screenshot(path=absolute_png, full_page=True)
                        except Exception:
                            pass
                        success = True
                        break

                    # 2) Hay resultados -> iterar cada tarjeta del grid solicitado
                    items_loc = page.locator("div[data-testid='gridContainer'].search-result-item")
                    if await items_loc.count() == 0:
                        # Fallbacks
                        items_loc = page.locator("#results .search-result-item, .search-result-item")

                    exact_hit = False
                    try:
                        n = await items_loc.count()
                        for i in range(n):
                            item = items_loc.nth(i)
                            # título
                            try:
                                title = (await item.locator(".result-title-link").first.inner_text(timeout=1200)).strip()
                            except Exception:
                                title = ""
                            # descripción
                            try:
                                desc = (await item.locator(".result-desc").first.inner_text(timeout=1200)).strip()
                            except Exception:
                                desc = ""
                            # url visible
                            try:
                                urltxt = (await item.locator(".result-url-text").first.inner_text(timeout=1200)).strip()
                            except Exception:
                                urltxt = ""

                            blob = " \n ".join([title, desc, urltxt])
                            if exact_phrase_re.search(blob):
                                exact_hit = True
                                break
                    except Exception:
                        exact_hit = False  # no bloquea el flujo

                    if exact_hit:
                        score_final = 5
                        mensaje_final = "Se encontraron hallazgos."
                    else:
                        score_final = 1
                        mensaje_final = "No se han encontrado coincidencias."

                    try:
                        await page.screenshot(path=absolute_png, full_page=True)
                    except Exception:
                        pass

                    success = True
                    break

                except Exception as e:
                    last_error = str(e)
                    continue

            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 3) Registrar en BD
        if success:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=score_final, estado="Validada",
                mensaje=mensaje_final,
                archivo=relative_png
            )
        else:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj, score=0,
                estado="Sin Validar",
                mensaje=last_error or "No fue posible obtener resultados (todas las URLs fallaron).",
                archivo=relative_png
            )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj, score=0,
                estado="Sin Validar", mensaje=str(e), archivo=""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
