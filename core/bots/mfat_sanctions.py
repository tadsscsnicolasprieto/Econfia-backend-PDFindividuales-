# bots/mfat_sanctions.py
import os, re, asyncio, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

# Debe existir un registro Fuente con este nombre
NOMBRE_SITIO = "mfat_sanctions"

URL_SEARCH = "https://www.mfat.govt.nz/search?keyword={q}&x=0&y=0"
GOTO_TIMEOUT_MS = 180_000

# Selectores
SEL_COOKIE_BTN   = "#btn-confirm"  # <button id="btn-confirm" class="btn">Accept</button>
SEL_P_NORESULTS  = "p"             # filtramos por texto "No results found."
SEL_RESULTS_UL   = "ul.search-results"
SEL_RESULT_LI    = "ul.search-results > li"
SEL_ARTICLE      = "article[itemtype='http://schema.org/Article']"
SEL_TITLE_LINK   = "h2 a, a"       # robusto: usualmente h2>a

def _norm(s: str) -> str:
    """ Normaliza: trim, lower, sin tildes, espacios comprimidos. """
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_mfat_sanctions(consulta_id: int, nombre: str, apellido: str):
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
    score_final = 1  # default
    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,  # visible, como pediste
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="en-US",
                timezone_id="America/Bogota",
            )
            page = await context.new_page()

            # 3) Ir directo a la búsqueda
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)
            await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)

            # 3.1) Aceptar cookies si aparece
            try:
                btn = page.locator(SEL_COOKIE_BTN)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click(timeout=5_000)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

            # 4) ¿No results found?
            p_nores = page.locator(SEL_P_NORESULTS, has_text="No results found.")
            if await p_nores.count() > 0 and await p_nores.first.is_visible():
                try:
                    mensaje_final = (await p_nores.first.inner_text()).strip() or "No results found."
                except Exception:
                    mensaje_final = "No results found."
                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True  # consulta válida sin hallazgos (score=1)

            else:
                # 5) Revisar resultados: ul.search-results > li > article[itemtype="http://schema.org/Article"]
                results_ul = page.locator(SEL_RESULTS_UL)
                # si no existe la UL, igual intentamos contar LI/ART por robustez
                items = page.locator(f"{SEL_RESULT_LI} {SEL_ARTICLE}")
                n = await items.count()
                exact_hit = False

                for i in range(n):
                    art = items.nth(i)
                    title_text = ""
                    try:
                        title_text = (await art.locator(SEL_TITLE_LINK).first.inner_text(timeout=3_000)).strip()
                    except Exception:
                        pass

                    if title_text and _norm(title_text) == norm_query:
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

        # 7) Guardar resultado
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
