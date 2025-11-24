# consulta/fbi_news.py (versión async adaptada a BD)
import os
import re
import asyncio
import unicodedata
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://www.fbi.gov/news/stories"
NOMBRE_SITIO = "fbi_news"

def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = " ".join(s.split())
    return s.casefold()

async def consultar_fbi_news(consulta_id: int, nombre_completo: str):
    navegador = None
    context = None
    page = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    nombre_completo = (nombre_completo or "").strip()
    if not nombre_completo:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje="Nombre vacío para la consulta.", archivo=""
        )
        return

    # 2) Rutas de salida
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = re.sub(r"[^\w\.-]+", "_", nombre_completo) or "consulta"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_name = f"{NOMBRE_SITIO}_{safe}_{ts}.png"
    absolute_path = os.path.join(absolute_folder, png_name)
    relative_path = os.path.join(relative_folder, png_name)

    async def _leer_total(p) -> int:
        """Devuelve N del “Results: N Items”. 0 si no aparece."""
        try:
            total_p = p.locator(".row.top-total p.right, .top-total .right, p.right:has-text('Results:')")
            if await total_p.count() > 0:
                txt = (await total_p.first.inner_text()).strip()
                txt = " ".join(txt.split())
                m = re.search(r"Results:\s*([\d,\.]+)\s*Items", txt, flags=re.I)
                if m:
                    num = m.group(1).replace(",", "").replace(".", "")
                    return int(num) if num.isdigit() else 0
        except Exception:
            pass
        return 0

    async def _hay_match_exacto(p, objetivo_norm: str) -> bool:
        """
        Busca coincidencia EXACTA del título en cada <li> del listado:
        <ul class="dt-media"> ... <p class="title"><a>...</a>
        Incluye fallbacks por si el layout varía.
        """
        link_sel = (
            "ul.dt-media li .title a, "             # layout actual
            "ul.castle-grid-block-sm-1 li .title a, "
            ".collection-listing .item .title a, "
            "article .title a"
        )
        try:
            links = p.locator(link_sel)
            n = 0
            try:
                n = await links.count()
            except Exception:
                n = 0
            for i in range(n):
                try:
                    t = (await links.nth(i).inner_text() or "").strip()
                    if _norm(t) == objetivo_norm:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    try:
        # 3) Navegación y captura
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            context = await navegador.new_context(viewport={"width": 1440, "height": 900}, locale="en-US")
            page = await context.new_page()

            await page.goto(URL, timeout=120000, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Cookies (best-effort)
            for sel in [
                "button:has-text('Accept')",
                "button#onetrust-accept-btn-handler",
                "button:has-text('I agree')",
            ]:
                try:
                    await page.locator(sel).first.click(timeout=1200)
                    break
                except Exception:
                    pass

            # Filtro
            inp = page.locator("#filter-input")
            await inp.wait_for(state="visible", timeout=60000)
            await inp.click()
            try:
                await inp.fill("")
            except Exception:
                pass
            await inp.type(nombre_completo, delay=25)

            # Enter para aplicar
            try:
                await inp.press("Enter")
            except Exception:
                await page.keyboard.press("Enter")

            # Esperar contenedor
            for sel in [
                ".row.top-total",
                ".dt-media",
                ".collection-listing",
                "section[role='main']",
                "div#content",
            ]:
                try:
                    await page.wait_for_selector(sel, timeout=10000)
                    break
                except Exception:
                    continue

            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(0.6)

            # 4) Total y lógica de score/mensaje
            total = await _leer_total(page)
            if total == 0:
                score_final = 0
                mensaje_final = "Results: 0 items"  # <- en minúsculas como pediste
            else:
                objetivo = _norm(nombre_completo)
                match = await _hay_match_exacto(page, objetivo)
                if match:
                    score_final = 10
                    mensaje_final = "Se han encontrado coincidencias"
                else:
                    score_final = 0
                    mensaje_final = "No se han encontrado coincidencias"

            # 5) Screenshot
            try:
                await page.mouse.wheel(0, 800)
                await asyncio.sleep(0.3)
            except Exception:
                pass
            await page.screenshot(path=absolute_path)

            await context.close()
            await navegador.close()
            navegador = None
            context = None

        # 6) Registrar
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=score_final,
            estado="Validada", mensaje=mensaje_final, archivo=relative_path
        )

    except Exception as e:
        # Cierre y error
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

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje=str(e), archivo=""
        )
