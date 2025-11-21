# bots/eur_lex_2022_398.py
import os, re, asyncio, urllib.parse, unicodedata, time
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "eur_lex_2022_398"  # crea/usa esta Fuente en tu tabla
URL_SEARCH = ("https://eur-lex.europa.eu/search.html"
              "?scope=EURLEX&text={q}&lang=en&type=quick&qid={qid}")
GOTO_TIMEOUT_MS = 180_000

# Selectores
SEL_ALERT_NORES   = "div.alert.alert-info[role='alert']"   # "No results found."
SEL_RESULT        = "div.SearchResult"                     # cada resultado
SEL_COOKIE_ACCEPT = "a.wt-ecl-button.wt-ecl-button--primary.wt-cck--actions-button[href='#accept']"

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def _clean_page_css(page):
    css = """
      header, footer, .header, .footer, #footer, #topbar, #navbar,
      .ec-header, .ec-footer, .ecl-footer, .ecl-topbar, .ecl-footer-core,
      .wt-cck-banner, .wt-cck--banner, .cookies, .cookie {
        display: none !important; visibility: hidden !important;
      }
      body { margin: 0 !important; padding: 0 !important; }
      main, #content, .SearchResults, .container {
        margin: 0 auto !important; padding: 10px !important; max-width: 1100px !important;
      }
    """
    try:
        await page.add_style_tag(content=css)
    except Exception:
        pass

async def consultar_eur_lex_2022_398(consulta_id: int, nombre: str, apellido: str):
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
    success = False
    last_error = None
    score_final = 1  # sube a 5 si hay match exacto

    norm_query = _norm(full_name)
    exact_re = re.compile(rf"(?<!\w){re.escape(norm_query)}(?!\w)")

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="en-US",
                timezone_id="America/Bogota",
            )
            page = await context.new_page()

            # 3) URL de búsqueda (qid variable)
            q = urllib.parse.quote_plus(full_name)
            qid = str(int(time.time() * 1000))
            search_url = URL_SEARCH.format(q=q, qid=qid)

            await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)

            # 3.1) Aceptar cookies si aparece
            try:
                btn = page.locator(SEL_COOKIE_ACCEPT)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click(timeout=5_000)
            except Exception:
                pass

            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

            # 4) ¿No results?
            nores = page.locator(SEL_ALERT_NORES).filter(has_text=re.compile(r"No results found\.?", re.I))
            if await nores.count() > 0 and await nores.first.is_visible():
                try:
                    mensaje_final = (await nores.first.inner_text()).strip()
                except Exception:
                    mensaje_final = "No results found."
                try:
                    await _clean_page_css(page)
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True

            else:
                # 5) Analizar resultados y buscar nombre exacto
                results = page.locator(SEL_RESULT)
                try:
                    await results.first.wait_for(state="visible", timeout=10_000)
                except Exception:
                    pass

                n = await results.count()
                exact_hit = False
                for i in range(n):
                    item = results.nth(i)
                    try:
                        blob = await item.inner_text(timeout=4_000)
                    except Exception:
                        blob = ""
                    norm_blob = _norm(blob)
                    if norm_blob and exact_re.search(norm_blob):
                        exact_hit = True
                        break

                if exact_hit:
                    score_final = 5
                    mensaje_final = f"Coincidencia exacta encontrada en EUR-Lex para: '{full_name}'."
                else:
                    score_final = 1
                    mensaje_final = "Se encontraron resultados, pero no hubo coincidencia exacta del nombre."

                try:
                    await _clean_page_css(page)
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass

                success = True

            # 6) Cierre
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 7) Persistencia
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
