# bots/mofa_bh_cte.py
import os
import re
import asyncio
import urllib.parse
import unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "mofa_bh_cte"
URL_SEARCH = "https://www.mofa.gov.bh/en/search?keyword={q}"
GOTO_TIMEOUT_MS = 180_000

# Selectores
SEL_COOKIE_BTN = "button.transparent-bg-btn.with-arrow.cookie-btn[data-cookie-string='CookieNotificationAccepted']"
SEL_ZERO_P = "p:has-text('Your search')"
SEL_RESULTS_WRAP = "div.search-results-wrapper"

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def _goto_with_retries(page, url, attempts=3, base_delay=1.0, timeout=GOTO_TIMEOUT_MS):
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            resp = await page.goto(url, timeout=timeout)
            return resp
        except Exception as e:
            last_exc = e
            if i < attempts:
                await asyncio.sleep(base_delay * (2 ** (i - 1)))
    raise last_exc

async def _wait_for_networkidle_with_retries(page, retries=3, base_delay=1.0, timeout=30000):
    for attempt in range(1, retries + 1):
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
            return True
        except Exception:
            if attempt < retries:
                await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
    return False

async def _clean_page_css(page):
    css = """
      header, footer, .footer, .header, #footer, #header,
      .cookie, .cookie-btn, .cookie-notification, .cookie-wrapper,
      .site-footer, .site-header, .virtual-assistant, .chat-widget, #chatbot,
      .newsletter-popup, .hero, .page-hero, .backdrop, .modal-backdrop, .overlay,
      .social-share, .share-toolbar, .floating-share, .sticky-share {
        display: none !important; visibility: hidden !important; opacity: 0 !important;
      }
      html, body { background: #ffffff !important; }
      body { margin: 0 !important; padding: 0 !important; }
      main, .container, .content, .search-results-section {
        margin: 0 auto !important; padding: 10px !important; max-width: 1100px !important;
      }
    """
    try:
        await page.add_style_tag(content=css)
    except Exception:
        pass

async def _hide_overlays(page):
    try:
        await page.evaluate("""
        () => {
          const selectors = [
            'header', '.site-header', '.site-top', '.hero', '.page-hero',
            'footer', '.site-footer', '.cookie-banner', '.cookie-notification',
            '.newsletter-popup', '.virtual-assistant', '.chat-widget', '#chatbot',
            '.backdrop', '.modal-backdrop', '.overlay', '.consent-modal', '.popup',
            '.social-share', '.share-toolbar', '.floating-share', '.sticky-share'
          ];
          selectors.forEach(sel => {
            document.querySelectorAll(sel).forEach(e => {
              try { e.style.display = 'none'; e.style.visibility = 'hidden'; e.style.opacity = '0'; } catch(e) {}
            });
          });
          document.documentElement.style.background = '#ffffff';
          document.body.style.background = '#ffffff';
        }
        """)
    except Exception:
        pass

