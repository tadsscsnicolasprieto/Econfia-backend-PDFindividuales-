# bots/insightcrime_search_pdf.py
import os
import re
import unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://insightcrime.org/es/investigaciones/"
NOMBRE_SITIO = "insightcrime_search_pdf"

# ---------- Timeouts (subidos) ----------
TIMEOUT_NAV = 180_000            # navegaciones (goto)
TIMEOUT_NETWORKIDLE = 35_000     # espera de red ociosa
TIMEOUT_SELECTOR = 25_000        # waits de selectores comunes
TIMEOUT_RESULTS = 30_000         # espera a resultados/empty
EXTRA_PAUSE_MS = 1_200           # pausa corta adicional
# ---------------------------------------

def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

def _norm_rel(path: str) -> str:
    return (path or "").replace("\\", "/")

def _normalize(txt: str) -> str:
    txt = (txt or "").strip().lower()
    txt = " ".join(txt.split())
    # quita acentos
    txt = unicodedata.normalize("NFKD", txt)
    txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
    return txt

# === utilidades ===
async def _pick_visible_input(page):
    # 1) dentro del modal
    for scope in ["div[role='dialog']", ".ucs-search-modal", ".search-modal"]:
        try:
            loc = page.locator(
                f"{scope} input.main-input, {scope} input[placeholder='Search here'], {scope} input[type='search']"
            )
            await loc.wait_for(state="visible", timeout=TIMEOUT_SELECTOR)
            return loc.first
        except Exception:
            pass
    # 2) global
    candidates = page.locator("input.main-input, input[placeholder='Search here'], input[type='search']")
    try:
        n = await candidates.count()
    except Exception:
        n = 0
    for i in range(n):
        el = candidates.nth(i)
        try:
            if await el.is_visible() and await el.is_enabled():
                return el
        except Exception:
            continue
    return None

