# bots/bis_unverified_pdf.py
import os, re, unicodedata
from datetime import datetime
from urllib.parse import urlencode
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL_START    = "https://www.bis.gov/regulations/ear/part-744/supplement-6-744/unverified-list"
URL_SEARCH   = "https://www.bis.gov/search"
NOMBRE_SITIO = "bis_unverified_pdf"

def _safe(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return s

PRINT_CLEAN = """
@media print {
  header, nav, footer, .usa-footer, .usa-header, .usa-banner,
  .site-feedback, .feedback, .skip-to-content, .sticky, .print:hidden {
    display: none !important;
  }
  body { margin: 0 !important; padding: 0 !important; }
}
"""

H2_RESULTS = "div.mb-4 h2.text-heading-sm.font-bold"
# ⚠️ Nada de 'sm:w-[73%]'. Tomamos todos los anchors de resultados dentro de main
RESULT_LINKS = "main a.hyperlink"

SEARCH_INPUTS = [
    'header input[placeholder*="Search" i]',
    'nav input[placeholder*="Search" i]',
    'input[type="text"][placeholder*="Search" i]',
]

async def consultar_bis_unverified_pdf(consulta_id: int, nombre: str):
    nombre = (nombre or "").strip()
    objetivo_norm = _norm(nombre)

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
            estado="Sin Validar", mensaje="El término de búsqueda llegó vacío.", archivo=""
        )
        return

    # Rutas
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = _safe(nombre)

    out_pdf_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.pdf")
    out_pdf_rel = os.path.join(relative_folder, os.path.basename(out_pdf_abs)).replace("\\", "/")

    out_png_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.png")
    out_png_rel = os.path.join(relative_folder, os.path.basename(out_png_abs)).replace("\\", "/")

    mensaje   = ""
    score     = 0
    final_url = URL_START

    try:
        # ------------ Fase visible: búsqueda + lectura de resultados ------------
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="en-US",
            )
            page = await ctx.new_page()

            await page.goto(URL_START, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

            # Buscar input
            used = False
            for sel in SEARCH_INPUTS:
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=5000)
                    inp = page.locator(sel).first
                    await inp.click(force=True)
                    try: await inp.fill("")
                    except Exception: pass
                    await inp.type(nombre, delay=25)
                    await page.keyboard.press("Enter")
                    used = True
                    break
                except Exception:
                    continue

            if not used:
                qs = urlencode({"q": nombre})
                await page.goto(f"{URL_SEARCH}?{qs}", wait_until="domcontentloaded", timeout=120000)

            # Esperar algo de resultados
            for sel in ["main .search-results", "main .results", "main article", "main"]:
                try:
                    await page.wait_for_selector(sel, timeout=8000)
                    break
                except Exception:
                    continue
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await page.wait_for_timeout(600)

            # Evidencia
            try:
                await page.screenshot(path=out_png_abs, full_page=True)
            except Exception:
                pass

            # Leer encabezado (por si dice 0 results)
            header_text = ""
            try:
                if await page.locator(H2_RESULTS).count() > 0:
                    header_text = (await page.locator(H2_RESULTS).first.inner_text() or "").strip()
            except Exception:
                header_text = ""

            # Recolectar anchors de resultados del contenedor principal
            exact_hit = False
            items = []
            try:
                links = page.locator(RESULT_LINKS)
                n = await links.count()
                for i in range(n):
                    txt = (await links.nth(i).inner_text() or "").strip()
                    if not txt:
                        continue
                    items.append(txt)
                    if _norm(txt) == objetivo_norm:
                        exact_hit = True
                # Si no hay anchors con clase, fallback a cualquier <a> dentro de main
                if not items:
                    any_links = page.locator("main a")
                    m = await any_links.count()
                    for i in range(m):
                        txt = (await any_links.nth(i).inner_text() or "").strip()
                        if not txt:
                            continue
                        items.append(txt)
                        if _norm(txt) == objetivo_norm:
                            exact_hit = True
            except Exception:
                pass

            if exact_hit:
                score = 10
                mensaje = f"Coincidencia exacta en resultados: {nombre}"
            else:
                score = 0
                # Si el header dice 0 results, úsalo tal cual; de lo contrario explica que no hubo match exacto
                m = re.search(r"^\s*0\s+results\b", header_text or "", flags=re.I)
                if m:
                    # Dejar solo el texto plano del H2
                    mensaje = header_text
                else:
                    mensaje = f"Sin coincidencia exacta para '{nombre}' en los resultados."

            final_url = page.url
            await browser.close()

        # ------------ Fase headless: PDF (respaldo) ------------
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = await ctx.new_page()

            await page.goto(final_url, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

            try: await page.add_style_tag(content=PRINT_CLEAN)
            except Exception: pass
            try: await page.emulate_media(media="print")
            except Exception: pass

            try:
                await page.pdf(
                    path=out_pdf_abs,
                    format="A4",
                    print_background=True,
                    margin={"top": "10mm", "right": "10mm", "bottom": "10mm", "left": "10mm"},
                    page_ranges="1"
                )
            except Exception:
                pass

            await browser.close()

        # Guardar: prioriza PNG; si falló, usa PDF
        archivo_rel = out_png_rel if os.path.exists(out_png_abs) else out_pdf_rel
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj,
            score=score,
            estado="Validada",
            mensaje=mensaje,
            archivo=archivo_rel
        )

    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje=str(e), archivo=""
        )
