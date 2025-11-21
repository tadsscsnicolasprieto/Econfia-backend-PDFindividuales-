# bots/apgml_search.py
import os, re, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "apgml_search"
URL_SEARCH   = "https://apgml.org/documents/search-results.aspx?keywords={q}"
GOTO_TIMEOUT_MS = 180_000

# Selectores
SEL_H2_MAIN_TITLE = "h2.mainTitle"         # e.g. 0 Search Results for "..."
SEL_DOC_LIST      = "ul.documentsList"
SEL_DOC_ITEM      = "ul.documentsList > li"
SEL_DOC_TITLE_A   = "h3 a"

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    # quitar diacríticos
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    # comprimir espacios
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_apgml_search(consulta_id: int, nombre: str, apellido: str):
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

    # 3) Defaults
    mensaje_final = "No hay coincidencias."
    score_final = 1
    success = False

    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,  # visible para depurar si lo deseas
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="en-US",
                timezone_id="America/Bogota",
            )
            page = await context.new_page()

            # 4) Ir a la búsqueda
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)
            await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

            # 5) Verificar si el H2 indica "0 Search Results..."
            try:
                h2 = page.locator(SEL_H2_MAIN_TITLE)
                if await h2.count() > 0:
                    h2_text = (await h2.first.inner_text()).strip()
                    if h2_text.lower().startswith("0 search results"):
                        mensaje_final = h2_text  # usar exactamente el texto mostrado
                        try:
                            await page.screenshot(path=absolute_png, full_page=True)
                        except Exception:
                            pass
                        success = True
                    else:
                        # 6) Hay resultados: revisar lista
                        items = page.locator(SEL_DOC_ITEM)
                        n = await items.count()
                        exact_hit = False

                        for i in range(n):
                            item = items.nth(i)
                            try:
                                title = (await item.locator(SEL_DOC_TITLE_A).first.inner_text(timeout=3000)).strip()
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

                        try:
                            await page.screenshot(path=absolute_png, full_page=True)
                        except Exception:
                            pass
                        success = True
                else:
                    # Si no aparece el H2 por cambios de layout, intentar directamente la lista
                    items = page.locator(SEL_DOC_ITEM)
                    n = await items.count()
                    if n == 0:
                        mensaje_final = "No hay resultados visibles en la página."
                    else:
                        exact_hit = False
                        for i in range(n):
                            item = items.nth(i)
                            try:
                                title = (await item.locator(SEL_DOC_TITLE_A).first.inner_text(timeout=3000)).strip()
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
                    try:
                        await page.screenshot(path=absolute_png, full_page=True)
                    except Exception:
                        pass
                    success = True

            finally:
                try:
                    await navegador.close()
                except Exception:
                    pass
                navegador = None

        # 7) Persistencia
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj,
            score=score_final,
            estado="Validada" if success else "Sin Validar",
            mensaje=mensaje_final if success else "No fue posible obtener resultados.",
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
