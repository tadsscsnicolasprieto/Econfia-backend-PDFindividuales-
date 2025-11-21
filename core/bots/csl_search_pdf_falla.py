# bots/csl_search_pdf.py
import os, re, asyncio, unicodedata
from datetime import datetime
from urllib.parse import urlencode
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

HOME_URL = "https://www.trade.gov/"
SEARCH_FALLBACK = "https://www.trade.gov/trade-search"
NOMBRE_SITIO = "csl_search_pdf"

def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) == "Mn")  # ← solo tildes
    # OJO: la línea correcta es eliminar las marcas, no quedarnos con ellas:
    # s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    # Si prefieres la versión correcta, usa la de arriba.
    return re.sub(r"\s+", " ", s).strip().casefold()

PRINT_CLEAN = """
@media print {
  header, nav, footer, .usa-banner, .usa-header, .site-feedback,
  #feedback, .feedback, .usa-footer, .usa-nav { display:none !important; }
  body { margin: 0 !important; padding: 0 !important; }
}
html, body { overflow: visible !important; }
"""

async def consultar_csl_search_pdf(consulta_id: int, nombre: str):
    nombre = (nombre or "").strip()

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    if not nombre:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje="El nombre llegó vacío.", archivo=""
        )
        return

    # Rutas
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = _safe_name(nombre)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_pdf_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.pdf")
    out_pdf_rel = os.path.join(relative_folder, os.path.basename(out_pdf_abs)).replace("\\", "/")
    out_png_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.png")
    out_png_rel = os.path.join(relative_folder, os.path.basename(out_png_abs)).replace("\\", "/")

    final_url = None
    exact_match = False

    try:
        # ---------- Fase 1: buscar y ESPERAR la navegación ----------
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="en-US",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/119.0.0.0 Safari/537.36")
            )
            page = await ctx.new_page()

            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=120000)

            # Intentar input del header
            used_header = False
            for sel in ['#header-search', 'input.ita-header__search-input[name="q"]']:
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=5000)
                    await page.click(sel, force=True)
                    try:
                        await page.fill(sel, "")
                    except Exception:
                        pass
                    # Esperar navegación de forma segura
                    async with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                        await page.type(sel, nombre, delay=15)
                        await page.keyboard.press("Enter")
                    used_header = True
                    break
                except Exception:
                    continue

            # Fallback directo
            if not used_header:
                qs = urlencode({"q": nombre})
                await page.goto(f"{SEARCH_FALLBACK}?{qs}", wait_until="domcontentloaded", timeout=120000)

            # Esperar a que se renderice el contenedor de resultados, con timeout razonable
            try:
                await page.wait_for_selector(".ResultsList", state="attached", timeout=20000)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await asyncio.sleep(0.8)  # pequeño respiro para hidratar

            # === Coincidencia EXACTA en los títulos de resultados ===
            objetivo = _norm(nombre)
            try:
                links = page.locator(".ResultsList .anItem a")
                n = await links.count()
                for i in range(n):
                    txt = (await links.nth(i).inner_text() or "").strip()
                    if _norm(txt) == objetivo:
                        exact_match = True
                        break
            except Exception:
                exact_match = False

            # Pantallazo SIEMPRE (luego de que todo está cargado)
            try:
                await page.screenshot(path=out_png_abs, full_page=True)
            except Exception:
                pass

            final_url = page.url
            await browser.close()

        # ---------- Fase 2: PDF (1–2 páginas) ----------
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = await ctx.new_page()
            await page.goto(final_url, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.add_style_tag(content=PRINT_CLEAN)
                await page.emulate_media(media="print")
            except Exception:
                pass

            await page.pdf(
                path=out_pdf_abs,
                format="A4",
                print_background=True,
                margin={"top": "10mm", "right": "10mm", "bottom": "10mm", "left": "10mm"},
                page_ranges="1-2"
            )
            await browser.close()

        # Score/Mensaje según coincidencia exacta
        score = 10 if exact_match else 0
        mensaje = "Se ha encontrado coincidencia." if exact_match else "No se han encontrado coincidencias."

        # Preferimos guardar el PNG como archivo; si falló, guardamos el PDF
        archivo_rel = out_png_rel if os.path.exists(out_png_abs) else out_pdf_rel

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=score,
            estado="Validada", mensaje=mensaje, archivo=archivo_rel
        )

    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje=str(e), archivo=""
        )
