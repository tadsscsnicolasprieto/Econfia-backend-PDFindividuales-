# core/bots/embajada_alemania_funcionarios.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "embajada_alemania_funcionarios"
URL = "https://alemania.embajada.gov.co/acerca/funcionarios"

# Selectores (el sitio es Drupal y a veces duplica el mismo id)
SEL_INPUT_VISIBLE   = "#edit-keys:visible"
SEL_INPUT_FALLBACK  = "main input#edit-keys.form-search:visible, main input[name='keys'].form-search:visible"

RESULT_HINTS = [
    ".view-content", ".region-content", "main", "article", ".block-system-main-block",
]

WAIT_NAV_MS  = 15000
WAIT_POST_MS = 2500

async def consultar_embajada_alemania_funcionarios(
    consulta_id: int,
    nombre: str,
    apellido: str,
):
    browser = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo="",
        )
        return

    try:
        # 2) Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        query = (f"{(nombre or '').strip()} {(apellido or '').strip()}").strip() or "consulta"
        safe_query = re.sub(r"\s+", "_", query)
        png_name = f"{NOMBRE_SITIO}_{safe_query}_{ts}.png"
        abs_png  = os.path.join(absolute_folder, png_name)
        rel_png  = os.path.join(relative_folder, png_name)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled", "--start-maximized"]
            )
            ctx = await browser.new_context(viewport=None, locale="es-CO")
            page = await ctx.new_page()

            # 3) Ir al sitio
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_NAV_MS)
            except Exception:
                pass

            # 4) Cerrar cookies/popups genéricos si aparecen (best-effort)
            try:
                for sel in [
                    "button:has-text('Aceptar')",
                    ".eu-cookie-compliance-default-button",
                    "button.close, .close[aria-label='Close']",
                ]:
                    btns = page.locator(sel)
                    for i in range(await btns.count()):
                        b = btns.nth(i)
                        if await b.is_visible():
                            try:
                                await b.click(timeout=1000)
                            except Exception:
                                pass
            except Exception:
                pass

            # 5) Asegurar que el contenido principal esté a la vista
            try:
                main = page.locator("main").first
                if await main.count() > 0:
                    el = await main.element_handle()
                    if el:
                        await page.evaluate("(el)=>el.scrollIntoView({behavior:'instant', block:'start'})", el)
                        await asyncio.sleep(0.2)
            except Exception:
                pass

            # 6) Localizar el input visible (hay duplicados con el mismo id)
            try:
                await page.wait_for_selector(SEL_INPUT_VISIBLE, state="visible", timeout=7000)
                input_loc = page.locator(SEL_INPUT_VISIBLE).first
            except Exception:
                await page.wait_for_selector(SEL_INPUT_FALLBACK, state="visible", timeout=8000)
                input_loc = page.locator(SEL_INPUT_FALLBACK).first

            # 7) Escribir y Enter
            await input_loc.click()
            try:
                await input_loc.fill("")
            except Exception:
                pass
            await input_loc.type(query, delay=25)
            await input_loc.press("Enter")

            # 8) Esperar render
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_NAV_MS)
            except Exception:
                pass
            await asyncio.sleep(WAIT_POST_MS / 1000)

            # ===== LÓGICA DE MENSAJE/ SCORE =====
            # a) Detectar "Su búsqueda no produjo resultados"
            nores_sel = "div.content h3:has-text('Su búsqueda no produjo resultados'), h3:has-text('Su búsqueda no produjo resultados')"
            nores = page.locator(nores_sel).first

            if (await nores.count()) > 0 and (await nores.is_visible()):
                score_final = 0
                mensaje_final = "Su búsqueda no produjo resultados"
            else:
                # En cualquier otro caso, considerar hallazgos
                score_final = 10
                mensaje_final = "Se encontraron hallazgos"

            # 9) Screenshot de página COMPLETA
            await page.screenshot(path=abs_png, full_page=True)

            await ctx.close()
            await browser.close()
            browser = None

        # 10) Registrar OK
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=rel_png,
        )

    except Exception as e:
        # Error + cierre seguro
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=str(e),
                archivo="",
            )
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
