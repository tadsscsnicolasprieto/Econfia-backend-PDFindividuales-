# bots/guardia_civil_buscados_pdf.py
import os
import re
import unicodedata
import asyncio
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://web.guardiacivil.es/es/colaboracion/Buscados/buscados/"
NOMBRE_SITIO = "guardia_civil_buscados_pdf"

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

def _fallback_blank_png(out_png_abs: str, text: str):
    """Crea un PNG simple con texto si no pudimos capturar pantalla."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        os.makedirs(os.path.dirname(out_png_abs), exist_ok=True)
        img = Image.new("RGB", (1280, 720), (32, 32, 32))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("arial.ttf", 20)
        except Exception:
            font = ImageFont.load_default()
        draw.text((40, 40), text, fill=(240, 240, 240), font=font)
        img.save(out_png_abs)
        return True
    except Exception:
        return False

def _normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    s = s.replace("\xa0", " ")
    return s.strip()

PRINT_CLEAN = """
@media print {
  header, nav, footer, .cookie, #onetrust-banner-sdk, #onetrust-pc-sdk,
  .usa-banner, .usa-identifier, .cmpbox, .cmpbox-container,
  .sede-electronica, .cmpboxbtns, .cmpboxclose, .site-messages { display:none !important; }
  body { margin: 0 !important; padding: 0 !important; }
}
html, body { overflow: visible !important; }
"""

async def consultar_guardia_civil_buscados_pdf(consulta_id: int, nombre: str):
    """
    - Guarda PNG en BD (archivo): lista si no hay resultados, detalle si hay match exacto.
    - No hay resultados: score=0, mensaje="No hay resultados".
    - Match exacto: score=10, mensaje="se encontraron resultados".
    - PDF se genera (auditoría) pero NO se guarda en BD.
    """
    nombre = (nombre or "").strip()
    navegador = None
    ctx = None
    page = None
    final_url = URL
    score_final = 0
    mensaje_final = "No hay resultados"

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
    rel_folder = os.path.join("resultados", str(consulta_id))
    abs_folder = os.path.join(settings.MEDIA_ROOT, rel_folder)
    os.makedirs(abs_folder, exist_ok=True)

    safe = _safe_name(nombre)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_pdf_abs = os.path.join(abs_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.pdf")
    out_png_lista_abs = os.path.join(abs_folder, f"{NOMBRE_SITIO}_{safe}_{ts}_lista.png")
    out_png_detalle_abs = os.path.join(abs_folder, f"{NOMBRE_SITIO}_{safe}_{ts}_detalle.png")

    # por defecto, guardaremos la lista
    selected_png_abs = out_png_lista_abs

    # Selectores
    INPUT_SEARCH = "input.filtro_busqueda-opciones[placeholder='Término de búsqueda'], input[placeholder='Término de búsqueda']"
    SEL_UL = "ul.listado, ul[class*='listado']"
    SEL_ITEM = "ul.listado li.SagaListado-Buscado-individual, li.SagaListado-Buscado-individual"
    SEL_H3 = "h3.nombre-buscado"
    ENLACE_ITEM = "a.enlace_elemento"
    COOKIE_ACCEPT = "#aceptarCookie, #aceptarCookies, #aceptaCookies, button#onetrust-accept-btn-handler"

    try:
        # ====== FASE 1: visible ======
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = await navegador.new_context(
                viewport={"width": 1440, "height": 1000},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/119.0.0.0 Safari/537.36"),
                locale="es-ES",
                bypass_csp=True,
            )
            page = await ctx.new_page()

            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Cookies
            try:
                btn = page.locator(COOKIE_ACCEPT)
                if await btn.count() and await btn.first.is_visible():
                    await btn.first.click()
            except Exception:
                pass

            # Buscar
            await page.wait_for_selector(INPUT_SEARCH, state="visible", timeout=25000)
            inp = page.locator(INPUT_SEARCH).first
            await inp.click(force=True)
            try:
                await inp.fill("")
            except Exception:
                pass
            await inp.type(nombre, delay=20)
            try:
                await inp.press("Enter")
            except Exception:
                pass

            # Screenshot temprano (respaldo)
            try:
                await asyncio.sleep(0.8)
                await page.screenshot(path=out_png_lista_abs, full_page=True)
            except Exception:
                pass

            # Lazy-load
            try:
                for _ in range(6):
                    await page.mouse.wheel(0, 800)
                    await asyncio.sleep(0.25)
            except Exception:
                pass

            # Espera flexible
            try:
                await page.wait_for_function(
                    """() => {
                        const ul = document.querySelector("ul.listado, ul[class*='listado']");
                        const items = document.querySelectorAll("li.SagaListado-Buscado-individual");
                        const h3 = document.querySelector("h3.nombre-buscado");
                        return !!(ul || (items && items.length) || h3);
                    }""",
                    timeout=25000
                )
            except Exception:
                final_url = page.url
                mensaje_final = "No hay resultados"
            else:
                buscado_norm = _normalize(nombre)
                match_idx = -1
                total = 0
                try:
                    items = page.locator(SEL_ITEM)
                    try:
                        await items.first.wait_for(state="visible", timeout=5000)
                    except Exception:
                        pass
                    total = await items.count()
                except Exception:
                    total = 0

                # Refrescar screenshot lista
                try:
                    await page.screenshot(path=out_png_lista_abs, full_page=True)
                except Exception:
                    pass

                if total > 0:
                    for i in range(total):
                        itm = items.nth(i)
                        try:
                            h3 = itm.locator(SEL_H3)
                            if await h3.count() == 0:
                                continue
                            txt = (await h3.first.inner_text()).strip()
                            if _normalize(txt) == buscado_norm:
                                match_idx = i
                                break
                        except Exception:
                            continue

                if match_idx >= 0:
                    score_final = 10
                    mensaje_final = "se encontraron resultados"
                    try:
                        with page.expect_navigation(timeout=20000):
                            await items.nth(match_idx).locator(ENLACE_ITEM).first.click()
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass
                    except Exception:
                        pass

                    final_url = page.url
                    # Screenshot detalle
                    try:
                        await page.screenshot(path=out_png_detalle_abs, full_page=True)
                        selected_png_abs = out_png_detalle_abs
                    except Exception:
                        # si falla, nos quedamos con el de lista
                        selected_png_abs = out_png_lista_abs
                else:
                    final_url = page.url
                    score_final = 0
                    mensaje_final = "No hay resultados"
                    selected_png_abs = out_png_lista_abs

            # Cierre visible
            try:
                await ctx.close()
            except Exception:
                pass
            try:
                await navegador.close()
            except Exception:
                pass
            navegador, ctx, page = None, None, None

        # ====== FASE 2: PDF headless (opcional, no se guarda en BD) ======
        async with async_playwright() as p:
            browser2 = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            ctx2 = await browser2.new_context(viewport={"width": 1440, "height": 1000}, locale="es-ES")
            page2 = await ctx2.new_page()

            await page2.goto(final_url, wait_until="domcontentloaded", timeout=120000)
            try:
                await page2.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass

            try:
                await page2.add_style_tag(content=PRINT_CLEAN)
            except Exception:
                pass
            try:
                await page2.emulate_media(media="print")
            except Exception:
                pass

            try:
                await page2.pdf(
                    path=out_pdf_abs,
                    format="A4",
                    print_background=True,
                    margin={"top":"10mm","right":"10mm","bottom":"10mm","left":"10mm"},
                    page_ranges="1",
                )
            except Exception:
                _fallback_blank_pdf(out_pdf_abs, f"Guardia Civil – sin datos visibles para: {nombre}")

            await ctx2.close()
            await browser2.close()

        # ====== Validar PNG seleccionado y registrar en BD ======
        if not os.path.exists(selected_png_abs) or os.path.getsize(selected_png_abs) < 2000:
            _fallback_blank_png(selected_png_abs, f"Guardia Civil – evidencia: {mensaje_final} – {nombre}")

        selected_png_rel = os.path.join(rel_folder, os.path.basename(selected_png_abs))

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=selected_png_rel,   # <— GUARDAMOS PNG EN BD
        )

    except Exception as e:
        # Intento de PNG de respaldo en error
        try:
            if not os.path.exists(out_png_lista_abs):
                _fallback_blank_png(out_png_lista_abs, f"Guardia Civil – error: {e}")
        except Exception:
            pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje=str(e),
            archivo=os.path.join(rel_folder, os.path.basename(out_png_lista_abs)) if os.path.exists(out_png_lista_abs) else ""
        )
