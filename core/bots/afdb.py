# core/bots/afdb.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://www.afdb.org/en"
NOMBRE_SITIO = "afdb"

SEARCH_INPUT = "#edit-search-block-form--2"
VIEW_EMPTY   = ".view-empty"          # contenedor Drupal cuando no hay resultados
ROW_SEL      = ".views-row"           # cada resultado
TITLE_SEL    = ".views-field-title"   # título dentro del row
BODY_SEL     = ".views-field-body"    # snippet dentro del row
PATH_SEL     = ".views-field-path"    # url visible dentro del row

NAV_TIMEOUT_MS = 120_000
WAIT_IDLE_MS   = 6_000
WAIT_AFTER_MS  = 1_000

def _norm(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s.upper()

async def consultar_afdb(consulta_id: int, nombre: str, apellido: str):
    """Busca nombre completo en afdb.org y registra evidencia + coincidencias exactas."""
    # 1) Fuente
    try:
        fuente = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    # 2) Preparar paths
    full_name_raw = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()
    full_name_disp = full_name_raw or "consulta"
    safe = re.sub(r"[^\w\.-]+", "", re.sub(r"\s+", "", full_name_disp))
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_name = f"{NOMBRE_SITIO}{safe}{ts}.png"
    abs_png  = os.path.join(absolute_folder, png_name)
    rel_png  = os.path.join(relative_folder, png_name).replace("\\", "/")

    # 3) Playwright
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = await ctx.new_page()

            # Ir al home
            await page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_IDLE_MS)
            except Exception:
                pass

            # Escribir y Enter
            await page.wait_for_selector(SEARCH_INPUT, state="visible", timeout=15000)
            await page.fill(SEARCH_INPUT, full_name_raw)
            await page.keyboard.press("Enter")

            # Esperar navegación a resultados / señales de contenido
            try:
                await page.wait_for_url("/search/", timeout=20000)
            except Exception:
                # Si no cambió la URL, igual espera contenedores
                pass

            # Esperar a que haya o VACÍO o filas
            try:
                await page.wait_for_selector(f"{VIEW_EMPTY}, {ROW_SEL}", timeout=20000)
            except Exception:
                # dar un pequeño respiro extra
                await page.wait_for_timeout(WAIT_AFTER_MS)

            # ¿No hay resultados?
            nores = await page.locator(VIEW_EMPTY).count() > 0
            if nores:
                mensaje = "Unfortunately your search did not return any results."
                score = 1
                # screenshot full page
                try:
                    await page.evaluate("window.scrollTo(0, 0)")
                except Exception:
                    pass
                await page.screenshot(path=abs_png, full_page=True)

                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente, score=score,
                    estado="Validado", mensaje=mensaje, archivo=rel_png
                )
                await ctx.close(); await browser.close()
                return

            # Hay filas -> revisar coincidencia EXACTA del nombre en todos los rows
            # Normalizamos comparaciones
            needle = _norm(full_name_raw)
            exact_hit = False

            rows = page.locator(ROW_SEL)
            try:
                n = await rows.count()
            except Exception:
                n = 0

            for i in range(n):
                row = rows.nth(i)
                # Extraer textos relevantes
                def safe_text(loc):
                    async def inner():
                        try:
                            el = row.locator(loc).first
                            if await el.count():
                                return (await el.inner_text()) or ""
                        except Exception:
                            pass
                        return ""
                    return inner()

                title = await safe_text(TITLE_SEL)
                body  = await safe_text(BODY_SEL)
                pathv = await safe_text(PATH_SEL)

                blob = _norm(" ".join([title, body, pathv]))
                if needle and needle in blob:
                    exact_hit = True
                    break

            if exact_hit:
                mensaje = "Se encontraron coincidencias."
            else:
                mensaje = "No hay coincidencias."

            score = 1  # siempre 1 (según requerimiento)

            # Evidencia (página completa)
            try:
                await page.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass
            await page.screenshot(path=abs_png, full_page=True)

            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente, score=score,
                estado="Validado", mensaje=mensaje, archivo=rel_png
            )

            await ctx.close(); await browser.close()

    except Exception as e:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente, score=0,
            estado="Sin validar", mensaje=str(e), archivo=""
        )