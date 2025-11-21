# bots/epa_fugitives_search_pdf.py
import os
import re
import asyncio
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://www.epa.gov/enforcement/epa-fugitives"
NOMBRE_SITIO = "epa_fugitives_search_pdf"

def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

def _fallback_blank_pdf(out_pdf_abs: str, text: str):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        os.makedirs(os.path.dirname(out_pdf_abs), exist_ok=True)
        c = canvas.Canvas(out_pdf_abs, pagesize=A4)
        w, h = A4
        c.setFont("Helvetica", 12)
        c.drawCentredString(w/2, h/2, text)
        c.save()
        return True
    except Exception:
        return False

PRINT_CLEAN = """
@media print {
  header, nav, footer, .usa-banner, .epac-advisory, .sticky, .back-to-top,
  .site-header, .site-footer, .feedback, .social-media { display:none !important; }
  body { margin: 0 !important; padding: 0 !important; }
}
html, body { overflow: visible !important; }
"""

# ====== NUEVO: limpieza ligera para PNG (evita headers pegajosos) ======
SCREEN_CLEAN = """
header, nav, footer, .usa-banner, .sticky, .back-to-top, .site-header, .site-footer {
  display:none !important; visibility:hidden !important; height:0!important; overflow:hidden!important;
}
*[style*="position:fixed"] { position: static !important; }
html, body { margin:0!important; padding:0!important; }
"""

CONTENT_SELECTORS = [
    "#main-content", "#content", ".view-content", ".search-results", "main", "body"
]

async def consultar_epa_fugitives_search_pdf(consulta_id: int, nombre: str):
    """
    Busca un nombre en EPA Fugitives, puntúa por coincidencia exacta en cada resultado,
    genera PNG (para 'archivo') y además PDF (1–2 páginas) como respaldo.
    """
    navegador = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    nombre = (nombre or "").strip()
    if not nombre:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje="El nombre llegó vacío.", archivo=""
        )
        return

    # Salida
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = _safe_name(nombre)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    out_pdf_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.pdf")
    out_pdf_rel = os.path.join(relative_folder, os.path.basename(out_pdf_abs))

    out_png_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.png")
    out_png_rel = os.path.join(relative_folder, os.path.basename(out_png_abs))

    INPUT = "#search-box"

    score_final = 1
    mensaje_final = "0 resultados"

    try:
        # -------- FASE 1: navegar y ejecutar búsqueda (headful) --------
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = await navegador.new_context(
                viewport={"width": 1440, "height": 1000},
                device_scale_factor=2,           # PNG nítido
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"),
                locale="en-US"
            )
            page = await ctx.new_page()

            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

            await page.wait_for_selector(INPUT, state="visible", timeout=15000)
            campo = page.locator(INPUT)
            await campo.click(force=True)
            try:
                await campo.fill("")
            except Exception:
                pass
            await campo.type(nombre, delay=25)
            try:
                await campo.press("Enter")
            except Exception:
                pass

            # Esperar que el área de resultados aparezca (hay 2 variantes de texto)
            nores_sel   = "span.searchUI_color:has-text('No results for')"
            results_sel = "span.searchUI_color:has-text('Results ')"
            try:
                await page.wait_for_selector(f"{nores_sel}, {results_sel}", timeout=15000)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(2.0)

            # ------ SCORE por coincidencia exacta en cada resultado ------
            # Lista Angular: div[ng-repeat*='data.response.docs']
            items = page.locator("div[ng-repeat*='data.response.docs']")
            total_items = await items.count()
            if total_items == 0:
                score_final = 1
                mensaje_final = "0 resultados"
            else:
                exacto = False
                pattern = re.compile(rf"\b{re.escape(nombre.strip())}\b", re.I)
                # Limito por seguridad
                to_check = min(total_items, 50)
                for i in range(to_check):
                    txt = (await items.nth(i).inner_text() or "").strip()
                    if pattern.search(txt):
                        exacto = True
                        break
                if exacto:
                    score_final = 5
                    mensaje_final = "Coincidencia exacta en resultados"
                else:
                    score_final = 1
                    mensaje_final = "Resultados sin coincidencia exacta"

            # ------ PNG limpio ------
            try:
                await page.add_style_tag(content=SCREEN_CLEAN)
            except Exception:
                pass

            tomado = False
            for sel in CONTENT_SELECTORS:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.screenshot(path=out_png_abs, type="png")
                        tomado = True
                        break
                except Exception:
                    continue
            if not tomado:
                await page.screenshot(path=out_png_abs, full_page=True, type="png")

            final_url = page.url

            try:
                await ctx.close()
            except Exception:
                pass
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # -------- FASE 2: imprimir PDF (opcional, respaldo) --------
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = await ctx.new_page()

            await page.goto(final_url, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            try:
                await page.add_style_tag(content=PRINT_CLEAN)
            except Exception:
                pass
            try:
                await page.emulate_media(media="print")
            except Exception:
                pass

            await page.pdf(
                path=out_pdf_abs,
                format="A4",
                print_background=True,
                margin={"top":"10mm","right":"10mm","bottom":"10mm","left":"10mm"},
                page_ranges="1-2"
            )

            await ctx.close()
            await browser.close()

        # Fallback por si el PDF quedó vacío
        if not os.path.exists(out_pdf_abs) or os.path.getsize(out_pdf_abs) < 500:
            _fallback_blank_pdf(out_pdf_abs, f"EPA – sin datos visibles para: {nombre}")

        # Registrar (GUARDANDO EL PNG en 'archivo')
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj,
            score=score_final, estado="Validada",
            mensaje=mensaje_final, archivo=out_png_rel
        )

    except Exception as e:
        try:
            _fallback_blank_pdf(out_pdf_abs, f"EPA – error: {e}")
        except Exception:
            pass
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje=str(e), archivo=""
        )
