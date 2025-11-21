# bots/dhs_search.py
import os, re, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "dhs_search"
URL_SEARCH = "https://www.dhs.gov/search?goog={q}"
GOTO_TIMEOUT_MS = 180_000

# Selectores del Google Custom Search en dhs.gov
SEL_NORES_SNIPPET = "div.gs-snippet"                       # usamos has_text="No Results"
SEL_RESULT_ITEM   = "div.gsc-webResult.gsc-result"
SEL_RESULT_TITLE  = ".gs-title"                             # dentro del item
SEL_RESULT_SNIP   = ".gs-snippet"                           # dentro del item

def _norm(s: str) -> str:
    """Normaliza para comparación exacta (case-insensitive, sin tildes, espacios comprimidos)."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_dhs_search(consulta_id: int, nombre: str, apellido: str):
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
                headless=True,  # te dejo visible como vienes pidiendo
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="en-US",
                timezone_id="America/Bogota",
            )
            page = await context.new_page()

            # 3) Ir a la URL de búsqueda con el nombre completo
            q = urllib.parse.quote_plus(full_name)
            url = URL_SEARCH.format(q=q)
            await page.goto(url, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

            # 4) ¿"No Results"?
            nores = page.locator(SEL_NORES_SNIPPET, has_text="No Results")
            if await nores.count() > 0:
                # Tomamos el texto exacto del snippet que diga "No Results"
                try:
                    # busca el primero que contenga ese texto
                    idx = 0
                    for i in range(await nores.count()):
                        t = (await nores.nth(i).inner_text()).strip()
                        if "No Results" in t:
                            idx = i
                            break
                    mensaje_final = (await nores.nth(idx).inner_text()).strip()
                except Exception:
                    mensaje_final = "No Results"

                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass

                success = True  # consulta válida sin resultados (score=1)

            else:
                # 5) Iterar cada resultado: match exacto del nombre en título o snippet
                items = page.locator(SEL_RESULT_ITEM)
                n = await items.count()
                exact_hit = False

                for i in range(n):
                    item = items.nth(i)
                    try:
                        title = (await item.locator(SEL_RESULT_TITLE).first.inner_text(timeout=3_000)).strip()
                    except Exception:
                        title = ""
                    try:
                        snip = (await item.locator(SEL_RESULT_SNIP).first.inner_text(timeout=2_000)).strip()
                    except Exception:
                        snip = ""

                    blob_title = _norm(title)
                    blob_snip  = _norm(snip)

                    if blob_title == norm_query or blob_snip == norm_query:
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

            # 6) Cerrar navegador
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 7) Persistir Resultado
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj,
            score=score_final,
            estado="Validada" if success else "Sin Validar",
            mensaje=mensaje_final if success else "No fue posible obtener resultados.",
            archivo=relative_png if success else relative_png
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
