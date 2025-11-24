# consulta/fbi_topten.py (versión async adaptada a BD)
from playwright.async_api import async_playwright
import os
import re
import asyncio
import unicodedata
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente

NOMBRE_SITIO = "fbi"
URL = "https://www.fbi.gov/wanted/topten"

def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = " ".join(s.split())
    return s.casefold()

async def consultar_fbi(consulta_id: int, nombre: str):
    """
    Abre el Top Ten del FBI, filtra por `nombre`, toma un pantallazo
    y evalúa coincidencia EXACTA contra los títulos de cada tarjeta:
      - score=10 y mensaje="Se ha encontrado coincidencia." si existe match exacto
      - score=0  y mensaje="No se han encontrado coincidencias." si no
    Guarda la captura en MEDIA_ROOT/resultados/<consulta_id>/ y crea el registro en BD.
    """
    navegador = None
    context = None
    page = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo="",
        )
        return

    nombre = (nombre or "").strip()
    if not nombre:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje="El nombre llegó vacío.",
            archivo="",
        )
        return

    # Carpeta resultados/<consulta_id>
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_name = f"{NOMBRE_SITIO}_{consulta_id}_{timestamp}.png"
    absolute_path = os.path.join(absolute_folder, screenshot_name)
    relative_path = os.path.join(relative_folder, screenshot_name)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            context = await navegador.new_context(
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36")
            )
            page = await context.new_page()

            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Aceptar cookies (best-effort)
            for sel in [
                "button#onetrust-accept-btn-handler",
                "button:has-text('Accept')",
                "button:has-text('I agree')",
            ]:
                try:
                    await page.locator(sel).first.click(timeout=1200)
                    break
                except Exception:
                    pass

            # Filtrar por nombre
            await page.wait_for_selector("#filter-input", timeout=30000)
            await page.fill("#filter-input", nombre)

            # Click o Enter
            try:
                await page.click("button.plone-btn.plone-btn-default", timeout=5000)
            except Exception:
                try:
                    await page.locator("#filter-input").press("Enter")
                except Exception:
                    pass

            # Esperar a que la grilla/listado esté disponible
            for sel in [
                "ul.full-grid.wanted-grid-natural",
                "ul.full-grid",
                ".collection-listing",
                "section[role='main']",
                "div#content",
            ]:
                try:
                    await page.wait_for_selector(sel, timeout=10000)
                    break
                except Exception:
                    continue

            # Pequeño settle
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            await asyncio.sleep(0.6)

            # Buscar coincidencia EXACTA en cada <li> -> <h3 class="title"> <a>TEXT</a>
            exact_match = False
            try:
                link_sel = (
                    "ul.full-grid li .title a, "
                    "ul.full-grid.wanted-grid-natural li .title a, "
                    ".collection-listing .item h3 a, "
                    "article h3 a"
                )
                links = page.locator(link_sel)
                count = 0
                try:
                    count = await links.count()
                except Exception:
                    count = 0

                objetivo = _norm(nombre)
                for i in range(count):
                    try:
                        t = (await links.nth(i).inner_text() or "").strip()
                        if _norm(t) == objetivo:
                            exact_match = True
                            break
                    except Exception:
                        continue
            except Exception:
                exact_match = False

            score_final = 10 if exact_match else 0
            mensaje_final = "Se ha encontrado coincidencia." if exact_match else "No se han encontrado coincidencias."

            # Captura (best-effort)
            try:
                await page.mouse.wheel(0, 800)
                await asyncio.sleep(0.3)
            except Exception:
                pass
            await page.screenshot(path=absolute_path, full_page=True)

            await context.close()
            await navegador.close()
            navegador = None
            context = None

        # Registrar en BD
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,   # SOLO TEXTO
            archivo=relative_path,
        )

    except Exception as e:
        # Cierre defensivo
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if navegador is not None:
                await navegador.close()
        except Exception:
            pass

        # Registrar error
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje=str(e),
            archivo="",
        )
