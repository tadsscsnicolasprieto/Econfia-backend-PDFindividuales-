# core/bots/cgfm_mas_buscados.py
import os
import re
import unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "cgfm_mas_buscados"
URL = "https://www.cgfm.mil.co/es/taxonomy/term/4070"

# Selectores clave
SEL_COOKIES_BTN = "button.agree-button.eu-cookie-compliance-secondary-button.button.button--small"
SEL_SEARCH_INPUT = "#edit-keys"
SEL_RESULT_HINTS = [
    "div.view-content",
    "div.views-element-container",
    "ul.search-results",
    "article",
    "div.region-content",
    "main[role='main']",
]

# Tiempos
NAV_TIMEOUT_MS     = 120000
WAIT_AFTER_NAV_MS  = 2500
WAIT_COOKIES_MS    = 3000
WAIT_RESULTS_MS    = 15000


# ===================== Helpers (normalización y scoring) =====================

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

def _score_por_cantidad(n: int) -> int:
    if n <= 0:
        return 0
    if n == 1:
        return 2
    if 2 <= n <= 5:
        return 6
    return 10

def _norm_rel(path: str) -> str:
    return path.replace("\\", "/")


async def _contar_coincidencias_y_mensaje(page, query_text: str):
    """
    Devuelve (coincidencias_nombre_completo, mensaje, es_sin_resultados_bool)
    - Si aparece <em class="empty-search-results-text"> => (0, ese texto, True)
    - Si hay lista de resultados (<ol class="search-results"> li.search-results__item):
        * cuenta si el texto del ítem contiene el nombre completo normalizado
    """
    # 1) ¿Vacío explícito?
    try:
        empty_el = await page.query_selector("em.empty-search-results-text")
        if empty_el and await empty_el.is_visible():
            texto = (await empty_el.inner_text()).strip() or "Su búsqueda no produjo resultados"
            return 0, texto, True
    except Exception:
        pass

    # 2) Lista de resultados
    coincidencias = 0
    try:
        items = await page.query_selector_all("ol.search-results li.search-results__item")
        total_items = len(items)
        qn = _norm(query_text)

        if total_items == 0:
            # Fallback conservador
            return 0, "Su búsqueda no produjo resultados", True

        for li in items:
            try:
                txt = await li.inner_text()
                if qn and qn in _norm(txt):
                    coincidencias += 1
            except Exception:
                continue

        if coincidencias > 0:
            return coincidencias, f"Se encontraron {coincidencias} coincidencia(s) del nombre completo.", False
        else:
            return 0, "No se encontraron coincidencias exactas del nombre completo.", False

    except Exception:
        return 0, "Su búsqueda no produjo resultados", True


async def _guardar_resultado(consulta_id, fuente_obj, estado, mensaje, rel_path, score: int):
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=score,
        estado=estado,
        mensaje=mensaje,
        archivo=_norm_rel(rel_path) if rel_path else "",
    )


async def _relajar_layout_para_screenshot(page):
    """
    Evita cortes en capturas largas:
    - Oculta headers/footers/cookies flotantes
    - Pasa elementos position:fixed a estáticos
    - Quita overflow/alturas que recorten
    """
    css = """
      header, footer, nav, .cookie-banner, #onetrust-banner-sdk,
      .eu-cookie-compliance, .eu-cookie-compliance-banner,
      .toolbar, .sticky, .sticky-header, .stick-header,
      .boton-flotante, .floating, .float, .chatbot {
        display: none !important;
        visibility: hidden !important;
        height: 0 !important;
        overflow: hidden !important;
      }
      *[style*="position:fixed"] { position: static !important; }
      html, body { margin: 0 !important; padding: 0 !important; }
    """
    try:
        await page.add_style_tag(content=css)
    except Exception:
        pass

    # Relaja contenedores que suelen tener overflow
    try:
        await page.evaluate("""
            () => {
              const relaxNode = (node) => {
                if (!node) return;
                const s = getComputedStyle(node);
                if (['hidden','auto','scroll','clip'].includes(s.overflowY) || s.maxHeight !== 'none') {
                  node.style.overflow = 'visible';
                  node.style.overflowY = 'visible';
                  node.style.maxHeight = 'none';
                }
                if (s.transform !== 'none') node.style.transform = 'none';
                relaxNode(node.parentElement);
              };
              const mains = document.querySelectorAll('main, #content, .layout-content, .region-content, article');
              mains.forEach(el => {
                el.style.overflow = 'visible';
                el.style.maxHeight = 'none';
                el.style.height = 'auto';
                relaxNode(el.parentElement);
              });
              window.scrollTo(0,0);
            }
        """)
    except Exception:
        pass