async def consultar_insightcrime_search_pdf(consulta_id: int, nombre: str):
    """
    Busca `nombre` en InsightCrime, toma screenshot del modal de resultados
    y crea un Resultado **por cada li.item.item-doc.item-website-doc**:
      - Si el título/snippet del li contiene el nombre → score=5.
      - Si no contiene → score=1.
    Si no hay resultados → un solo Resultado con score=0.
    """
    navegador = None
    ctx = None

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

    # Rutas de salida -> resultados/<consulta_id>/
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = _safe_name(nombre)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_png_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.png")
    out_png_rel = _norm_rel(os.path.join(relative_folder, os.path.basename(out_png_abs)))

    BTN_SEARCH = "#vertex-search"
    RESULTS_HINTS = [
        "ol.results-list li.item",
        "ol.results-list",
        "div.results-count",
        "div[role='dialog'] .results-list",
        "div.empty",
    ]

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            ctx = await navegador.new_context(
                viewport={"width": 1440, "height": 900},
                device_scale_factor=2,
                locale="es-ES",
            )
            page = await ctx.new_page()
            # timeouts globales
            page.set_default_timeout(TIMEOUT_SELECTOR)
            page.set_default_navigation_timeout(TIMEOUT_NAV)

            await page.goto(URL, wait_until="domcontentloaded", timeout=TIMEOUT_NAV)
            try:
                await page.wait_for_load_state("networkidle", timeout=TIMEOUT_NETWORKIDLE)
            except Exception:
                pass

            # Cookies / consent
            for sel in [
                "button.cmplz-btn.cmplz-accept",
                ".cmplz-accept",
                "button:has-text('Accept')",
                "button:has-text('Aceptar')",
                "#cky-btn-accept",
                "#cookie_action_close_header",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        await el.click(timeout=1500)
                        break
                except Exception:
                    pass

            # Abrir buscador (con tolerancia)
            opened = False
            for sel in [BTN_SEARCH, "button[aria-label*='Buscar' i]", "button[aria-label*='Search' i]"]:
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=TIMEOUT_SELECTOR)
                    await page.click(sel)
                    opened = True
                    break
                except Exception:
                    continue
            if not opened:
                # último intento: tecla "/" (algunos sitios abren search)
                try:
                    await page.keyboard.press("/")
                except Exception:
                    pass

            # Input visible
            inp = await _pick_visible_input(page)
            if not inp:
                await page.mouse.wheel(0, 600)
                await page.wait_for_timeout(400)
                inp = await _pick_visible_input(page)
            if not inp:
                raise RuntimeError("No pude encontrar el input visible del buscador.")

            # Escribir término (robusto)
            wrote = False
            for mode in ("fill", "type", "js", "kbd"):
                try:
                    if mode == "fill":
                        await inp.fill("")
                        await inp.fill(nombre)
                    elif mode == "type":
                        await inp.click(force=True)
                        await inp.type(nombre, delay=30)
                    elif mode == "js":
                        await page.evaluate(
                            """(value) => {
                                const sel = "div[role='dialog'] input.main-input, input.main-input, input[placeholder='Search here'], input[type='search']";
                                const el = document.querySelector(sel);
                                if (!el) return;
                                el.focus();
                                el.value = value;
                                el.dispatchEvent(new Event('input', {bubbles:true}));
                                el.dispatchEvent(new Event('change', {bubbles:true}));
                            }""",
                            nombre
                        )
                    else:
                        await page.evaluate(
                            """() => {
                                const el = document.querySelector("div[role='dialog'] input.main-input, input.main-input, input[placeholder='Search here'], input[type='search']");
                                el?.focus();
                            }"""
                        )
                        await page.keyboard.insert_text(nombre)

                    try:
                        val = await inp.input_value(timeout=1200)
                    except Exception:
                        val = ""
                    if val:
                        wrote = True
                        break
                except Exception:
                    continue

            if not wrote:
                raise RuntimeError("El texto no quedó en el campo de búsqueda.")

            # Enviar búsqueda
            try:
                await inp.press("Enter")
            except Exception:
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass

            # Esperar resultados (más tiempo)
            found_any = False
            for sel in RESULTS_HINTS:
                try:
                    await page.wait_for_selector(sel, timeout=TIMEOUT_RESULTS)
                    found_any = True
                    break
                except Exception:
                    continue

            # Si aún no “canta” ningún selector, espera por función: lista/contador/empty
            if not found_any:
                try:
                    await page.wait_for_function(
                        """
                        () => {
                          const scope = document.querySelector('div[role="dialog"]') || document;
                          return !!(
                            scope.querySelector('ol.results-list li.item') ||
                            scope.querySelector('div.results-count') ||
                            scope.querySelector('div.empty')
                          );
                        }
                        """,
                        timeout=TIMEOUT_RESULTS
                    )
                except Exception:
                    pass

            try:
                await page.wait_for_load_state("networkidle", timeout=TIMEOUT_NETWORKIDLE)
            except Exception:
                pass
            await page.wait_for_timeout(EXTRA_PAUSE_MS)

            # ======= CAPTURA: sólo el MODAL del buscador =======
            modal = None
            for sel in ["div[role='dialog']", ".ucs-search-modal", ".search-modal"]:
                try:
                    cand = page.locator(sel).first
                    if await cand.count() > 0 and await cand.is_visible():
                        modal = cand
                        break
                except Exception:
                    continue

            if modal:
                try:
                    await modal.scroll_into_view_if_needed(timeout=1000)
                except Exception:
                    pass
                await modal.screenshot(path=out_png_abs)
            else:
                await page.screenshot(path=out_png_abs)

            # ======= PROCESAR CADA <li> INDIVIDUALMENTE =======
            scope = modal if modal else page
            items = scope.locator("ol.results-list li.item.item-doc.item-website-doc")
            try:
                total = await items.count()
            except Exception:
                total = 0

            normalized_query = _normalize(nombre)

            if total == 0:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj, score=0,
                    estado="Validada",
                    mensaje="No results found, try a different search.",
                    archivo=out_png_rel
                )
            else:
                for i in range(total):
                    li = items.nth(i)
                    try:
                        title = (await li.locator(".header a").first.inner_text()).strip()
                    except Exception:
                        try:
                            title = (await li.locator("a").first.inner_text()).strip()
                        except Exception:
                            title = ""

                    try:
                        href = await li.locator(".header a, .link a, a").first.get_attribute("href")
                    except Exception:
                        href = ""

                    try:
                        snippet = (await li.locator("p.collapsed").first.inner_text()).strip()
                    except Exception:
                        snippet = ""

                    texto_li_norm = _normalize(f"{title} {snippet}")
                    coincide = normalized_query in texto_li_norm if normalized_query else False

                    score = 5 if coincide else 1
                    if coincide:
                        mensaje = f"Coincidencia con '{nombre}' en el ítem: {title} — {href or 'sin URL'}"
                    else:
                        mensaje = f"Ítem sin coincidencia explícita con '{nombre}'"

                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id, fuente=fuente_obj, score=score,
                        estado="Validada", mensaje=mensaje, archivo=out_png_rel
                    )

            await ctx.close()
            await navegador.close()
            navegador = None
            ctx = None

    except Exception as e:
        # Evidencia de error
        err_rel = ""
        try:
            if ctx is not None:
                pages = ctx.pages
                if pages:
                    err_abs = out_png_abs.replace(".png", "_ERROR.png")
                    await pages[0].screenshot(path=err_abs)
                    err_rel = _norm_rel(os.path.join(relative_folder, os.path.basename(err_abs)))
        except Exception:
            pass
        finally:
            try:
                if ctx is not None:
                    await ctx.close()
            except Exception:
                pass
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje=str(e), archivo=err_rel
        )
