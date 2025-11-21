# bots/ebrd.py
import os, re, asyncio, urllib.parse
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "ebrd"
URL_HOME = "https://www.ebrd.com/"
URL_SEARCH = "https://www.ebrd.com/home/search.html?search={q}"

GOTO_TIMEOUT_MS = 180_000

SEL_COOKIE_BTN   = "#acceptCookie"
SEL_CARD         = "div.search-result__result-card"
SEL_CARD_TITLE   = ".search-result__result-card-title"
SEL_NORES_H4     = "h4"      # luego filtramos por texto
SEL_NORES_P      = "h4 + p"  # párrafo que sigue al H4

async def consultar_ebrd(consulta_id: int, nombre: str, apellido: str):
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

    # Carpeta / archivo
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^\w\.-]+", "_", full_name)
    png_name = f"{NOMBRE_SITIO}{safe_name}{ts}.png"
    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    mensaje_final = "No hay coincidencias."
    success = False
    last_error = None

    # Coincidencia exacta de nombre y apellido (palabra completa, case-insensitive)
    exact_name_re = re.compile(rf"(?<!\w){re.escape(full_name)}(?!\w)", re.IGNORECASE)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="en-GB",
                timezone_id="Europe/London",
            )
            page = await context.new_page()

            # 1) Ir directo a la URL de búsqueda
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)
            await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)

            # 2) Aceptar cookies si aparecen
            try:
                btn = page.locator(SEL_COOKIE_BTN)
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=5_000)
                    try:
                        await btn.wait_for(state="detached", timeout=5_000)
                    except Exception:
                        pass
            except Exception:
                pass

            # 3) Esperar carga de resultados
            try:
                # la página suele ser /home/search.html
                await page.wait_for_url(re.compile(r"/(home/)?search\.html"), timeout=60_000)
            except Exception:
                pass
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

            # 4) ¿"No results found."?
            nores_h4 = page.locator(SEL_NORES_H4, has_text="No results found.")
            if await nores_h4.count() > 0 and await nores_h4.first.is_visible():
                try:
                    h4_txt = (await nores_h4.first.inner_text()).strip()
                    p_txt = ""
                    p_loc = page.locator(SEL_NORES_P)
                    if await p_loc.count() > 0:
                        p_txt = (await p_loc.first.inner_text()).strip()
                    mensaje_final = f"{h4_txt} {p_txt}".strip()
                except Exception:
                    mensaje_final = "No results found. Please try a different search word or phrase."

                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True

            else:
                # 5) Iterar tarjetas de resultados y buscar el nombre exacto
                cards = page.locator(SEL_CARD)
                n = await cards.count()
                hit = False

                for i in range(n):
                    item = cards.nth(i)
                    # Título (h4 o <a>) dentro del bloque de título
                    try:
                        title = (await item.locator(f"{SEL_CARD_TITLE} h4, {SEL_CARD_TITLE} a")
                                         .first.inner_text(timeout=2_000)).strip()
                    except Exception:
                        title = ""
                    # Snippet (p) dentro del mismo bloque de título
                    try:
                        snippet = (await item.locator(f"{SEL_CARD_TITLE} p")
                                          .first.inner_text(timeout=1_500)).strip()
                    except Exception:
                        snippet = ""

                    blob = f"{title}\n{snippet}"
                    if exact_name_re.search(blob):
                        hit = True
                        break

                mensaje_final = "Se encontraron coincidencias." if hit else "No hay coincidencias."

                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True

            # 6) Cerrar navegador
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 7) Guardar resultado (score siempre 1)
        if success:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1,
                estado="Validada",
                mensaje=mensaje_final,
                archivo=relative_png
            )
        else:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1, estado="Sin Validar",
                mensaje=last_error or "No fue posible obtener resultados.",
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