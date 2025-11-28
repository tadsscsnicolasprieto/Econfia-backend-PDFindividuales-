# core/bots/offshore.py
import os
import re
import unicodedata
import urllib.parse
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://offshoreleaks.icij.org/"
NOMBRE_SITIO = "offshore"
GOTO_TIMEOUT_MS = 120_000

# Tamaño carta objetivo en px (150 DPI): 8.5 x 11 in -> 1275 x 1650 px
LETTER_W = 1275
LETTER_H = 1650

def _norm(s: str) -> str:
    s = (s or "").strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s).strip()
    return s.casefold()

async def _goto_with_retries(page, url, attempts=3, base_delay=1.0, timeout=GOTO_TIMEOUT_MS):
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
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

async def _tabla_tiene_match_exacto(page, nombre_objetivo: str) -> bool:
    try:
        try:
            await page.wait_for_selector("#search_results", timeout=20000)
        except Exception:
            pass
        try:
            sp = page.locator("img[data-spinner]")
            if await sp.count() > 0:
                await sp.first.wait_for(state="hidden", timeout=15000)
        except Exception:
            pass
        sel_links = "#search_results table tbody tr td:first-child a"
        links = page.locator(sel_links)
        n = await links.count()
        objetivo = _norm(nombre_objetivo)
        for i in range(n):
            txt = (await links.nth(i).inner_text() or "").strip()
            if _norm(txt) == objetivo:
                return True
    except Exception:
        pass
    return False

async def _hide_overlays(page):
    try:
        await page.add_style_tag(content="""
          header, footer, .site-header, .site-footer, .cookie, .cookie-banner,
          .modal-backdrop, .overlay, .newsletter-popup, .floating-share,
          .social-share, .chat-widget, #chatbot, .consent, .consent-modal { display: none !important; visibility: hidden !important; opacity: 0 !important; }
          html, body { background: #ffffff !important; }
        """)
    except Exception:
        pass
    try:
        await page.evaluate("""
        () => {
          const sel = ['header', 'footer', '.site-header', '.site-footer', '.cookie', '.cookie-banner',
                       '.modal-backdrop', '.overlay', '.newsletter-popup', '.floating-share',
                       '.social-share', '.chat-widget', '#chatbot', '.consent', '.consent-modal'];
          sel.forEach(s => document.querySelectorAll(s).forEach(e => { try { e.style.display='none'; e.style.visibility='hidden'; e.style.opacity='0'; } catch(e){} }));
          document.documentElement.style.background = '#ffffff';
          document.body.style.background = '#ffffff';
        }
        """)
    except Exception:
        pass

async def _accept_terms_and_submit(page, checkbox_selectors=None, submit_selectors=None, timeout=3000):
    checkbox_selectors = checkbox_selectors or [
        'input#accept', 'input[name="accept"]', 'label[for="termsCheck"]',
        'label[for="accept"]', 'input[type="checkbox"]'
    ]
    submit_selectors = submit_selectors or [
        "button:has-text('Submit')", "button:has-text('SUBMIT')",
        "button:has-text('Accept & Continue')", "button:has-text('Accept')",
        "button:has-text('Aceptar')", "button:has-text('Continue')",
        "button:has-text('I Agree')"
    ]
    try:
        for sel in checkbox_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    try:
                        handle = await loc.element_handle()
                        tag = await page.evaluate("(el) => el.tagName.toLowerCase()", handle) if handle else None
                    except Exception:
                        tag = None
                    if tag == "label":
                        try:
                            await loc.click(timeout=1200)
                        except Exception:
                            pass
                    else:
                        try:
                            await loc.check(timeout=1200)
                        except Exception:
                            try:
                                await loc.click(timeout=1200)
                            except Exception:
                                pass
            except Exception:
                continue
        try:
            await page.wait_for_timeout(300)
        except Exception:
            pass
        for btn_sel in submit_selectors:
            try:
                btn = page.locator(btn_sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    try:
                        await btn.click(timeout=timeout)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            await page.wait_for_timeout(700)
                        return True
                    except Exception:
                        try:
                            await btn.focus()
                            await page.keyboard.press("Enter")
                            try:
                                await page.wait_for_load_state("networkidle", timeout=8000)
                            except Exception:
                                await page.wait_for_timeout(700)
                            return True
                        except Exception:
                            pass
            except Exception:
                continue
        try:
            modal_buttons = page.locator(".modal button, .dialog button, .cookie-banner button, .consent button, .cookie button")
            count = await modal_buttons.count()
            for i in range(count):
                try:
                    b = modal_buttons.nth(i)
                    if await b.is_visible():
                        try:
                            await b.click(timeout=2000)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=8000)
                            except Exception:
                                await page.wait_for_timeout(500)
                            return True
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            pass
    except Exception:
        pass
    return False

