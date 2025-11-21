# bots/opensanctions_au_dfat.py
import os, re, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "opensanctions_au_dfat"  # Asegúrate de crear esta Fuente en tu BD
URL_SEARCH   = "https://www.opensanctions.org/search/?scope=au_dfat_sanctions&q={q}"
GOTO_TIMEOUT_MS = 180_000

# Selectores robustos (clases con hash variable)
SEL_ALERT_NORES = "div.alert-heading.h4"                # <div class="alert-heading h4">No matching entities were found.</div>
SEL_LIST        = "ul[class*='Search_resultList']"
SEL_ITEM        = "li[class*='Search_resultItem']"
SEL_TITLE_A     = "div[class*='Search_resultTitle'] a"

def _norm(s: str) -> str:
    """Normaliza: minúsculas, sin tildes, espacios comprimidos."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)

async def consultar_opensanctions_au_dfat(consulta_id: int, nombre: str, apellido: str):
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

    # 2) Carpeta y archivo
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^\w\.-]+", "_", full_name)
    png_name = f"{NOMBRE_SITIO}_{safe_name}_{ts}.png"
    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    mensaje_final = "No hay coincidencias."
    score_final = 1     # por defecto 1 salvo match exacto
    success = False

    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            # headless False como pediste
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

            # 3) Navegar a la búsqueda con scope AU DFAT
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)
            await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

            # 4) Caso "No results"
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
                success = True  # consulta válida sin hallazgos (score=1)

            else:
                # 5) Hay resultados: mirar títulos para match exacto
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
                    mensaje_final = "Se encontraron resultados, pero sin coincidencia exacta del nombre."

                try:
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

        # 7) Persistencia Resultado
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
