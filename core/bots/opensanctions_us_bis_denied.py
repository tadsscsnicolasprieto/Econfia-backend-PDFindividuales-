# bots/opensanctions_us_bis_denied.py
import os, re, asyncio, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

# Debe existir una Fuente con este nombre en tu BD
NOMBRE_SITIO = "opensanctions_us_bis_denied"
URL_SEARCH = "https://www.opensanctions.org/search/?scope=us_bis_denied&q={q}"
GOTO_TIMEOUT_MS = 180_000

# Selectores robustos (clases con hashes variables)
SEL_ALERT_NORES = "div.alert-heading.h4"                 # <div class="alert-heading h4">No matching entities were found.</div>
SEL_LIST        = "ul[class*='Search_resultList']"
SEL_ITEM        = "li[class*='Search_resultItem']"
SEL_TITLE_A     = "div[class*='Search_resultTitle'] a"   # <a> con el nombre de la entidad

def _norm(s: str) -> str:
    """ Normaliza para comparación exacta: lowercase, sin diacríticos, espacios comprimidos. """
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_opensanctions_us_bis_denied(consulta_id: int, nombre: str, apellido: str):
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
    score_final = 1  # por defecto 1 salvo match exacto
    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            # *** Headless en False como pediste ***
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

            # 3) Ir a la URL de búsqueda en el scope us_bis_denied
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)
            await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

            # 4) ¿"No matching entities were found."?
            nores = page.locator(SEL_ALERT_NORES, has_text="No matching entities were found.")
            if await nores.count() > 0 and await nores.first.is_visible():
                try:
                    mensaje_final = (await nores.first.inner_text()).strip()
                except Exception:
                    mensaje_final = "No matching entities were found."
                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True  # consulta válida, sin hallazgos (score=1)

            else:
                # 5) Hay lista de resultados: iterar títulos y comparar exacto
                items = page.locator(f"{SEL_LIST} {SEL_ITEM}")
                n = await items.count()
                exact_hit = False

                for i in range(n):
                    item = items.nth(i)
                    try:
                        title = (await item.locator(SEL_TITLE_A).first.inner_text(timeout=3_000)).strip()
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
                    # Importante: solo dar este mensaje si hubo resultados (no el alert de 'no results')
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

        # 7) Persistir Resultado (Validada si pudimos consultar)
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
