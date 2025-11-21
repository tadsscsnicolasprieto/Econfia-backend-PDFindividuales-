import os
import re
import asyncio
from datetime import datetime
from PIL import Image, ImageDraw

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "supersolidaria_noticias"
URL = "https://www.supersolidaria.gov.co/index.php/es/content/noticias"

SEL_INPUT_VISIBLE   = "#edit-keys:visible"
SEL_INPUT_FALLBACK  = "main input.form-text#edit-keys:visible, main input[name='keys'].form-text:visible"
SEL_HINTS = [".view-content", "article", ".region-content", "main", ".block-system-main-block"]

WAIT_NAV   = 15000
WAIT_POST  = 3000

async def consultar_supersolidaria_noticias(consulta_id: int, nombre: str, apellido: str):
    max_intentos = 3
    intentos = 0
    error_final = None
    browser = None

    query = (f"{(nombre or '').strip()} {(apellido or '').strip()}").strip() or "consulta"
    safe_query = re.sub(r"\s+", "_", query)

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="sin validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    while intentos < max_intentos:
        try:
            intentos += 1

            # Carpeta
            relative_folder = os.path.join("resultados", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            png_name = f"{NOMBRE_SITIO}_{safe_query}_{ts}.png"
            abs_png = os.path.join(absolute_folder, png_name)
            rel_png = os.path.join(relative_folder, png_name)

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
                ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="es-CO")
                page = await ctx.new_page()

                # Navegar
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=WAIT_NAV)
                except Exception:
                    pass

                # Cookies
                try:
                    cookies_btn = page.locator("button:has-text('Aceptar'), .eu-cookie-compliance-default-button")
                    if await cookies_btn.first.is_visible():
                        await cookies_btn.first.click()
                except Exception:
                    pass

                # Scroll a contenido principal
                try:
                    main = page.locator("main")
                    if await main.first.count() > 0:
                        el = await main.first.element_handle()
                        if el:
                            await page.evaluate("(el) => el.scrollIntoView({behavior:'instant', block:'start'})", el)
                            await asyncio.sleep(0.2)
                except Exception:
                    pass

                # Input visible o fallback
                try:
                    await page.wait_for_selector(SEL_INPUT_VISIBLE, state="visible", timeout=7000)
                    input_loc = page.locator(SEL_INPUT_VISIBLE).first
                except Exception:
                    await page.wait_for_selector(SEL_INPUT_FALLBACK, state="visible", timeout=8000)
                    input_loc = page.locator(SEL_INPUT_FALLBACK).first

                # Escribir y presionar Enter
                await input_loc.click()
                try: await input_loc.fill("") 
                except Exception: pass
                await input_loc.type(query, delay=25)
                await input_loc.press("Enter")

                # Esperar render de resultados
                try:
                    await page.wait_for_load_state("networkidle", timeout=WAIT_NAV)
                except Exception: pass
                await asyncio.sleep(WAIT_POST / 1000)

                # Centrar resultados
                try:
                    tgt = None
                    for sel in SEL_HINTS:
                        try:
                            await page.wait_for_selector(sel, state="visible", timeout=1200)
                            tgt = page.locator(sel).first
                            break
                        except Exception:
                            continue
                    if tgt:
                        el = await tgt.element_handle()
                        if el:
                            await page.evaluate(
                                "(el) => { const r = el.getBoundingClientRect(); window.scrollTo({top: r.top + window.scrollY - 120, behavior:'instant'}); }",
                                el
                            )
                            await asyncio.sleep(0.2)
                except Exception:
                    pass

                # Screenshot
                await page.screenshot(path=abs_png, full_page=False)

                await ctx.close()
                await browser.close()
                browser = None

            # Guardar resultado como VALIDADO
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="validado",
                mensaje="No se encontraron hallazgos",
                archivo=rel_png,
            )
            return

        except Exception as e:
            error_final = e
            if browser:
                try: await browser.close()
                except Exception: pass
            if intentos < max_intentos:
                continue

    # Guardar error tras 3 intentos con screenshot dummy
    try:
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        png_name = f"{NOMBRE_SITIO}_error_{safe_query}_{ts}.png"
        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name)

        from PIL import Image, ImageDraw
        img = Image.new("RGB", (800, 600), color=(255, 255, 255))
        d = ImageDraw.Draw(img)
        d.text((10, 10), f"Error en consulta Supersolidaria: {str(error_final)}", fill=(0, 0, 0))
        img.save(abs_png)

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje="Ocurrió un problema al obtener la información de la fuente",
            archivo=rel_png,
        )
    except Exception as e2:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje=f"Error guardando fallo: {e2}",
            archivo=""
        )
