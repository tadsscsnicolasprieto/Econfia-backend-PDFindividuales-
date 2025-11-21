import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

nombre_sitio = "medicaldevices"

async def consultar_medical_devices(consulta_id: int, nombre_empresa: str):
    """
    Consulta medicaldevices.icij.org, acepta términos, toma captura y
    guarda el resultado en la BD:
      - No resultados => score=0, mensaje="No results found"
      - Con resultados => score=10, mensaje="con hallazgo"
    Archivos en MEDIA_ROOT/resultados/<consulta_id>/.
    """
    navegador = None
    fuente_obj = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{nombre_sitio}': {e}", archivo=""
        )
        return

    nombre_empresa = (nombre_empresa or "").strip()
    # Construir URL de búsqueda
    nombre_busqueda = (nombre_empresa.replace(" ", "+") if nombre_empresa else "")
    url = f"https://medicaldevices.icij.org/search?q%5Bdisplay_cont%5D={nombre_busqueda}"

    # Carpetas de salida
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_name = f"{nombre_sitio}_{(nombre_empresa or 'consulta').replace(' ', '_')}_{ts}.png"
    abs_png = os.path.join(absolute_folder, screenshot_name)
    rel_png = os.path.join(relative_folder, screenshot_name)

    # Selectores
    TERMS_CHECK = 'label[for="termsCheck"]'
    TERMS_BTN   = 'button.btn.btn-primary.font-weight-bold.text-uppercase.ml-2'
    TABLE_BODY  = "table.search__results tbody"
    NORES_BOX   = "div.text-center.p-3.border.border-light.rounded.text-muted"

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            page = await navegador.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=120000)

            # Aceptar términos (best-effort)
            try:
                if await page.locator(TERMS_CHECK).first.is_visible():
                    await page.locator(TERMS_CHECK).first.click(timeout=1500)
                if await page.locator(TERMS_BTN).first.is_visible():
                    await page.locator(TERMS_BTN).first.click(timeout=1500)
            except Exception:
                pass

            # Esperar a que carguen resultados o el recuadro de "no results"
            try:
                await page.wait_for_selector(f"{TABLE_BODY}, {NORES_BOX}", timeout=15000)
            except Exception:
                pass

            # Decidir estado
            score_final = 0
            mensaje_final = "No se encuentran coincidencias."
            shot_target = page  # por defecto, captura de página completa

            # ¿Hay filas de resultados?
            try:
                rows = page.locator(f"{TABLE_BODY} tr")
                if await rows.count() > 0:
                    # Con hallazgo
                    score_final = 10
                    mensaje_final = "con hallazgo"
                    # Capturar la tabla si es posible
                    table_container = page.locator("div.table-responsive").first
                    if await table_container.count() > 0:
                        shot_target = table_container
            except Exception:
                pass

            # Si no se detectaron filas, revisar recuadro de "No results found"
            if score_final == 0:
                try:
                    nores = page.locator(NORES_BOX).first
                    if await nores.count() > 0 and await nores.is_visible():
                        raw = (await nores.inner_text() or "").strip()
                        # Normalizar y quedarnos solo con el texto:
                        raw = " ".join(raw.split())
                        # Forzar exactamente el texto pedido si contiene "No results found"
                        mensaje_final = "No results found" if "no results found" in raw.lower() else raw
                        shot_target = nores
                except Exception:
                    pass

            # Captura
            try:
                await shot_target.screenshot(path=abs_png)
            except Exception:
                # Último recurso: pantalla completa
                await page.screenshot(path=abs_png, full_page=True)

            await navegador.close()
            navegador = None

        # Guardar en BD (estado ok)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=rel_png
        )

    except Exception as e:
        # Registrar error
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=str(e),
                archivo=""
            )
        finally:
            try:
                if navegador:
                    await navegador.close()
            except Exception:
                pass
