# core/bots/fac_busqueda_pdf.py
import os
import re
import unicodedata
from datetime import datetime
from urllib.parse import quote_plus

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "fac_busqueda_pdf"
BASE_URL = "https://www.fac.mil.co/"
SEARCH_PATH = "busqueda-global"  # /busqueda-global?keys=<q>

# Selectores
SEL_COOKIE_ACEPTAR = "button.agree-button.eu-cookie-compliance-secondary-button"
SEL_LUPA           = ".gva-search-region .icon, .gva-search-region"
SEL_INPUT_SEARCH   = "input#edit-keys"
SEL_RESULTS_HINTS  = [
    ".search-results", ".view-content", "#block-gavias-vitaco-content",
    "main .region-content", "article", ".node__content"
]
SEL_COUNTER        = ".counter-result"
SEL_ITEM           = ".container-results--item.item-result"  # cada resultado

# Tiempos
NAV_TIMEOUT_MS    = 120000
WAIT_AFTER_NAV_MS = 2000
WAIT_OPEN_SEARCH  = 6000
WAIT_RESULTS_MS   = 15000


def _rel(path: str) -> str:
    return path.replace("\\", "/") if path else ""


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")


def _norm(s: str) -> str:
    s = _strip_accents(s).lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


async def _guardar_resultado(consulta_id, fuente_obj, estado, mensaje, rel_path, score: int = 0):
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=score,
        estado=estado,
        mensaje=mensaje,
        archivo=_rel(rel_path),
    )


async def consultar_fac_busqueda_pdf(
    consulta_id: int,
    nombre: str,
    apellido: str,
):
    """
    FAC – Búsqueda global (con screenshot):
      - Abre visible, intenta lupa→input y fallback a /busqueda-global?keys=...
      - Coincidencia EXACTA del nombre completo (ignorando acentos/mayúsculas) ⇒ score=5
      - Si no hay coincidencia exacta ⇒ usa el texto del contador y score=0
      - Toma UN screenshot de página completa (sin recortes).
    """
    browser = None
    ctx = None
    page = None

    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await _guardar_resultado(
            consulta_id, None, "Sin Validar",
            f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", "", score=0
        )
        return

    # Carpeta resultados/<consulta_id>
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    q_raw = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()
    safe_q = re.sub(r"\s+", "_", q_raw) or "consulta"

    # Screenshot (único)
    png_name     = f"{NOMBRE_SITIO}_{safe_q}_{ts}.png"
    abs_png_path = os.path.join(absolute_folder, png_name)
    rel_png_path = os.path.join(relative_folder, png_name)

    # Normalización del nombre para coincidencia exacta
    name_norm = _norm(q_raw)
    patt_exact = re.compile(rf"(?<!\w){re.escape(name_norm)}(?!\w)")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,  # visible para que veas el proceso
                args=["--disable-dev-shm-usage"]
            )
            ctx = await browser.new_context(
                viewport={"width": 1366, "height": 900},
                device_scale_factor=1,
                locale="es-CO"
            )
            page = await ctx.new_page()

            # 1) Home
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(WAIT_AFTER_NAV_MS)

            # Cookies
            try:
                btn = page.locator(SEL_COOKIE_ACEPTAR)
                if await btn.count() > 0:
                    await btn.first.click(timeout=1500)
            except Exception:
                pass

            # 2) Intento normal: lupa → input
            used_fallback = False
            try:
                lup = page.locator(SEL_LUPA).first
                await lup.click(timeout=5000)
                await page.wait_for_selector(SEL_INPUT_SEARCH, timeout=WAIT_OPEN_SEARCH)
                await page.fill(SEL_INPUT_SEARCH, q_raw)
                await page.keyboard.press("Enter")
            except Exception:
                # 3) Fallback directo a la URL
                used_fallback = True
                search_url = f"{BASE_URL}{SEARCH_PATH}?keys={quote_plus(q_raw)}"
                await page.goto(search_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

            # Esperar resultados / contenedores
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            found_any_container = False
            for sel in SEL_RESULTS_HINTS + [SEL_ITEM, SEL_COUNTER]:
                try:
                    await page.wait_for_selector(sel, timeout=3000)
                    found_any_container = True
                    break
                except Exception:
                    continue

            # 4) Extraer items y buscar coincidencia EXACTA
            matches = 0
            items_text = []
            try:
                items_text = await page.evaluate(
                    """(sel) => Array.from(document.querySelectorAll(sel)).map(el => {
                          const title = el.querySelector('h3, .item-result--title, a')?.innerText || '';
                          const body  = el.querySelector('.item-result--body, p')?.innerText || '';
                          return (title + ' ' + body).trim();
                       })""",
                    SEL_ITEM
                )
            except Exception:
                items_text = []

            for t in items_text:
                if patt_exact.search(_norm(t or "")):
                    matches += 1

            # 5) Determinar mensaje y score
            if matches > 0:
                mensaje_final = f"Coincidencia exacta del nombre en {matches} resultado(s)."
                score_final = 5
            else:
                # Obtener texto del contador si existe
                counter_text = ""
                try:
                    counter = page.locator(SEL_COUNTER).first
                    if await counter.count() and await counter.is_visible():
                        counter_text = (await counter.inner_text() or "").strip()
                except Exception:
                    pass

                if counter_text:
                    mensaje_final = counter_text
                else:
                    # Si no hay contador, usar número de ítems detectados
                    mensaje_final = f"Se han encontrado {len(items_text)} contenidos"
                score_final = 0  # sin coincidencia exacta

            # 6) Screenshot de página completa (único)
            await page.screenshot(path=abs_png_path, full_page=True)

            # Cerrar
            await ctx.close()
            await browser.close()
            browser = ctx = page = None

        # Registrar resultado
        extra = " (Usó búsqueda por URL)" if used_fallback else ""
        await _guardar_resultado(
            consulta_id=consulta_id,
            fuente_obj=fuente_obj,
            estado="Validada",
            mensaje=mensaje_final + extra,
            rel_path=rel_png_path,
            score=score_final,
        )

    except Exception as e:
        # Screenshot de error si es posible (sin duplicar nombre)
        try:
            if page:
                await page.screenshot(path=abs_png_path, full_page=True)
        except Exception:
            pass
        finally:
            try:
                if ctx:
                    await ctx.close()
            except Exception:
                pass
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass

        await _guardar_resultado(
            consulta_id=consulta_id,
            fuente_obj=fuente_obj,
            estado="Sin Validar",
            mensaje=str(e),
            rel_path=rel_png_path if os.path.exists(abs_png_path) else "",
            score=0,
        )