# ===================== Bot principal =====================

async def consultar_cgfm_mas_buscados(
    consulta_id: int,
    nombre: str,
    apellido: str,
):
    """
    - Abre CGFM 'Más buscados'
    - Acepta cookies si aparece
    - Busca 'nombre apellido' en #edit-keys (Enter)
    - Espera resultados
    - Toma UN screenshot full_page (sin cortes) y lo guarda como PNG
    - Guarda en MEDIA_ROOT/resultados/<consulta_id>/... y registra en BD con score/mensaje
    """
    browser = None
    resultados_ok = False  # para mensaje fallback

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    try:
        # 2) Carpeta
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        query_text = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()
        safe_query = re.sub(r"\s+", "_", query_text) or "consulta"

        # Nombre del PNG final
        png_name = f"{NOMBRE_SITIO}_{safe_query}_{ts}.png"
        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,  # estable y rápido
                args=["--disable-dev-shm-usage"]
            )
            ctx = await browser.new_context(
                viewport={"width": 1366, "height": 1400},
                device_scale_factor=2,  # nitidez
                locale="es-CO"
            )
            page = await ctx.new_page()

            # 3) Navegar
            await page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(WAIT_AFTER_NAV_MS)

            # 4) Cookies
            try:
                await page.wait_for_selector(SEL_COOKIES_BTN, timeout=WAIT_COOKIES_MS)
                await page.locator(SEL_COOKIES_BTN).click()
                await page.wait_for_timeout(400)
            except Exception:
                pass

            # 5) Buscar
            await page.wait_for_selector(SEL_SEARCH_INPUT, timeout=15000)
            await page.fill(SEL_SEARCH_INPUT, query_text or "")
            await page.keyboard.press("Enter")

            # 6) Esperar contenedores de resultados visibles
            for sel in SEL_RESULT_HINTS:
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=WAIT_RESULTS_MS)
                    resultados_ok = True
                    break
                except Exception:
                    continue
            await page.wait_for_timeout(800)

            # 7) Conteo y mensaje
            coincidencias, mensaje_res, es_vacio = await _contar_coincidencias_y_mensaje(page, query_text)
            score_final = 0 if es_vacio else _score_por_cantidad(coincidencias)

            # 8) Relajar layout y screenshot full_page (sin cortes)
            await _relajar_layout_para_screenshot(page)
            try:
                await page.evaluate("window.scrollTo(0,0)")
            except Exception:
                pass

            await page.screenshot(path=abs_png, full_page=True)

            await ctx.close()
            await browser.close()
            browser = None

        # 9) Guardar resultado
        mensaje_final = mensaje_res or (
            "Resultados cargados y captura generada."
            if resultados_ok else "No se detectaron contenedores de resultados explícitos; se generó la captura de la página."
        )

        await _guardar_resultado(
            consulta_id=consulta_id,
            fuente_obj=fuente_obj,
            estado="Validada",
            mensaje=mensaje_final,
            rel_path=rel_png,
            score=score_final,
        )

    except Exception as e:
        # Evidencia en caso de error (si alcanza a estar abierto)
        try:
            if browser:
                # intentamos capturar la primera página abierta
                ctxs = await browser.contexts()
                if ctxs:
                    pages = ctxs[0].pages
                    if pages:
                        # ruta de emergencia
                        emer_name = f"{NOMBRE_SITIO}_ERROR_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                        emer_abs = os.path.join(absolute_folder, emer_name)
                        emer_rel = os.path.join(relative_folder, emer_name)
                        await pages[0].screenshot(path=emer_abs, full_page=True)
                        await _guardar_resultado(
                            consulta_id=consulta_id,
                            fuente_obj=fuente_obj,
                            estado="Sin validar",
                            mensaje=str(e),
                            rel_path=emer_rel,
                            score=0
                        )
                        return
        except Exception:
            pass
        finally:
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass

        # si no hubo screenshot de emergencia
        await _guardar_resultado(
            consulta_id=consulta_id,
            fuente_obj=fuente_obj,
            estado="Sin validar",
            mensaje=str(e),
            rel_path="",
            score=0
        )
