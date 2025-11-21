# core/bots/cne_magistrados_busqueda_pdf.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from core.models import Resultado, Fuente

NOMBRE_SITIO = "cne_magistrados_busqueda_pdf"
URL = "https://www.cne.gov.co/la-entidad/magistrados"

# Selectores robustos (algunos sitios Joomla cambian IDs)
SEL_INPUTS = [
    "input#mod-search-searchword92:visible",
    "input[placeholder*='Buscar']:visible",
    "form input[type='search']:visible",
]
SEL_ENGAGEBOX_CLOSE = "[data-ebox-cmd='close']"

NAV_TIMEOUT_MS = 180_000
WAIT_POST_MS   = 3_000
RETRIES        = 3


async def _goto_with_retries(page, url: str) -> None:
    """ Navegar con reintentos y backoff. """
    last_err = None
    for i in range(RETRIES):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            # Intento de estabilizar red
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            return
        except Exception as e:
            last_err = e
            # backoff: 1s, 3s, 6s
            await asyncio.sleep(1 * (2 ** i) if i else 1)
    raise last_err if last_err else RuntimeError("Fallo de navegación desconocido")


async def _guardar_resultado(consulta_id, fuente_obj, estado, mensaje, rel_path, score: int = 0):
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=score,
        estado=estado,
        mensaje=mensaje,
        archivo=rel_path,
    )


async def consultar_cne_magistrados_busqueda_pdf(
    consulta_id: int,
    nombre: str,
    apellido: str,
):
    browser = None

    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await _guardar_resultado(
            consulta_id, None, "Sin Validar",
            f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", "", 0
        )
        return

    try:
        # Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        q = " ".join([(nombre or "").strip(), (apellido or "").strip()]).strip() or "consulta"
        safe_q = re.sub(r"\s+", "_", q)

        # Nombres de archivo PNG
        png_name = f"{NOMBRE_SITIO}_{safe_q}_{ts}.png"
        abs_png  = os.path.join(absolute_folder, png_name)
        rel_png  = os.path.join(relative_folder, png_name)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )
            ctx = await browser.new_context(
                viewport={"width": 1366, "height": 900},
                locale="es-CO",
                ignore_https_errors=True,
            )

            page = await ctx.new_page()
            await page.route("**/*", lambda route: route.continue_())
            await _goto_with_retries(page, URL)

            # Cerrar cookies / popups si aparecen
            try:
                cookies_btn = page.locator("button:has-text('Aceptar'), .eu-cookie-compliance-default-button")
                if await cookies_btn.first.is_visible():
                    await cookies_btn.first.click()
            except Exception:
                pass
            try:
                closes = page.locator(SEL_ENGAGEBOX_CLOSE)
                for i in range(await closes.count()):
                    btn = closes.nth(i)
                    if await btn.is_visible():
                        try:
                            await btn.click(timeout=1000)
                        except Exception:
                            pass
            except Exception:
                pass

            # Buscar input visible
            input_loc = None
            last_err = None
            for sel in SEL_INPUTS:
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=8000)
                    input_loc = page.locator(sel).first
                    break
                except Exception as e:
                    last_err = e
                    continue
            if not input_loc:
                raise RuntimeError(
                    f"No se encontró un campo de búsqueda visible. Último error: {last_err}"
                )

            await input_loc.click()
            try:
                await input_loc.fill("")
            except Exception:
                pass
            await input_loc.type(q, delay=25)
            await input_loc.press("Enter")
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            await asyncio.sleep(WAIT_POST_MS / 1000)

            # ===== leer total y mensaje/score =====
            total_resultados = 0
            try:
                badge = page.locator(".form-group.searchintro .badge.badge-info").first
                if await badge.count() > 0 and await badge.is_visible():
                    badge_txt = (await badge.inner_text()).strip()
                    m = re.search(r"\d+", badge_txt)
                    if m:
                        total_resultados = int(m.group(0))
                else:
                    intro = page.locator(".form-group.searchintro").first
                    if await intro.count() > 0:
                        intro_txt = (await intro.inner_text()).strip()
                        m2 = re.search(r"Total:\s*(\d+)", intro_txt, flags=re.I)
                        if m2:
                            total_resultados = int(m2.group(1))
            except Exception:
                total_resultados = 0

            if total_resultados > 0:
                mensaje_final = f"Total: {total_resultados} resultados encontrados."
                score_final = 6
            else:
                mensaje_final = "No se encontraron resultados."
                score_final = 0

            # ===== GUARDAR IMAGEN EN LUGAR DE PDF =====
            await page.screenshot(path=abs_png, full_page=True)

            await ctx.close()
            await browser.close()
            browser = None

        await _guardar_resultado(
            consulta_id=consulta_id,
            fuente_obj=fuente_obj,
            estado="Validada",
            mensaje=mensaje_final,
            rel_path=rel_png,
            score=score_final
        )

    except Exception as e:
        screenshot_path = ""
        if browser:
            try:
                pages = await browser.pages()
                if pages:
                    ts_err = datetime.now().strftime("%Y%m%d_%H%M%S")
                    error_png_name = f"{NOMBRE_SITIO}_ERROR_{ts_err}.png"
                    abs_error_png = os.path.join(absolute_folder, error_png_name)
                    rel_error_png = os.path.join(relative_folder, error_png_name)
                    screenshot_path = rel_error_png

                    await pages[0].screenshot(path=abs_error_png, full_page=True)
            except Exception:
                pass
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

        await _guardar_resultado(
            consulta_id=consulta_id,
            fuente_obj=fuente_obj if 'fuente_obj' in locals() else None,
            estado="Error",
            mensaje="La fuente esta presentando fallas en este momento",
            rel_path=screenshot_path,
            score=0
        )
