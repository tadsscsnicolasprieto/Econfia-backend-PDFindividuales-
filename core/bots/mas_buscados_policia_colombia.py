import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
import zipfile

# Ajusta a tu app real
from core.models import Resultado, Fuente

url = "https://www.policia.gov.co/los-mas-buscados-colombia"
nombre_sitio = "mas_buscados_policia_colombia"


async def consultar_mas_buscados_policia_colombia(consulta_id: int, cedula: str, nombre: str, apellido: str):
    navegador = None
    fuente_obj = None

    # Buscar la fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{nombre_sitio}': {e}",
            archivo=""
        )
        return

    try:
        # Carpeta resultados/<consulta_id>
        relative_folder = os.path.join('resultados', str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Nombre de captura (siempre la generamos)
        screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
        abs_screenshot = os.path.join(absolute_folder, screenshot_name)
        rel_screenshot = os.path.join(relative_folder, screenshot_name)

        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()
            await pagina.goto(url)
            # Espera a que cargue algo de la página (caja de resultados o tarjetas)
            try:
                await pagina.wait_for_selector(".view-content, .buscado", timeout=20000)
            except Exception:
                # igual continuamos; haremos captura y consideramos sin hallazgos
                pass

            # Normalizar criterio de búsqueda
            nombre_busqueda = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip().lower()

            encontrados_paths_abs = []
            found_any = False

            # --------- 1) Detección por .view-content ----------
            try:
                rows = pagina.locator("div.view-content .views-row")
                total_rows = await rows.count()
                if total_rows > 0:
                    if nombre_busqueda:
                        for i in range(total_rows):
                            link = rows.nth(i).locator("a").first
                            if await link.count() > 0:
                                t = (await link.inner_text() or "").strip().lower()
                                if nombre_busqueda in t:
                                    found_any = True
                                    break
                    else:
                        found_any = True
            except Exception:
                pass

            # --------- 2) Fallback: tarjetas .buscado y descarga de PDFs ----------
            try:
                bloques = pagina.locator(".buscado")
                total = await bloques.count()
                for i in range(total):
                    try:
                        nombre_completo = await bloques.nth(i).locator(".nombre").inner_text()
                    except Exception:
                        continue
                    nombre_completo = (nombre_completo or "").strip().lower()

                    match_tarjeta = (not nombre_busqueda) or (nombre_busqueda in nombre_completo)
                    if not match_tarjeta:
                        continue

                    found_any = True  # al menos una tarjeta coincide

                    # Intentar descargar PDF asociado
                    link_pdf = None
                    try:
                        link_pdf = await bloques.nth(i).locator(".file-link a").get_attribute("href")
                    except Exception:
                        link_pdf = None

                    if link_pdf:
                        if link_pdf.startswith("/"):
                            link_pdf = "https://www.policia.gov.co" + link_pdf

                        pdf_name = f"{nombre_sitio}_{cedula}_{timestamp}_{i+1}.pdf"
                        abs_pdf_path = os.path.join(absolute_folder, pdf_name)

                        resp = await pagina.request.get(link_pdf)
                        if resp.ok:
                            with open(abs_pdf_path, "wb") as f:
                                f.write(await resp.body())
                            encontrados_paths_abs.append(abs_pdf_path)
            except Exception:
                pass

            # --------- 3) SIEMPRE: captura de pantalla ---------
            try:
                await pagina.screenshot(path=abs_screenshot, full_page=True)
            except Exception:
                # último recurso: nada
                pass

            await navegador.close()
            navegador = None

        # ------- Resultado y archivo (ZIP si descargó PDFs; si no, screenshot) -------
        if found_any:
            archivo_rel = rel_screenshot  # por defecto, la captura
            if encontrados_paths_abs:
                zip_name = f"{nombre_sitio}_{cedula}_{timestamp}.zip"
                abs_zip_path = os.path.join(absolute_folder, zip_name)
                archivo_rel = os.path.join(relative_folder, zip_name)

                with zipfile.ZipFile(abs_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for abs_path in encontrados_paths_abs:
                        zf.write(abs_path, arcname=os.path.basename(abs_path))

            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=10,  # hay hallazgos
                estado="Validada",
                mensaje="con hallazgoz",
                archivo=archivo_rel
            )
        else:
            # sin hallazgos -> adjuntamos la captura
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,  # sin hallazgos
                estado="Validada",
                mensaje="sin hallazgos",
                archivo=rel_screenshot
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
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
