# bots/sca_search.py
import os, re, asyncio, urllib.parse, unicodedata, random
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from core.models import Resultado, Fuente

NOMBRE_SITIO = "sca_search"  # agrega este nombre en tu tabla core.Fuente
URL_SEARCH = "https://www.sca.gov.ae/en/search.aspx?type=all&query={q}"

GOTO_TIMEOUT_MS = 200_000
RETRIES = 2

# Selectores
SEL_TOTAL_SPAN = "span.listing-total-count-num[data-listingcount='totalitemscount']"
# cada tarjeta: <div class=" data-item col-md-4 item grid-item">
SEL_RESULT_ITEM = "div[class*='data-item'][class*='grid-item']"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

def _norm(s: str) -> str:
    """minúsculas, sin tildes, espacios comprimidos"""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_sca_search(consulta_id: int, nombre: str, apellido: str):
    navegador = None
    full_name = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()

    # 1) Fuente
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
                locale="en-US",
                timezone_id="Asia/Dubai",
                user_agent=UA,
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
                }
            )
            page = await context.new_page()
            await page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

            async def _load_search():
                q = urllib.parse.quote_plus(full_name)
                url = URL_SEARCH.format(q=q)
                await page.goto(url, timeout=GOTO_TIMEOUT_MS)
                await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=60_000)
                except Exception:
                    pass

            async def _wait_count_or_items(timeout_ms=90_000):
                try:
                    await page.wait_for_selector(f"{SEL_TOTAL_SPAN}, {SEL_RESULT_ITEM}",
                                                 state="visible", timeout=timeout_ms)
                    return True
                except PWTimeout:
                    return False

            for intento in range(1, RETRIES + 1):
                try:
                    await _load_search()
                    ok = await _wait_count_or_items(70_000)
                    if not ok:
                        continue  # reintenta carga

                    # Leer el contador si existe
                    count_text = ""
                    total_span = page.locator(SEL_TOTAL_SPAN)
                    if await total_span.count() > 0 and await total_span.first.is_visible():
                        try:
                            count_text = (await total_span.first.inner_text()).strip()
                        except Exception:
                            count_text = ""

                    # Si explícitamente dice 0 → sin resultados
                    if count_text and re.fullmatch(r"\d+", count_text) and int(count_text) == 0:
                        mensaje_final = f"TotalCount: {count_text}"
                        success = True
                        break

                    # Hay resultados (o al menos el contador no es 0). Revisar tarjetas
                    items = page.locator(SEL_RESULT_ITEM)
                    n = await items.count()

                    exact_hit = False
                    for i in range(n):
                        try:
                            blob = (await items.nth(i).inner_text(timeout=5_000)).strip()
                        except Exception:
                            blob = ""
                        if blob and _norm(blob).find(norm_query) != -1:
                            exact_hit = True
                            break

                    if exact_hit:
                        score_final = 5
                        mensaje_final = f"Coincidencia exacta con el nombre buscado: '{full_name}'."
                    else:
                        score_final = 1
                        # Si tenemos count_text distinto de vacío, lo anexamos para trazabilidad
                        extra = f" (TotalCount: {count_text})" if count_text else ""
                        mensaje_final = "Se encontraron resultados, pero sin coincidencia exacta del nombre." + extra

                    success = True
                    break

                except Exception:
                    if intento == RETRIES:
                        raise

            # Screenshot siempre
            try:
                await page.screenshot(path=absolute_png, full_page=True)
            except Exception:
                pass

            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 7) Guardado Resultado
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