async def _save_screenshot_letter(page, folder, prefix):
    """
    Intenta guardar una captura con tamaño carta (8.5x11 in) a 150 DPI -> 1275x1650 px.
    - Busca main o contenedor de resultados y centra el recorte en él.
    - Si no puede recortar, hace full_page como fallback.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_path = os.path.join(folder, f"{prefix}_{ts}.png")

    # Tamaño objetivo en píxeles (150 DPI)
    target_w = 1275
    target_h = 1650

    try:
        # Obtener dimensiones totales de la página (CSS pixels)
        page_size = await page.evaluate("""() => ({w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight, vw: window.innerWidth, vh: window.innerHeight})""")
        total_w = int(page_size.get("w", 0))
        total_h = int(page_size.get("h", 0))
        viewport_w = int(page_size.get("vw", 0))
        viewport_h = int(page_size.get("vh", 0))

        # Preferir main o contenedor de resultados para centrar el recorte
        box = None
        candidates = ["main, #main", "div.search-results-wrapper", ".search-results-section", ".container", ".content", "body"]
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    b = await loc.bounding_box()
                    if b and b.get("width") and b.get("height"):
                        box = b
                        break
            except Exception:
                continue

        # Si no hay bounding box válido, usar el viewport como referencia
        if not box:
            box = {"x": 0, "y": 0, "width": viewport_w or total_w, "height": viewport_h or total_h}

        # Convertir target (px) a CSS pixels para clip.
        # Playwright usa CSS pixels; asumimos device_scale_factor=1 en el contexto.
        clip_w = min(target_w, total_w)
        clip_h = min(target_h, total_h)

        # Centrar el clip en el contenedor (box)
        center_x = box["x"] + box["width"] / 2
        center_y = box["y"] + box["height"] / 2

        x = int(max(0, center_x - clip_w / 2))
        y = int(max(0, center_y - clip_h / 2))

        # Ajustar para no salirse de la página
        if x + clip_w > total_w:
            x = max(0, total_w - clip_w)
        if y + clip_h > total_h:
            y = max(0, total_h - clip_h)

        # Si el clip es demasiado pequeño (por ejemplo total_w < 200), fallback a full_page
        if clip_w < 100 or clip_h < 100:
            raise Exception("Clip demasiado pequeño, fallback a full_page")

        # Tomar screenshot con clip
        await page.screenshot(path=png_path, clip={"x": x, "y": y, "width": clip_w, "height": clip_h})
        return png_path

    except Exception:
        # Fallback: intentar full_page
        try:
            await page.screenshot(path=png_path, full_page=True)
            return png_path
        except Exception:
            try:
                await page.screenshot(path=png_path, full_page=False)
                return png_path
            except Exception:
                return ""

async def consultar_mofa_bh_cte(consulta_id: int, nombre: str, apellido: str, headless=True):
    """
    Captura en tamaño carta:
    - Contexto con device_scale_factor=1 para que los píxeles del clip correspondan al tamaño objetivo.
    - Oculta overlays y centra el recorte en el main o contenedor de resultados.
    - Guarda solo la captura y persiste Resultado.
    """
    # Normalizar headless
    if isinstance(headless, str):
        headless = headless.strip().lower() in ("1", "true", "yes", "y", "t")
    else:
        headless = bool(headless)

    navegador = None
    full_name = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=1,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    if not full_name:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=1,
            estado="Sin Validar",
            mensaje="Nombre y/o apellido vacíos para la consulta.",
            archivo=""
        )
        return

    # Carpeta / archivo
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^\w\.-]+", "_", full_name)
    png_name = f"{NOMBRE_SITIO}_{safe_name}_{ts}.png"
    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    mensaje_final = "No hay coincidencias."
    success = False
    last_error = None
    score_final = 1

    norm_query = _norm(full_name)
    exact_re = re.compile(rf"(?<!\w){re.escape(norm_query)}(?!\w)")

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
            # device_scale_factor=1 para que clip px ≈ imagen px
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                device_scale_factor=1,
                locale="en-US",
                timezone_id="Asia/Bahrain",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.mofa.gov.bh/en/"}
            )
            page = await context.new_page()

            # 1) Visitar la home para obtener cookies/estado
            home_url = "https://www.mofa.gov.bh/en/"
            try:
                await _goto_with_retries(page, home_url, attempts=2, base_delay=1.0, timeout=60000)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
            except Exception:
                pass
            await _wait_for_networkidle_with_retries(page, retries=2, base_delay=0.5, timeout=15000)
            try:
                await page.evaluate("() => document.fonts.ready")
            except Exception:
                pass

            # 2) Realizar la búsqueda en el mismo contexto (mismas cookies)
            q = urllib.parse.quote(full_name)
            search_url = URL_SEARCH.format(q=q)
            resp = None
            try:
                resp = await _goto_with_retries(page, search_url, attempts=3, base_delay=1.0, timeout=GOTO_TIMEOUT_MS)
            except Exception as e:
                last_error = f"Error navegando a la URL de búsqueda: {e}"

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
            except Exception:
                pass
            await _wait_for_networkidle_with_retries(page, retries=2, base_delay=0.5, timeout=15000)

            # 3) Aceptar cookies si aparece (best-effort)
            try:
                btn = page.locator(SEL_COOKIE_BTN)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click(timeout=5_000)
                    await _wait_for_networkidle_with_retries(page, retries=1, base_delay=0.5, timeout=8000)
            except Exception:
                pass

            # 4) Ocultar overlays y elementos molestos antes de la captura
            await _clean_page_css(page)
            await _hide_overlays(page)
            await asyncio.sleep(0.25)

            # 5) Detectar bloqueo por status o por contenido
            blocked = False
            resp_status = None
            if resp:
                try:
                    resp_status = resp.status
                except Exception:
                    resp_status = None
            if resp_status == 403:
                blocked = True
            else:
                try:
                    content_lower = (await page.content()).lower()
                    indicators = ["access denied", "forbidden", "error 403", "cloudfront", "cloudflare", "request blocked", "web page blocked", "attack id"]
                    if any(ind in content_lower for ind in indicators):
                        blocked = True
                except Exception:
                    pass

            # 6) Si bloqueado en headless, hacer evidencia headful (solo screenshot)
            if headless and blocked:
                try:
                    try:
                        await context.close()
                    except Exception:
                        pass
                    try:
                        await navegador.close()
                    except Exception:
                        pass

                    navegador = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
                    context = await navegador.new_context(viewport={"width": 1400, "height": 900}, device_scale_factor=1, locale="en-US", timezone_id="Asia/Bahrain")
                    page = await context.new_page()
                    try:
                        await _goto_with_retries(page, search_url, attempts=2, base_delay=1.0, timeout=GOTO_TIMEOUT_MS)
                    except Exception:
                        pass
                    await _wait_for_networkidle_with_retries(page, retries=2, base_delay=0.5, timeout=15000)

                    try:
                        await _clean_page_css(page)
                        await _hide_overlays(page)
                    except Exception:
                        pass
                    evidencia_png = await _save_screenshot_letter(page, absolute_folder, "mofa_bh_evidence_headful")

                    mensaje_final = "Bloqueo detectado (headless)."
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id, fuente=fuente_obj,
                        score=1, estado="Sin Validar",
                        mensaje=mensaje_final,
                        archivo=os.path.join(relative_folder, os.path.basename(evidencia_png)).replace("\\", "/") if evidencia_png else ""
                    )
                    try:
                        await navegador.close()
                    except Exception:
                        pass
                    return
                except Exception:
                    pass

            # 7) Analizar resultados y capturar (tamaño carta)
            zero_p = page.locator(SEL_ZERO_P)
            zero_text = ""
            if await zero_p.count() > 0:
                try:
                    zero_text = (await zero_p.first.inner_text()).strip()
                except Exception:
                    zero_text = ""

            if zero_text and re.search(r"found\s*0\s*results", zero_text, re.I):
                mensaje_final = zero_text
                shot = await _save_screenshot_letter(page, absolute_folder, "mofa_bh_result")
                success = True if shot else False
            else:
                wrappers = page.locator(SEL_RESULTS_WRAP)
                try:
                    await wrappers.first.wait_for(state="visible", timeout=10_000)
                except Exception:
                    pass
                n = await wrappers.count()
                exact_hit = False
                for i in range(n):
                    item = wrappers.nth(i)
                    try:
                        blob = await item.inner_text(timeout=4_000)
                    except Exception:
                        blob = ""
                    norm_blob = _norm(blob)
                    if norm_blob and exact_re.search(norm_blob):
                        exact_hit = True
                        break
                if exact_hit:
                    score_final = 5
                    mensaje_final = f"Coincidencia exacta encontrada en MOFA Bahrain para: '{full_name}'."
                else:
                    score_final = 1
                    mensaje_final = "Se encontraron resultados, pero no hubo coincidencia exacta del nombre."
                shot = await _save_screenshot_letter(page, absolute_folder, "mofa_bh_result")
                success = True if shot else False

            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 8) Persistencia
        if success:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=score_final, estado="Validada",
                mensaje=mensaje_final, archivo=relative_png
            )
        else:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1, estado="Sin Validar",
                mensaje=last_error or "No fue posible obtener resultados.", archivo=relative_png
            )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1, estado="Sin Validar",
                mensaje=str(e), archivo=""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
