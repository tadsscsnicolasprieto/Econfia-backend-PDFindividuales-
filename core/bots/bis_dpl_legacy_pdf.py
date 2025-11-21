# bots/bis_dpl_legacy_pdf.py
import os, re, asyncio
from datetime import datetime
from urllib.parse import urlencode
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL_START = "https://www.bis.doc.gov/index.php/the-denied-persons-list"
URL_SEARCH_FALLBACK = "https://www.bis.doc.gov/index.php"
NOMBRE_SITIO = "bis_dpl_legacy_pdf"

def _safe(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

PRINT_CLEAN = """
@media print {
  header, nav, footer, .footer, .header, .moduletable, .skip-link,
  .navbar, .breadcrumbs, .menu, .cookie-consent, .feedback, .print:hidden {
    display: none !important;
  }
  body { margin: 0 !important; padding: 0 !important; }
}
"""

# Patrones típicos de página de bloqueo / error de BIS
_BLOCK_PATTERNS = re.compile(
    r"(This\s+action\s+is\s+unavailable|The\s+page\s+cannot\s+be\s+displayed|BIS\s+support\s+team|Message\s+ID:\s*[A-Z0-9\-]+)",
    re.IGNORECASE
)
def _is_block_page(html: str) -> bool:
    return bool(html and _BLOCK_PATTERNS.search(html))

SEARCH_INPUT = "#mod-search-searchword69"  # buscador del sitio

async def consultar_bis_dpl_legacy_pdf(consulta_id: int, cedula: str):
    """Busca en el DPL por cédula, genera PDF y PNG. Si hay bloqueo/error → Sin Validar, score=0."""
    cedula = (cedula or "").strip()
    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)

    if not cedula:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj,
            score=0, estado="Sin Validar",
            mensaje="La cédula llegó vacía.", archivo=""
        )
        return

    # Rutas de salida
    carpeta_rel = os.path.join("resultados", str(consulta_id))
    carpeta_abs = os.path.join(settings.MEDIA_ROOT, carpeta_rel)
    os.makedirs(carpeta_abs, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = _safe(cedula)
    out_pdf_abs = os.path.join(carpeta_abs, f"{NOMBRE_SITIO}_{safe}_{ts}.pdf")
    out_pdf_rel = os.path.join(carpeta_rel, os.path.basename(out_pdf_abs)).replace("\\", "/")
    out_png_abs = os.path.join(carpeta_abs, f"{NOMBRE_SITIO}_{safe}_{ts}.png")
    out_png_rel = os.path.join(carpeta_rel, os.path.basename(out_png_abs)).replace("\\", "/")

    # Defaults
    message_text = ""
    score_val = 0
    final_url = URL_START

    screenshot_on_error = ""  # si hay error, intentamos guardar PNG aquí

    try:
        # ---------- Fase visible: búsqueda por CÉDULA y lectura del contador ----------
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = await ctx.new_page()

            await page.goto(URL_START, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Buscar por cédula
            triggered = False
            try:
                await page.wait_for_selector(SEARCH_INPUT, state="visible", timeout=7000)
                inp = page.locator(SEARCH_INPUT)
                await inp.click(force=True)
                try:
                    await inp.fill("")
                except Exception:
                    pass
                await inp.type(cedula, delay=25)
                await page.keyboard.press("Enter")
                triggered = True
            except Exception:
                triggered = False

            # Fallback: armar query en home si no se activó el buscador
            if not triggered:
                qs = urlencode({"searchword": cedula, "searchphrase": "all"})
                await page.goto(f"{URL_SEARCH_FALLBACK}?{qs}", wait_until="domcontentloaded", timeout=120000)

            # Esperar contenedor de resultados
            for sel in ["div.searchintro", "div.search-result", "div.search-results", "div#search-results", "article", "main"]:
                try:
                    await page.wait_for_selector(sel, timeout=8000)
                    break
                except Exception:
                    continue

            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await page.wait_for_timeout(700)

            # ¿Página de bloqueo?
            try:
                html = await page.content()
                if _is_block_page(html):
                    # Dejar evidencia
                    try:
                        await page.screenshot(path=out_png_abs, full_page=True)
                        screenshot_on_error = out_png_rel
                    except Exception:
                        pass
                    await browser.close()
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id, fuente=fuente_obj,
                        score=0, estado="Sin Validar",
                        mensaje="La fuente BIS bloqueó el acceso (página de error/bloqueo).",
                        archivo=screenshot_on_error or ""
                    )
                    return
            except Exception:
                pass

            # Leer "Total: N results found."
            try:
                strong_loc = page.locator("div.searchintro strong").first
                if await strong_loc.count() > 0:
                    text = (await strong_loc.inner_text() or "").strip()
                    message_text = text
                    m = re.search(r"Total:\s*(\d+)\s+results\s+found", text, flags=re.I)
                    if m:
                        n = int(m.group(1))
                        score_val = 10 if n > 0 else 0
                else:
                    badge_loc = page.locator("div.searchintro .badge-info").first
                    if await badge_loc.count() > 0:
                        n_text = (await badge_loc.inner_text()).strip()
                        n = int(re.sub(r"[^\d]", "", n_text) or "0")
                        message_text = f"Total: {n} results found."
                        score_val = 10 if n > 0 else 0
            except Exception:
                # si no pudimos leer, deja un mensaje genérico
                message_text = "Consulta generada (revisar evidencia)."
                score_val = 0

            final_url = page.url

            # Screenshot de la página de resultados como evidencia rápida
            try:
                await page.screenshot(path=out_png_abs, full_page=True)
                screenshot_on_error = out_png_rel
            except Exception:
                pass

            await browser.close()

        # ---------- Fase headless: generar PDF y PNG (desde PDF si es posible) ----------
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = await ctx.new_page()

            await page.goto(final_url, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

            # Detectar bloqueo aquí también
            try:
                html = await page.content()
                if _is_block_page(html):
                    try:
                        await page.screenshot(path=out_png_abs, full_page=True)
                        screenshot_on_error = out_png_rel
                    except Exception:
                        pass
                    await browser.close()
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id, fuente=fuente_obj,
                        score=0, estado="Sin Validar",
                        mensaje="La fuente BIS bloqueó el acceso (página de error/bloqueo).",
                        archivo=screenshot_on_error or ""
                    )
                    return
            except Exception:
                pass

            # Limpiar para impresión y exportar PDF
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
                page_ranges="1",
            )

            # Intentar PNG desde el PDF (mejor calidad)
            png_ok = False
            try:
                from pdf2image import convert_from_path
                imgs = convert_from_path(out_pdf_abs, dpi=220, first_page=1, last_page=1)
                if imgs:
                    imgs[0].save(out_png_abs, "PNG")
                    png_ok = True
            except Exception:
                png_ok = False

            if not png_ok:
                # Fallback: screenshot de la página
                try:
                    await page.screenshot(path=out_png_abs, full_page=True)
                except Exception:
                    pass

            await browser.close()

        # ---------- Registrar en BD (usar PNG como 'archivo' para el consolidado) ----------
        msg_out = message_text or "Consulta generada (revisar evidencia)."
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_val,
            estado="Validada",
            mensaje=msg_out,
            archivo=out_png_rel if os.path.exists(out_png_abs) else out_pdf_rel,
        )

    except Exception as e:
        # Error: dejar Sin Validar + PNG si lo hubo
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj,
            score=0, estado="Sin Validar",
            mensaje=f"La fuente está presentando problemas para la consulta: {e}",
            archivo=screenshot_on_error or "",
        )
