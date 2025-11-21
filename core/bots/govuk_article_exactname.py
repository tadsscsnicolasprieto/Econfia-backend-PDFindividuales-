# bots/govuk_article_exactname.py
import os, re, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "govuk_article_exactname"  # asegúrate de tener esta Fuente
GOTO_TIMEOUT_MS = 180_000

# Selectores
SEL_COOKIE_ACCEPT = "button.gem-c-button.govuk-button[data-accept-cookies='true'][data-cookie-types='all']"
SEL_LIST_ITEM     = "li.gem-c-document-list__item"
SEL_TITLE_LINK    = f"{SEL_LIST_ITEM} h3 a, {SEL_LIST_ITEM} a"

URL_SEARCH = "https://www.gov.uk/search/all?keywords={q}"

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_govuk_article_exactname(consulta_id: int, nombre: str, apellido: str):
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

    # Defaults
    mensaje_final = "No results found."
    score_final = 1
    success = False
    norm_query = _norm(full_name)

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

            # 3) Buscar por URL
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)
            await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)

            # 4) Aceptar cookies si aparecen
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

            # 5) Procesar resultados
            items = page.locator(SEL_LIST_ITEM)
            n = await items.count()

            if n == 0:
                # Sin resultados visibles (ni UL/LI)
                mensaje_final = "No results found."
                success = True
            else:
                exact_hit = False
                for i in range(n):
                    try:
                        title = (await page.locator(SEL_TITLE_LINK).nth(i).inner_text(timeout=3_000)).strip()
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
                success = True

            # 6) Screenshot (siempre que success)
            try:
                await page.screenshot(path=absolute_png, full_page=True)
            except Exception:
                pass

            # 7) Cerrar navegador
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 8) Guardar resultado
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj,
            score=score_final,
            estado="Validada" if success else "Sin Validar",
            mensaje=mensaje_final,
            archivo=relative_png if success else ""
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
