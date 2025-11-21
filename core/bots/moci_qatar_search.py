# bots/moci_qatar_search.py
import os, re, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "moci_qatar_search"  # Asegúrate de tener esta Fuente creada en BD
URL_SEARCH   = "https://www.moci.gov.qa/en/?s={q}"
GOTO_TIMEOUT_MS = 180_000

# Selectores (WordPress típico)
SEL_NORES_H1   = "h1.page-title"                  # "Nothing Found"
SEL_RESULTS_CT = "main, #main, .site-main"        # contenedor general
SEL_ARTICLE    = "article[id^='post-']"           # cada resultado
SEL_TITLE_A    = f"{SEL_ARTICLE} h2 a, {SEL_ARTICLE} .entry-title a"

def _norm(s: str) -> str:
    """Normaliza: trim, lower, sin tildes, comprime espacios."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_moci_qatar_search(consulta_id: int, nombre: str, apellido: str):
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
    last_error = None

    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,  # visual
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="en-US",
                timezone_id="America/Bogota",
            )
            page = await context.new_page()

            # 3) Ir a la URL de búsqueda
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)
            await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

            # 4) Detectar "Nothing Found"
            nores_h1 = page.locator(SEL_NORES_H1, has_text="Nothing Found")
            if await nores_h1.count() > 0 and await nores_h1.first.is_visible():
                try:
                    # toma el texto exacto del H1 como mensaje
                    mensaje_final = (await nores_h1.first.inner_text()).strip()
                except Exception:
                    mensaje_final = "Nothing Found"
                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True  # consulta válida sin hallazgos (score 1)

            else:
                # 5) Hay resultados: iterar artículos <article id="post-...">
                results_container = page.locator(SEL_RESULTS_CT)
                # si no hay contenedor, igual buscamos por artículos
                articles = page.locator(SEL_ARTICLE)
                n = await articles.count()
                exact_hit = False

                for i in range(n):
                    art = articles.nth(i)
                    try:
                        title = (await art.locator(SEL_TITLE_A).first.inner_text(timeout=3_000)).strip()
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

            # 6) Cierre de navegador
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 7) Persistencia del Resultado
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
