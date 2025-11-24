# core/bots/ramajudicial_corte_constitucional_magistrados.py
import os, re, unicodedata, asyncio
from urllib.parse import urlencode, quote_plus
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "ramajudicial_corte_constitucional_magistrados"
SEARCH_BASE  = "https://www.ramajudicial.gov.co/search"

SEL_RESULTS_UL = "ul.list-group.list-group-notification.show-quick-actions-on-hover"

def _safe(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

def _norm(s: str) -> str:
    s = (s or "").strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

async def consultar_ramajudicial_corte_constitucional_magistrados(
    consulta_id: int,
    nombre: str,
    apellido: str = "",
    site_id: str | None = None,   # <-- opcional; si lo conoces pásalo: p. ej. "10228", "389063", etc.
):
    # Fuente
    fuente_obj, _ = await sync_to_async(Fuente.objects.get_or_create)(
        nombre=NOMBRE_SITIO,
        defaults={"descripcion": "Rama Judicial – búsqueda por URL (UL de resultados)"}
    )

    # Rutas de salida
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    query_raw = (" ".join([(nombre or "").strip(), (apellido or "").strip()]) or "consulta").strip()
    q_norm    = _norm(query_raw)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_name  = f"{NOMBRE_SITIO}_{_safe(query_raw)}_{ts}.png"
    abs_png   = os.path.join(absolute_folder, png_name)
    rel_png   = os.path.join(relative_folder, png_name).replace("\\", "/")

    # Construir URL de búsqueda
    params = {"q": query_raw}
    if site_id:
        params["site"] = site_id
    url = f"{SEARCH_BASE}?{urlencode(params, quote_via=quote_plus)}"

    score   = 1
    mensaje = "No hay coincidencia exacta en resultados"

    browser = None
    ctx = None
    page = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="es-CO")
            page = await ctx.new_page()

            await page.goto(url, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=25000)
            except Exception:
                pass

            # Esperar el UL de resultados con polling (hasta ~40s)
            found = False
            for _ in range(40):
                try:
                    if await page.locator(SEL_RESULTS_UL).count() > 0:
                        found = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(1.0)

            block_text = ""
            if found:
                try:
                    block_text = await page.locator(SEL_RESULTS_UL).first.inner_text()
                except Exception:
                    block_text = ""

            block_norm = _norm(block_text)

            if block_norm:
                # Coincidencia EXACTA del nombre completo (ignorando acentos/mayúsculas; límites de palabra)
                patt = re.compile(r"(?<!\w)" + re.escape(q_norm) + r"(?!\w)")
                if patt.search(block_norm):
                    score   = 5
                    mensaje = "Coincidencia exacta encontrada en resultados"
                else:
                    score   = 1
                    mensaje = "No hay coincidencia exacta en resultados"
            else:
                score   = 1
                mensaje = "No se encontró la lista de resultados"

            # Pantallazo (full page)
            try:
                await page.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass
            await page.screenshot(path=abs_png, full_page=True)

            await ctx.close()
            await browser.close()
            ctx = browser = None

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score,
            estado="Validado",
            mensaje=mensaje,
            archivo=rel_png,
        )

    except Exception as e:
        # Intenta dejar evidencia
        try:
            if page:
                await page.screenshot(path=abs_png, full_page=True)
        except Exception:
            pass
        try:
            if ctx: await ctx.close()
        except Exception:
            pass
        try:
            if browser: await browser.close()
        except Exception:
            pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje=f"Error al consultar: {e}",
            archivo=rel_png if os.path.exists(abs_png) else "",
        )
