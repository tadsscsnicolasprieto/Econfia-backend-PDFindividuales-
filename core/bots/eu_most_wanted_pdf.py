# bots/eu_most_wanted_pdf.py
import os
import re
import unicodedata
import asyncio
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://eumostwanted.eu/es/"
NOMBRE_SITIO = "eu_most_wanted_pdf"

def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

def _normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    s = s.replace("\xa0", " ")
    return s.strip()

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

async def _screenshot_to_pdf(page, out_pdf_abs):
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    tmp_png = out_pdf_abs.replace(".pdf", ".png")
    os.makedirs(os.path.dirname(out_pdf_abs), exist_ok=True)
    await page.screenshot(path=tmp_png, full_page=True)
    img = ImageReader(tmp_png)
    iw, ih = img.getSize()
    pdf = canvas.Canvas(out_pdf_abs, pagesize=(iw, ih))
    pdf.drawImage(img, 0, 0, width=iw, height=ih)
    pdf.save()
    try:
        os.remove(tmp_png)
    except Exception:
        pass

PRINT_FIX = """
@media print {
  header, .site-header, .header, #header, .eu-logo, .enfast-logo, .navbar,
  .global-header, .page-header, .layout-header, .masthead { display:block !important; visibility:visible !important; height:auto !important; overflow:visible !important; opacity:1 !important; }
  .no-print { display:block !important; }
}
"""

SEARCH_INPUTS = [
    "input[placeholder*='Buscar' i]",
    "input[placeholder*='Search' i]",
    "input[type='search']",
    "input[name='search']",
    "input.search__input",
    "input.form-control",
]

# Selectores propios del listado EU Most Wanted
SEL_LIST     = ".wanted_list"
SEL_ITEM     = ".wanted_list .wantedItem"
SEL_NAME_1   = ".title .content"   # suele tener "APELLIDO, Nombre"
SEL_NAME_2   = ".micro-title"      # alternativa en teaser

