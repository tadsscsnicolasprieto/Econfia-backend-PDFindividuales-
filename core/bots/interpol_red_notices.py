import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://www.interpol.int/es/Como-trabajamos/Notificaciones/Notificaciones-rojas/Ver-las-notificaciones-rojas"
NOMBRE_SITIO = "interpol_red_notices"

async def consultar_interpol_red_notices(consulta_id: int, nombre: str, cedula):
    """
    Busca `nombre` en el contenedor de resultados de INTERPOL y toma pantallazo.
    Score=0 si no hay coincidencias exactas, score=10 si se encuentra.
    """
    browser = None
    context = None

    # Buscar fuente al inicio
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    # Carpetas/archivos
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_name = f"{NOMBRE_SITIO}_{cedula}_{ts}.png"
    absolute_path = os.path.join(absolute_folder, png_name)
    relative_path = os.path.join(relative_folder, png_name)

    nombre = (nombre or "").strip()
    score_final = 0
    mensaje_final = ""

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1400, "height": 900},
                locale="es-ES"
            )
            page = await context.new_page()

            # 1) Cargar página
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # (Opcional) aceptar cookies
            for sel in [
                "button#onetrust-accept-btn-handler",
                "button:has-text('Aceptar')",
                "button:has-text('Accept')",
            ]:
                try:
                    await page.locator(sel).first.click(timeout=1200)
                    break
                except Exception:
                    pass

            # 2) Abrir buscador
            for sel in [
                "div.search__toggle.js-toggleSearch",
                "button.js-toggleSearch",
                "button[aria-label*='Buscar']",
                "button[aria-label*='Search']",
                ".search__toggle",
            ]:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        await loc.click(timeout=3000)
                        break
                except Exception:
                    continue

            # 3) Escribir en input y buscar
            inp = page.locator("input.search__input[name='search']").first
            await inp.wait_for(state="visible", timeout=10000)
            await inp.fill(nombre)
            await page.wait_for_timeout(200)
            try:
                await page.locator("button.search__trigger").first.click(timeout=4000)
            except Exception:
                try:
                    await inp.press("Enter")
                except Exception:
                    pass

            await page.wait_for_timeout(2500)

            # ======= Buscar coincidencias exactas dentro del contenedor =======
            results_block = page.locator(".search__resultsBlock--results.js-gallery").first
            found = False
            try:
                items = results_block.locator("*")  # todos los elementos dentro
                count = await items.count()
                for i in range(count):
                    texto = (await items.nth(i).inner_text() or "").strip()
                    if texto.lower() == nombre.lower():  # coincidencia exacta
                        found = True
                        break
            except Exception:
                found = False

            if found:
                score_final = 10
                mensaje_final = f"Se encontró la coincidencia exacta: {nombre}"
            else:
                score_final = 0
                mensaje_final = "No hay coincidencias para los parametros ingresados"

            # 4) Pantallazo completo
            await page.screenshot(path=absolute_path, full_page=True)

            await context.close()
            await browser.close()

        # Registrar resultado en BD
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=relative_path
        )

    except Exception as e:
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje=str(e),
            archivo=""
        )
