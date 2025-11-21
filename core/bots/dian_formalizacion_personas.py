# core/bots/dian_formalizacion_personas.py
import os
import asyncio
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "dian_formalizacion_personas"
URL = ("https://muisca.dian.gov.co/WebArquitectura/DefFormalizacionPersonas.faces"
       "?idRequest=cf256177c0a8d10f0b3d74bd4a2e6904")

# Mapeo exacto según <option value="..."> del <select>
TIPO_DOC_MAP = {
    "CC": "10910094",   # Cédula de Ciudadanía
    "TI": "10910093",   # Tarjeta de Identidad
    "CE": "10910096",   # Cédula de Extranjería
    "PAS": "10910098",  # Pasaporte
    "DIE": "10910394",  # Documento de Identificación Extranjero
}

async def consultar_dian_formalizacion_personas(
    consulta_id: int,
    cedula: str,
    tipo_doc: str,
):
    """
    Flujo:
      1) Abre la página.
      2) Cierra popup de error si aparece.
      3) Selecciona tipo de documento y digita la cédula.
      4) Click en 'Siguiente'.
      5) Detecta mensaje de 'no habilitó su cuenta' (sin resultados) o hallazgos.
      6) Toma screenshot.
      7) Guarda Resultado en BD con estado/mensaje/score.
    """
    browser = None
    fuente_obj = None

    # Buscar fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo="",
        )
        return

    try:
        # Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        png_name = f"{NOMBRE_SITIO}_{cedula}_{ts}.png"
        absolute_path = os.path.join(absolute_folder, png_name)
        relative_path = os.path.join(relative_folder, png_name).replace("\\", "/")

        # Valor del select según mapa
        tipo_value = TIPO_DOC_MAP.get((tipo_doc or "").upper(), TIPO_DOC_MAP["CC"])

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="es-CO",
            )
            page = await context.new_page()

            # 1) Abrir
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(0.5)

            # 2) Cerrar popup de error si aparece
            try:
                btn = page.locator("img[onclick*='cerrarLayer']")
                if await btn.count() > 0:
                    await btn.first.click(timeout=2000)
                else:
                    await page.get_by_text("Error", exact=False).first.wait_for(timeout=2000)
                    await page.locator("img[src*='botcerrarrerror'], img[alt='Cerrar']").first.click(timeout=1500)
            except Exception:
                pass

            # 3) Seleccionar tipo de documento
            try:
                await page.select_option(
                    "#vistaformalizacionPersona\\:frmformalizacionPersona\\:selecTipoIdentificacion",
                    value=tipo_value
                )
            except Exception:
                try:
                    await page.click("#vistaformalizacionPersona\\:frmformalizacionPersona\\:selecTipoIdentificacion")
                    await page.locator(f"option[value='{tipo_value}']").click()
                except Exception:
                    pass

            # 4) Escribir cédula
            input_ced = page.locator("#vistaformalizacionPersona\\:frmformalizacionPersona\\:txtIdentificacion")
            await input_ced.wait_for(state="visible", timeout=10000)
            await input_ced.fill(str(cedula))

            # 5) Clic en 'Siguiente'
            try:
                await page.locator("input[type='image'][src*='botsiguiente']").first.click(timeout=5000)
            except Exception:
                await page.locator("input[name='vistaformalizacionPersona:frmformalizacionPersona:_id217']").first.click(timeout=5000)

            # 6) Espera de resultados / mensajes
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(1.5)

            # ---- Detección de “no habilitó su cuenta” vs. hallazgos ----
            score = 0
            mensaje = ""

            # El bloque de error suele estar dentro de .elDivScroll
            err_box = page.locator("div.elDivScroll").first
            if await err_box.count() > 0 and await err_box.is_visible():
                # Tomar el texto tal cual del contenedor
                try:
                    mensaje = (await err_box.inner_text()).strip()
                except Exception:
                    mensaje = "El usuario no ha habilitado su cuenta."
                score = 0  # sin resultados
            else:
                # Si no aparece el contenedor de error, asumimos hallazgos
                mensaje = "se hallaron resultados"
                score = 10

            # 7) Screenshot
            await page.screenshot(path=absolute_path, full_page=True)

            await browser.close()
            browser = None

        # Guardar en BD
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score,
            estado="Validada",
            mensaje=mensaje,
            archivo=relative_path,
        )

    except Exception as e:
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
            try:
                if browser is not None:
                    await browser.close()
            except Exception:
                pass
