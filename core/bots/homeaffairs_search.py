# bots/homeaffairs_search.py
import os, re, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "homeaffairs_search"
URL_SEARCH = "https://www.homeaffairs.gov.au/sitesearch?k={q}"
GOTO_TIMEOUT_MS = 180_000

# Selectores
SEL_NORES_WRAPPER = "div.search-results-list"
SEL_NORES_H4      = f"{SEL_NORES_WRAPPER} > h4"
SEL_RESULT_ITEM   = "ha-result-item"
SEL_RESULT_TITLE  = "ha-result-item a"   # fallback, el primer <a> dentro del item

def _norm(s: str) -> str:
    """Normaliza para comparación exacta 'humana': minúsculas, sin diacríticos, espacios comprimidos."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_homeaffairs_search(consulta_id: int, nombre: str, apellido: str):
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
    score_final = 1  # por defecto 1 (sólo sube a 5 si hay match exacto)
    success = False
    last_error = None

    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,  # <- como pediste
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="en-AU",
                timezone_id="Australia/Sydney",  # el sitio es AU; no afecta lógica
            )
            page = await context.new_page()

            # 3) Ir a la URL de búsqueda
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)
            await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=45_000)
            except Exception:
                pass

            # 4) Detectar "No results"
            nores_h4 = page.locator(SEL_NORES_H4, has_text="No results")
            if await nores_h4.count() > 0 and await nores_h4.first.is_visible():
                # Intentar capturar el bloque completo tal cual lo muestra el sitio
                try:
                    wrapper = page.locator(SEL_NORES_WRAPPER).first
                    wrapper_txt = (await wrapper.inner_text()).strip()
                    # Si no logramos extraer todo, dejamos un mensaje canónico
                    if not wrapper_txt:
                        raise Exception("wrapper vacío")
                    mensaje_final = wrapper_txt
                except Exception:
                    # Mensaje canónico del usuario (manteniendo estructura)
                    mensaje_final = (
                        "No results\n"
                        f"Unfortunately there were no results for {full_name}\n"
                        "Try refining your search with some different key words or looking under a different function"
                    )

                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass

                success = True  # consulta válida sin hallazgos (score 1)

            else:
                # 5) Hay resultados -> iterar <ha-result-item>
                items = page.locator(SEL_RESULT_ITEM)
                n = await items.count()
                exact_hit = False

                for i in range(n):
                    item = items.nth(i)
                    title_text = ""

                    # Intentar obtener el texto del título principal (primer <a>)
                    try:
                        if await item.locator(SEL_RESULT_TITLE).count() > 0:
                            title_text = (await item.locator(SEL_RESULT_TITLE).first.inner_text(timeout=3_000)).strip()
                    except Exception:
                        title_text = ""

                    if not title_text:
                        # Fallback: usa todo el inner_text del item para no perder coincidencias
                        try:
                            title_text = (await item.inner_text(timeout=2_000)).strip()
                        except Exception:
                            title_text = ""

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

            # 6) Cierre
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 7) Persistir Resultado
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
                score=1,
                estado="Sin Validar",
                mensaje=last_error or "No fue posible obtener resultados.",
                archivo=relative_png
            )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1,
                estado="Sin Validar",
                mensaje=str(e),
                archivo=""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
