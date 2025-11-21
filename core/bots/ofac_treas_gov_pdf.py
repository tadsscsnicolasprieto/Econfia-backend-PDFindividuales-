import os
import re
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://sanctionssearch.ofac.treas.gov/"
NOMBRE_SITIO = "ofac_treas"


async def consultar_ofac_treas_pdf(consulta_id: int, nombre: str, cedula):
    """
    Busca 'nombre' en OFAC (Treasury) y guarda un pantallazo de la página de resultados.
    Reintenta hasta 3 veces antes de registrar error en la BD.
    """
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe_name = re.sub(r"\s+", "_", (nombre or "consulta").strip()) or "consulta"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    intento = 0
    success = False
    relative_file = ""
    score = 0
    mensaje = ""

    while intento < 3 and not success:
        intento += 1
        try:
            screenshot_name = f"{NOMBRE_SITIO}_{cedula}_{ts}.png"
            absolute_path = os.path.join(absolute_folder, screenshot_name)
            relative_file = os.path.join(relative_folder, screenshot_name)

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    locale="en-US",
                    viewport={"width": 1440, "height": 2000}  # ajustar altura según necesidad
                )
                page = await context.new_page()

                # Abrir sitio
                await page.goto(URL, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                # Llenar campos de búsqueda
                name_input = page.locator("#ctl00_MainContent_txtLastName").first
                id_input = page.locator("#ctl00_MainContent_txtID").first
                await name_input.wait_for(state="visible", timeout=12000)
                await name_input.fill(nombre or "")
                await id_input.fill(cedula or "")

                await page.locator("#ctl00_MainContent_btnSearch").first.click()

                # Esperar resultados
                got = False
                for sel in [
                    "#ctl00_MainContent_gvResults",
                    "text=Lookup Results",
                    "table:has(th:has-text('Name'))",
                    "tbody tr",
                ]:
                    try:
                        await page.wait_for_selector(sel, timeout=12000)
                        got = True
                        break
                    except Exception:
                        continue
                if not got:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass

                # Verificar mensaje de "no results"
                no_result_span = page.locator("#ctl00_MainContent_lblMessage")
                if await no_result_span.count() > 0:
                    texto = (await no_result_span.text_content() or "").strip()
                    if "Your search has not returned any results" in texto:
                        score = 0
                        mensaje = texto
                    else:
                        score = 10
                        mensaje = "Se encontraron resultados"
                else:
                    score = 10
                    mensaje = "Se encontraron resultados"

                # Tomar pantallazo completo
                await page.screenshot(path=absolute_path, full_page=True)
                await browser.close()

            # Guardar en BD
            success = True
            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validado",
                mensaje=mensaje,
                archivo=relative_file
            )

        except Exception as e:
            # Inicializar rutas para evitar errores si ocurre excepción antes de su definición
            error_abs = locals().get('error_abs', 'error_screenshot.png')
            error_rel = locals().get('error_rel', '')
            try:
                error_png = f"{NOMBRE_SITIO}_{cedula}_{ts}_error_intento{intento}.png"
                error_abs = os.path.join(absolute_folder, error_png)
                error_rel = os.path.join(relative_folder, error_png)
                if 'page' in locals():
                    await page.screenshot(path=error_abs, full_page=True)
            except Exception:
                error_rel = ""

            if intento == 3:
                fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin validar",
                    mensaje=f"Ocurrió un problema al obtener la información de la fuente: {e}",
                    archivo=error_rel
                )