async def _save_screenshot_letter(page, folder, prefix):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_path = os.path.join(folder, f"{prefix}_{ts}.png")
    try:
        try:
            await page.evaluate("() => document.fonts.ready")
        except Exception:
            pass
        await asyncio.sleep(0.25)
        page_size = await page.evaluate("""() => ({w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight, vw: window.innerWidth, vh: window.innerHeight})""")
        total_w = int(page_size.get("w", 0) or 0)
        total_h = int(page_size.get("h", 0) or 0)
        viewport_w = int(page_size.get("vw", 0) or 0)
        viewport_h = int(page_size.get("vh", 0) or 0)
        box = None
        candidates = ["#search_results", "main, #main", "div.results, .results", "body"]
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
        if not box:
            box = {"x": 0, "y": 0, "width": viewport_w or total_w or LETTER_W, "height": viewport_h or total_h or LETTER_H}
        clip_w = min(LETTER_W, total_w if total_w > 0 else LETTER_W)
        clip_h = min(LETTER_H, total_h if total_h > 0 else LETTER_H)
        center_x = box["x"] + box["width"] / 2
        center_y = box["y"] + box["height"] / 2
        x = int(max(0, center_x - clip_w / 2))
        y = int(max(0, center_y - clip_h / 2))
        if total_w > 0 and x + clip_w > total_w:
            x = max(0, total_w - clip_w)
        if total_h > 0 and y + clip_h > total_h:
            y = max(0, total_h - clip_h)
        if clip_w >= 100 and clip_h >= 100:
            await page.screenshot(path=png_path, clip={"x": x, "y": y, "width": clip_w, "height": clip_h})
            return png_path
    except Exception:
        pass
    try:
        await page.screenshot(path=png_path, full_page=True)
        return png_path
    except Exception:
        try:
            await page.screenshot(path=png_path, full_page=False)
            return png_path
        except Exception:
            return ""

async def consultar_offshore(consulta_id: int, nombre: str, cedula, headless=True):
    if isinstance(headless, str):
        headless = headless.strip().lower() in ("1", "true", "yes", "y", "t")
    else:
        headless = bool(headless)
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)
    safe = re.sub(r"\s+", "_", (nombre or "consulta").strip()) or "consulta"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    intentos = 0
    max_intentos = 3
    rel_paths = []
    error_final = None
    while intentos < max_intentos:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
                context = await browser.new_context(
                    viewport={"width": LETTER_W, "height": LETTER_H},
                    device_scale_factor=1,
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9", "Referer": URL}
                )
                page = await context.new_page()
                try:
                    await _goto_with_retries(page, URL, attempts=2, base_delay=1.0, timeout=GOTO_TIMEOUT_MS)
                except Exception:
                    pass
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=60000)
                except Exception:
                    pass
                await _wait_for_networkidle_with_retries(page, retries=2, base_delay=0.5, timeout=15000)
                # intentar aceptar términos/modal en la home si aparece
                try:
                    await _accept_terms_and_submit(page)
                except Exception:
                    pass
                try:
                    await page.wait_for_selector('input[name="q"]', timeout=15000)
                    await page.fill('input[name="q"]', nombre or "")
                    await page.keyboard.press("Enter")
                except Exception:
                    try:
                        await page.evaluate("""(val) => { const i = document.querySelector('input[name="q"]'); if(i){ i.value = val; i.dispatchEvent(new Event('input')); } }""", nombre or "")
                        await page.keyboard.press("Enter")
                    except Exception:
                        pass
                try:
                    await _wait_for_networkidle_with_retries(page, retries=2, base_delay=0.5, timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(2000)
                # intentar aceptar términos/modal en la search results si aparece
                try:
                    await _accept_terms_and_submit(page)
                except Exception:
                    pass
                found_exact = False
                for i in range(1, 4):
                    if not found_exact:
                        try:
                            if await _tabla_tiene_match_exacto(page, nombre):
                                found_exact = True
                        except Exception:
                            pass
                    await _hide_overlays(page)
                    await asyncio.sleep(0.25)
                    png_name = f"{NOMBRE_SITIO}_{cedula}_{ts}_page{i}.png"
                    absolute_path = os.path.join(absolute_folder, png_name)
                    relative_path = os.path.join(relative_folder, png_name).replace("\\", "/")
                    shot = await _save_screenshot_letter(page, absolute_folder, f"{NOMBRE_SITIO}_{cedula}_{ts}_page{i}")
                    if shot:
                        try:
                            if shot != absolute_path:
                                os.replace(shot, absolute_path)
                        except Exception:
                            pass
                    else:
                        try:
                            await page.screenshot(path=absolute_path, full_page=True)
                        except Exception:
                            with open(absolute_path, "wb") as f:
                                f.write(b"\x89PNG\r\n\x1a\n")
                    rel_paths.append(relative_path)
                    next_button = page.locator('a.page-link[aria-label="Next »"], a[rel="next"]')
                    try:
                        if await next_button.count() and await next_button.is_enabled():
                            await next_button.first.click()
                            try:
                                await _wait_for_networkidle_with_retries(page, retries=2, base_delay=0.5, timeout=15000)
                            except Exception:
                                pass
                            await page.wait_for_timeout(2000)
                        else:
                            break
                    except Exception:
                        break
                try:
                    await browser.close()
                except Exception:
                    pass
            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
            score = 5 if found_exact else 1
            mensaje = "Coincidencia exacta encontrada" if score == 5 else "Sin coincidencia exacta en resultados"
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validado",
                mensaje=mensaje,
                archivo=",".join(rel_paths)
            )
            return
        except Exception as e:
            intentos += 1
            error_final = e
            error_png = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{cedula}_{ts}_error{intentos}.png")
            try:
                if 'page' in locals():
                    await _hide_overlays(page)
                    await page.screenshot(path=error_png, full_page=True)
                else:
                    with open(error_png, "wb") as f:
                        f.write(b"\x89PNG\r\n\x1a\n")
                rel_paths.append(os.path.join(relative_folder, os.path.basename(error_png)).replace("\\", "/"))
            except Exception:
                pass
            if intentos < max_intentos:
                await asyncio.sleep(1.0)
                continue
    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=0,
        estado="Sin validar",
        mensaje="Ocurrió un problema al obtener la información de la fuente",
        archivo=",".join(rel_paths)
    )
