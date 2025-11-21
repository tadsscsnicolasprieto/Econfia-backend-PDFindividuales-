# core/bots/un_consolidated_list.py 
import os, re, asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://main.un.org/securitycouncil/es/content/un-sc-consolidated-list"
NOMBRE_SITIO = "consolidated_list_onu"

async def _accept_cookies(page):
    for sel in [
        "button:has-text('Acepto las cookies')",
        "button:has-text('Accept all cookies')",
        "button#onetrust-accept-btn-handler",
        "button:has-text('I accept')",
        "button:has-text('Aceptar')",
    ]:
        try:
            await page.locator(sel).first.click(timeout=1500)
            return True
        except:
            pass
    return False


async def _run_scraper(nombre: str, cedula: str, absolute_path: str):
    """
    Lógica principal del scraping. Devuelve True si fue exitoso, False si falló.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="es-ES",
        )
        page = await context.new_page()

        await page.goto(URL, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except:
            pass

        await _accept_cookies(page)

        # Abrir buscador
        togglers = [
            "button.search-toggler.d-none.align-self-center.btn.gap-0.shadow-none.text-uppercase.ms-g.mb-0.p-0.d-lg-flex",
            "button.search-toggler[data-bs-target*='block-simple-search-form']",
            "button.search-toggler",
            "button[aria-controls*='block-simple-search-form']",
        ]
        for sel in togglers:
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    await loc.click(timeout=4000)
                    break
            except:
                continue

        # Input de búsqueda
        inp = page.locator("#edit-p--2").first
        if not await inp.count():
            inp = page.locator("input.block-serach.form-search.form-control").first
        if not await inp.count():
            inp = page.locator("input[placeholder*='Busca algo'], input[placeholder*='¿Busca algo?']").first

        btn = page.locator("#edit-submit--2").first
        if not await btn.count():
            btn = page.locator("button[type='submit']:has-text('Buscar'), input[type='submit'][value*='Buscar']").first

        await inp.wait_for(state="visible", timeout=10000)
        await inp.click()
        await inp.fill(nombre or "")
        await asyncio.sleep(0.25)

        try:
            if await btn.count() > 0:
                await btn.click()
            else:
                await inp.press("Enter")
        except:
            try:
                await inp.press("Enter")
            except:
                pass

        await asyncio.sleep(2.5)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except:
            pass
        try:
            await page.wait_for_selector(
                ".view, .search-results, .region-content, main, article, .views-row",
                timeout=12000
            )
        except:
            pass

        try:
            await page.mouse.wheel(0, 600)
            await asyncio.sleep(0.3)
        except:
            pass

        # Guardar screenshot SIEMPRE (funcione o no)
        await page.screenshot(path=absolute_path, full_page=True)
        await browser.close()

    return True


async def consultar_un_consolidated_list(consulta_id: int, nombre: str, cedula: str):
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"{NOMBRE_SITIO}_{cedula}_{ts}.png"
    absolute_path = os.path.join(absolute_folder, file_name)
    relative_path = os.path.join(relative_folder, file_name)

    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)

    success = False
    last_exception = None

    # Intentos máximos
    for intento in range(3):
        try:
            await _run_scraper(nombre, cedula, absolute_path)
            success = True
            break
        except Exception as e:
            last_exception = e
            await asyncio.sleep(2)  # pequeño delay entre intentos

    if success:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Validado",
            mensaje="",
            archivo=relative_path
        )
    else:
        # Guardar error y pantallazo
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje=str(last_exception),
            archivo=relative_path if os.path.exists(absolute_path) else ""
        )
