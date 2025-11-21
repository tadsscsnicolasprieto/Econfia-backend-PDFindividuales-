# consulta/eu_sanctions_tracker.py (versión async adaptada a BD)
import os, re, asyncio
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

# Ajusta a tu app real
from core.models import Resultado, Fuente

URL = "https://data.europa.eu/apps/eusanctionstracker/"
NOMBRE_SITIO = "eu_sanctions_tracker"

async def _accept_cookies(page):
    for sel in [
        "button:has-text('I accept cookies')",
        "button:has-text('Accept all cookies')",
        "button#onetrust-accept-btn-handler",
        "button:has-text('I accept')",
    ]:
        try:
            await page.locator(sel).first.click(timeout=1500)
            return True
        except Exception:
            pass
    return False

async def consultar_eu_sanctions_tracker(consulta_id: int, nombre_completo: str):
    """
    Busca `nombre_completo` en EU sanctions tracker y toma un pantallazo del resultado
    (o del mensaje 'No results found'). Guarda en MEDIA_ROOT/resultados/<consulta_id>/.
    En lugar de return, registra el resultado en la BD.
    """
    navegador = None
    nombre_completo = (nombre_completo or "").strip()

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    if not nombre_completo:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje="Nombre vacío para la consulta.", archivo=""
        )
        return

    try:
        # 2) Carpeta destino
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        # 3) Nombre de archivo
        safe = re.sub(r"\s+", "_", nombre_completo) or "consulta"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        img_name = f"{NOMBRE_SITIO}_{safe}_{ts}.png"
        absolute_path = os.path.join(absolute_folder, img_name)
        relative_path = os.path.join(relative_folder, img_name)

        # 4) Navegación y captura
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=False)
            context = await navegador.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
            page = await context.new_page()

            await page.goto(URL, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass

            await _accept_cookies(page)

            # Localizar combobox (Tom Select)
            inp = None
            try:
                inp = page.get_by_role("combobox", name=re.compile(r"Search sanctions", re.I))
                await inp.wait_for(state="visible", timeout=10000)
            except Exception:
                for s in [
                    "div#search-field-ts-control input",
                    "input#search-field",
                    "input[role='combobox']",
                    "input[aria-autocomplete='list']",
                ]:
                    loc = page.locator(s).first
                    if await loc.count() > 0:
                        inp = loc
                        break

            if inp is None:
                await page.screenshot(path=absolute_path, full_page=True)
                await context.close(); await navegador.close(); navegador = None
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj, score=0,
                    estado="Sin Validar", mensaje="No se encontró el campo de búsqueda del tracker.", archivo=""
                )
                return

            query = nombre_completo
            await inp.click()
            try:
                await inp.fill("")
            except Exception:
                pass
            await inp.type(query, delay=50)
            await asyncio.sleep(0.5)

            # Si .type no dejó valor, forzar value + events
            try:
                if query and (await inp.input_value() or "").strip() == "":
                    el = await inp.element_handle()
                    await page.evaluate(
                        """(el, val) => { el.value = val; el.dispatchEvent(new Event('input', {bubbles:true})); }""",
                        el, query
                    )
            except Exception:
                pass

            # Esperar dropdown
            try:
                await page.locator(".ts-dropdown").wait_for(state="visible", timeout=8000)
            except Exception:
                pass

            # ===== Detección SIN resultados =====
            try:
                nores = page.locator(".ts-dropdown .no-results:has-text('No results found')")
                if await nores.count() > 0 and await nores.first.is_visible():
                    await page.screenshot(path=absolute_path, full_page=True)
                    await context.close(); await navegador.close(); navegador = None
                    # score 0 y mensaje plano "No results found"
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id, fuente=fuente_obj, score=0,
                        estado="Validada", mensaje="No results found", archivo=relative_path
                    )
                    return
            except Exception:
                pass

            # ===== Con resultados: seleccionar el primero y capturar =====
            found = False
            first_opt = page.locator(".ts-dropdown .option").first
            if await first_opt.count() > 0:
                found = True
                await first_opt.click()
                try:
                    await page.wait_for_selector("h1, h2, .profile, .details, [role='main']", timeout=12000)
                except Exception:
                    await asyncio.sleep(3)
                try:
                    await page.wait_for_selector(".chart, table, .dataTables_wrapper, .related-entities", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(2.5)
                try:
                    await page.mouse.wheel(0, 1500)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
                await page.screenshot(path=absolute_path, full_page=True)
            else:
                # Fallback: Enter + capturar algo del estado actual
                try:
                    await inp.press("Enter")
                    await asyncio.sleep(4)
                except Exception:
                    pass
                # intentar detectar si apareció algo en pantalla
                if await page.locator(".ts-dropdown .option").count() > 0:
                    found = True
                await page.screenshot(path=absolute_path, full_page=True)

            await context.close()
            await navegador.close()
            navegador = None

        # 5) Registrar según se hayan encontrado hallazgos
        if found:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj, score=10,
                estado="Validada", mensaje="Se han encontrado hallazgos", archivo=relative_path
            )
        else:
            # fallback conservador si no se detectó explícitamente pero tampoco hubo "no results"
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj, score=0,
                estado="Validada", mensaje="No results found", archivo=relative_path
            )

    except Exception as e:
        try:
            if navegador is not None:
                await navegador.close()
        except Exception:
            pass
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje=str(e), archivo=""
        )
