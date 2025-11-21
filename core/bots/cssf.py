# bots/cssf.py
import os, re, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "cssf"  # crea/asegura esta Fuente en tu BD
URL_SEARCH   = "https://www.cssf.lu/fr/search/{q}"
GOTO_TIMEOUT_MS = 180_000

# Selectores
SEL_COOKIE_BTN    = "button.cookie-btn.cookie-accept-all[aria-label*='accepter']"
SEL_NORES_H2      = "h2:has-text('Désolé, aucun résultat')"  # <h2>Désolé, aucun résultat</h2>
SEL_RESULT_LIST   = "ul.library-table"                       # contenedor de resultados
SEL_RESULT_ITEMS  = f"{SEL_RESULT_LIST} li"                  # ítems dentro de la tabla
SEL_RESULT_TITLE  = "a, h3, .title, .library-title"          # posibles títulos dentro del ítem

def _norm(s: str) -> str:
    """Normaliza para comparación exacta: minúsculas, sin tildes, espacios comprimidos."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_cssf(consulta_id: int, nombre: str, apellido: str):
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
    score_final = 1
    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="fr-LU",
                timezone_id="Europe/Luxembourg",
            )
            page = await context.new_page()

            # 3) Ir a la página de búsqueda (formato /fr/search/<q>)
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)
            await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)

            # 3.1) Aceptar cookies si aparecen
            try:
                btn = page.locator(SEL_COOKIE_BTN)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click(timeout=5_000)
            except Exception:
                pass

            await page.wait_for_load_state("domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

            # 4) ¿Sin resultados?
            nores = page.locator(SEL_NORES_H2)
            if await nores.count() > 0 and await nores.first.is_visible():
                try:
                    # Texto exacto del H2
                    mensaje_final = (await nores.first.inner_text()).strip()
                except Exception:
                    mensaje_final = "Désolé, aucun résultat"
                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True  # consulta válida, sin hallazgos (score 1)

            else:
                # 5) Con resultados: iterar cada ul.library-table y sus li
                lists = page.locator(SEL_RESULT_LIST)
                exact_hit = False

                lists_count = await lists.count()
                for li_idx in range(lists_count):
                    lst = lists.nth(li_idx)
                    items = lst.locator("li")
                    n = await items.count()
                    for i in range(n):
                        it = items.nth(i)
                        # Título preferido: <a>
                        title = ""
                        try:
                            # Intentamos <a>, si no, otros posibles títulos
                            if await it.locator("a").count() > 0:
                                title = (await it.locator("a").first.inner_text(timeout=2_000)).strip()
                            elif await it.locator(SEL_RESULT_TITLE).count() > 0:
                                title = (await it.locator(SEL_RESULT_TITLE).first.inner_text(timeout=2_000)).strip()
                        except Exception:
                            title = ""

                        # Si no hay título, tomamos un bloque de texto del item
                        if not title:
                            try:
                                title = (await it.inner_text(timeout=2_000)).strip()
                            except Exception:
                                title = ""

                        if title and _norm(title) == norm_query:
                            exact_hit = True
                            break
                    if exact_hit:
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

            # 6) Cerrar navegador
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 7) Guardado de Resultado
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
