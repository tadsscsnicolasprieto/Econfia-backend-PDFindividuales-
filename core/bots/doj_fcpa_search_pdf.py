# bots/doj_fcpa_search_pdf.py
import os
import re
import asyncio
import unicodedata
from datetime import datetime
from urllib.parse import urlencode
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://www.justice.gov/criminal/criminal-fraud/foreign-corrupt-practices-act"
NOMBRE_SITIO = "doj_fcpa_search_pdf"

def _safe_name(s: str) -> str:
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
    except Exception:
        pass

PRINT_CLEAN = """
@media print {
  header, nav, footer, .usa-header, .usa-footer, .usa-banner,
  .doj-menu, .doj-mobile-menu, .doj-share, .site-feedback,
  #back-to-top, .sticky, .toolbar, .breadcrumb, .region-secondary-menu {
    display:none !important;
  }
  body { margin: 0 !important; padding: 0 !important; }
}
html, body { overflow: visible !important; }
"""

# Selectores estables del buscador DOJ
INPUT_SEL   = "input#search-field-en-small-desktop[name='query']"
SUBMIT_BTN  = "form[role='search'] button[type='submit'], header form button.usa-button[type='submit']"
NORES_SEL   = "#no-results .content-block-item"
RESULT_WRAP = "#results"
RESULT_ITEM = "#results .content-block-item.result h4.title a"

# 403 / CloudFront
_BLOCK403_RE = re.compile(r"(403\s*ERROR|The\s+request\s+could\s+not\s+be\s+satisfied)", re.I)

async def _is_403_blocked(page) -> bool:
    try:
        t = (await page.title() or "")
        if _BLOCK403_RE.search(t): return True
    except Exception:
        pass
    try:
        html = (await page.content() or "")
        if _BLOCK403_RE.search(html): return True
    except Exception:
        pass
    return False

async def consultar_doj_fcpa_search_pdf(consulta_id: int, cedula: str):
    cedula = (cedula or "").strip()
    objetivo_norm = _norm(cedula)

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    if not cedula:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje="La cédula llegó vacía.", archivo=""
        )
        return

    # Rutas de salida
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = _safe_name(cedula)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_pdf_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.pdf")
    out_pdf_rel = os.path.join(relative_folder, os.path.basename(out_pdf_abs)).replace("\\", "/")
    out_png_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.png")
    out_png_rel = os.path.join(relative_folder, os.path.basename(out_png_abs)).replace("\\", "/")

    estado = "Validada"
    score  = 0
    mensaje = ""
    final_url = None

    try:
        # ------------------- FASE 1: Búsqueda + lectura -------------------
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = await browser.new_context(
                viewport={"width": 1440, "height": 1000}, locale="en-US",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"),
            )
            page = await ctx.new_page()

            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)

            # Bloqueo temprano 403
            if await _is_403_blocked(page):
                try: await page.screenshot(path=out_png_abs, full_page=True)
                except Exception: pass
                await browser.close()
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj,
                    score=0, estado="Sin Validar",
                    mensaje="403 ERROR – The request could not be satisfied (bloqueo de acceso DOJ).",
                    archivo=out_png_rel if os.path.exists(out_png_abs) else ""
                )
                return

            try: await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception: pass

            # Ingresar cédula y enviar
            await page.wait_for_selector(INPUT_SEL, state="visible", timeout=20000)
            campo = page.locator(INPUT_SEL).first
            await campo.click(force=True)
            try: await campo.fill("")
            except Exception: pass
            await campo.type(cedula, delay=30)
            try:
                await campo.press("Enter")
            except Exception:
                try: await page.locator(SUBMIT_BTN).first.click(timeout=3000)
                except Exception: pass

            try: await page.wait_for_url("**/search**", timeout=20000)
            except Exception: pass

            # Esperar envoltorio de resultados o el bloque de no resultados
            try:
                await page.wait_for_selector(f"{RESULT_WRAP}, {NORES_SEL}", timeout=10000)
            except Exception:
                pass

            # Bloqueo 403 luego de enviar
            if await _is_403_blocked(page):
                try: await page.screenshot(path=out_png_abs, full_page=True)
                except Exception: pass
                await browser.close()
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj,
                    score=0, estado="Sin Validar",
                    mensaje="403 ERROR – The request could not be satisfied (bloqueo tras la búsqueda).",
                    archivo=out_png_rel if os.path.exists(out_png_abs) else ""
                )
                return

            # Evidencia (captura de pantalla)
            try:
                await asyncio.sleep(0.5)
                await page.screenshot(path=out_png_abs, full_page=True)
            except Exception:
                pass

            # 1) Caso “no results”
            nores = page.locator(NORES_SEL).first
            if await nores.count() > 0 and await nores.is_visible():
                texto = (await nores.inner_text()).strip()
                # Ej: Sorry, no results found for '1016002891'. Try entering...
                mensaje = texto
                score   = 0
                estado  = "Validada"
            else:
                # 2) Revisar resultados → match EXACTO del texto del <a> con la cédula
                exact_hit = False
                links = page.locator(RESULT_ITEM)
                try:
                    n = await links.count()
                    for i in range(n):
                        t = (await links.nth(i).inner_text() or "").strip()
                        if _norm(t) == objetivo_norm:
                            exact_hit = True
                            break
                except Exception:
                    exact_hit = False

                if exact_hit:
                    score   = 10
                    estado  = "Validada"
                    mensaje = f"Coincidencia exacta en resultados para la cédula: {cedula}"
                else:
                    score   = 0
                    estado  = "Validada"
                    mensaje = f"Sin coincidencia exacta para la cédula '{cedula}' en los resultados."

            final_url = page.url
            await browser.close()

        # ------------------- FASE 2: PDF (respaldo) -------------------
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = await ctx.new_page()

            await page.goto(final_url, wait_until="domcontentloaded", timeout=120000)
            try: await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception: pass

            try:
                await page.add_style_tag(content=PRINT_CLEAN)
                await page.emulate_media(media="print")
            except Exception:
                pass

            try:
                await page.pdf(
                    path=out_pdf_abs,
                    format="A4",
                    print_background=True,
                    margin={"top":"10mm","right":"10mm","bottom":"10mm","left":"10mm"},
                    page_ranges="1-2"
                )
            except Exception:
                _fallback_blank_pdf(out_pdf_abs, f"DOJ Search – sin datos visibles para cédula: {cedula}")

            await browser.close()

        # Guardar priorizando PNG (si existe); si no, PDF
        archivo_rel = out_png_rel if os.path.exists(out_png_abs) else out_pdf_rel
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj,
            score=score, estado=estado, mensaje=mensaje, archivo=archivo_rel
        )

    except Exception as e:
        try:
            if not os.path.exists(out_pdf_abs):
                _fallback_blank_pdf(out_pdf_abs, f"DOJ Search – error: {e}")
        except Exception:
            pass
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje=str(e),
            archivo=out_pdf_rel if os.path.exists(out_pdf_abs) else ""
        )
