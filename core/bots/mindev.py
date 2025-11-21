# bots/mindev.py
import os, re, asyncio, urllib.parse, unicodedata, random
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from core.models import Resultado, Fuente

NOMBRE_SITIO = "mindev"
URL_HOME   = "https://mindev.gov.ua/"
URL_SEARCH = "https://mindev.gov.ua/searchresult?key={q}"

GOTO_TIMEOUT_MS = 200_000
RETRIES = 2

# Selectores robustos
SEL_EMPTY  = "div.search_empty-msg#search_empty-msg"           # 'За вашим запитом не знайдено матеріалів'
SEL_ITEMS  = "div.search-res_list-item"                        # cada resultado
SEL_INPUTS = "input[type='search'], input[name='key'], input#search, input[placeholder*='Введіть'], input[placeholder*='Search']"
SEL_SUBMIT = "button[type='submit'], form button"

UA_POOL = [
    # UA reales modernos (Chromium)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_mindev(consulta_id: int, nombre: str, apellido: str):
    navegador = None
    full_name = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=1,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    if not full_name:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=1,
            estado="Sin Validar",
            mensaje="Nombre y/o apellido vacíos para la consulta.",
            archivo=""
        )
        return

    # Carpetas
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w\.-]+", "_", full_name)
    png_name = f"{NOMBRE_SITIO}_{safe}_{ts}.png"
    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    mensaje_final = "No hay coincidencias."
    score_final = 1
    success = False
    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="uk-UA",
                timezone_id="Europe/Kiev",   # sitio ucraniano: menos fricción
                user_agent=random.choice(UA_POOL),
                extra_http_headers={
                    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
                    "Referer": URL_HOME,
                }
            )
            page = await context.new_page()

            # Pequeño anti-automation
            await page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

            # Función helper: intentar cargar resultados por URL directa
            async def _try_url_flow():
                q = urllib.parse.quote_plus(full_name)
                url = URL_SEARCH.format(q=q)
                await page.goto(URL_HOME, timeout=GOTO_TIMEOUT_MS)  # calentar cookies/locale
                await page.goto(url, timeout=GOTO_TIMEOUT_MS)
                await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=60_000)
                except Exception:
                    pass

            # Función helper: si no hay lista ni vacío, forzar búsqueda escribiendo y Enter
            async def _fallback_type_and_submit():
                # Si estamos en otra vista, ir a home y abrir buscador por URL limpia
                if not page.url.startswith(URL_HOME):
                    await page.goto(URL_HOME, timeout=GOTO_TIMEOUT_MS)
                # Buscar un input de búsqueda visible
                inputs = page.locator(SEL_INPUTS)
                if await inputs.count() == 0:
                    # ir a la página de resultados y volver a intentar
                    q = urllib.parse.quote_plus(full_name)
                    await page.goto(URL_SEARCH.format(q=q), timeout=GOTO_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                    inputs = page.locator(SEL_INPUTS)

                if await inputs.count() > 0:
                    inp = inputs.first
                    await inp.click()
                    await inp.fill(full_name)
                    await inp.press("Enter")
                    try:
                        await page.wait_for_load_state("networkidle", timeout=60_000)
                    except Exception:
                        pass

            # Esperar algo “concreto”: lista o vacío
            async def _wait_results_or_empty(timeout_ms=90_000):
                try:
                    await page.wait_for_selector(f"{SEL_EMPTY}, {SEL_ITEMS}", timeout=timeout_ms, state="visible")
                    return True
                except PWTimeout:
                    return False

            # ==== Estrategia con reintentos ====
            for intento in range(1, RETRIES + 1):
                try:
                    await _try_url_flow()
                    ok = await _wait_results_or_empty(70_000)

                    if not ok:
                        # forzar vía input + Enter
                        await _fallback_type_and_submit()
                        ok = await _wait_results_or_empty(70_000)

                    # Si aún nada, pasar al siguiente intento
                    if not ok:
                        continue

                    # ¿Vacío?
                    empty_loc = page.locator(SEL_EMPTY, has_text="За вашим запитом не знайдено матеріалів")
                    if await empty_loc.count() > 0 and await empty_loc.first.is_visible():
                        try:
                            mensaje_final = (await empty_loc.first.inner_text()).strip()
                        except Exception:
                            mensaje_final = "За вашим запитом не знайдено матеріалів"
                        success = True
                        break

                    # Hay resultados: iterar
                    items = page.locator(SEL_ITEMS)
                    n = await items.count()
                    exact = False
                    for i in range(n):
                        try:
                            blob = (await items.nth(i).inner_text(timeout=5_000)).strip()
                        except Exception:
                            blob = ""
                        if blob and _norm(blob).find(norm_query) != -1:
                            exact = True
                            break

                    if exact:
                        score_final = 5
                        mensaje_final = f"Coincidencia exacta con el nombre buscado: '{full_name}'."
                    else:
                        score_final = 1
                        mensaje_final = "Se encontraron resultados, pero sin coincidencia exacta del nombre."

                    success = True
                    break

                except Exception:
                    # siguiente intento
                    if intento == RETRIES:
                        raise

            # Screenshot siempre
            try:
                await page.screenshot(path=absolute_png, full_page=True)
            except Exception:
                pass

            # Cerrar
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # Persistir
        if success:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=score_final,
                estado="Validada",
                mensaje=mensaje_final,
                archivo=relative_png
            )
        else:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1, estado="Sin Validar",
                mensaje="No fue posible obtener resultados.",
                archivo=relative_png
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