async def consultar_eu_most_wanted_pdf(consulta_id: int, nombre: str):
    """
    - Hace búsqueda por `nombre`.
    - Match EXACTO (normalizado) contra nombres en tarjetas -> score=10, "Se han encontrado hallazgos", guarda PNG de DETALLE.
    - Si no hay match -> score=0, "Your search yielded no results.", guarda PNG de LISTA.
    - Genera PDF para auditoría, pero NO se guarda en BD.
    """
    navegador = None
    ctx = None
    page = None
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
    rel_folder = os.path.join("resultados", str(consulta_id))
    abs_folder = os.path.join(settings.MEDIA_ROOT, rel_folder)
    os.makedirs(abs_folder, exist_ok=True)

    safe = _safe_name(nombre)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_pdf_abs   = os.path.join(abs_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.pdf")
    out_png_list  = os.path.join(abs_folder, f"{NOMBRE_SITIO}_{safe}_{ts}_lista.png")
    out_png_det   = os.path.join(abs_folder, f"{NOMBRE_SITIO}_{safe}_{ts}_detalle.png")
    selected_png  = out_png_list  # por defecto guardamos el de lista

    score_final   = 0
    mensaje_final = "Your search yielded no results."
    final_url     = URL

    try:
        # ======= FASE 1: Navegación visible =======
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = await navegador.new_context(
                viewport={"width": 1440, "height": 1000},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
                locale="es-ES",
            )
            page = await ctx.new_page()
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # Cookies (I agree)
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.3)
                # El banner cambia; intentamos textos genéricos
                for t in ["I agree", "Acepto", "Estoy de acuerdo", "Accept"]:
                    btns = page.get_by_text(t, exact=False)
                    if await btns.count():
                        await btns.first.click(timeout=2000)
                        break
            except Exception:
                pass

            # Buscar campo
            inp = None
            for sel in SEARCH_INPUTS:
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=2500)
                    cand = page.locator(sel).first
                    if await cand.is_visible():
                        inp = cand
                        break
                except Exception:
                    continue
            if not inp:
                # botón lupa
                try:
                    await page.get_by_role("button").filter(has_text=re.compile("search|buscar", re.I)).first.click(timeout=2000)
                    await page.wait_for_selector("input[type='search']", state="visible", timeout=2500)
                    inp = page.locator("input[type='search']").first
                except Exception:
                    pass
            if not inp:
                raise RuntimeError("No se encontró el campo de búsqueda en EU Most Wanted.")

            await inp.click(force=True)
            try: await inp.fill("")
            except Exception: pass
            await inp.type(nombre, delay=25)

            try:
                async with page.expect_navigation(timeout=15000):
                    await inp.press("Enter")
            except Exception:
                await asyncio.sleep(2.5)

            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(0.8)

            # Screenshot temprano (lista)
            try:
                await page.screenshot(path=out_png_list, full_page=True)
            except Exception:
                pass

            # Esperar que aparezca el contenedor/lista o algún item
            try:
                await page.wait_for_function(
                    """() => !!(document.querySelector('.wanted_list') || document.querySelector('.wanted_list .wantedItem'))""",
                    timeout=20000
                )
            except Exception:
                # si nada aparece, tratamos como sin resultados
                final_url = page.url
            else:
                # Buscar match exacto por nombre en tarjetas
                buscado = _normalize(nombre)
                match_idx = -1

                # Intento 1: leer todos los items visibles
                items = page.locator(SEL_ITEM)
                total = 0
                try:
                    total = await items.count()
                except Exception:
                    total = 0

                for i in range(total):
                    itm = items.nth(i)
                    try:
                        # texto preferente en .title .content
                        txt = ""
                        if await itm.locator(SEL_NAME_1).count():
                            txt = (await itm.locator(SEL_NAME_1).first.inner_text()).strip()
                        elif await itm.locator(SEL_NAME_2).count():
                            txt = (await itm.locator(SEL_NAME_2).first.inner_text()).strip()
                        if _normalize(txt) == buscado:
                            match_idx = i
                            break
                    except Exception:
                        continue

                if match_idx >= 0:
                    score_final   = 10
                    mensaje_final = "Se han encontrado hallazgos"
                    # Click al detalle: en este sitio el href está en el DIV .wantedItem
                    try:
                        with page.expect_navigation(timeout=20000):
                            await items.nth(match_idx).click()
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass
                    except Exception:
                        pass

                    final_url = page.url
                    # Screenshot detalle
                    try:
                        await page.screenshot(path=out_png_det, full_page=True)
                        selected_png = out_png_det
                    except Exception:
                        selected_png = out_png_list
                else:
                    # sin match exacto
                    final_url = page.url
                    score_final   = 0
                    mensaje_final = "Your search yielded no results."
                    selected_png  = out_png_list

            # cerrar visible
            try: await ctx.close()
            except Exception: pass
            try: await navegador.close()
            except Exception: pass
            navegador, ctx, page = None, None, None

        # ======= FASE 2: PDF para auditoría (no se guarda en BD) =======
        async with async_playwright() as p:
            browser2 = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            ctx2 = await browser2.new_context(viewport={"width": 1440, "height": 1000}, locale="es-ES")
            page2 = await ctx2.new_page()
            await page2.goto(final_url, wait_until="domcontentloaded", timeout=120000)
            try:
                await page2.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass

            ok = True
            try:
                await page2.add_style_tag(content=PRINT_FIX)
                await page2.emulate_media(media="print")
                await page2.pdf(
                    path=out_pdf_abs,
                    format="A4",
                    print_background=True,
                    margin={"top":"10mm","right":"10mm","bottom":"10mm","left":"10mm"},
                    page_ranges="1",
                )
            except Exception:
                ok = False
            if (not ok) or (not os.path.exists(out_pdf_abs)) or (os.path.getsize(out_pdf_abs) < 500):
                await _screenshot_to_pdf(page2, out_pdf_abs)

            await ctx2.close()
            await browser2.close()

        # ======= Validar PNG y registrar en BD =======
        if not os.path.exists(selected_png) or os.path.getsize(selected_png) < 2000:
            _fallback_blank_png(selected_png, f"EU Most Wanted – evidencia: {mensaje_final} – {nombre}")

        archivo_rel = os.path.join(rel_folder, os.path.basename(selected_png))
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=archivo_rel,   # <-- GUARDAMOS PNG EN BD
        )

    except Exception as e:
        # PNG de respaldo y registro de error
        try:
            if not os.path.exists(out_png_list):
                _fallback_blank_png(out_png_list, f"EU Most Wanted – error: {e}")
        except Exception:
            pass
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje=str(e),
            archivo=os.path.join(rel_folder, os.path.basename(out_png_list)) if os.path.exists(out_png_list) else ""
        )
