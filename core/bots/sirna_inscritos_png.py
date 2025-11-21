# bots/sirna_inscritos_png.py
import os
import re
import asyncio
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "sirna_inscritos_png"

# selectores robustos (clases estables del sitio)
SEL_TIPO_DOC   = "select.ddlTipoDocumentosBusqueda"
SEL_NUMERO     = "input.txtNumDocumento"
SEL_BTN_BUSCAR = "#btnEnviar"
# zona de resultados: la grilla aparece debajo
SEL_RESULTADOS_HINTS = [
    "table",                      # la grilla es una tabla
    "div[data-role='grid']",      # por si cambia a grid
    "#ContentPlaceHolder1",       # contenedor grande
]

# tiempos
WAIT_AFTER_NAV     = 15000
WAIT_AFTER_CLICK   = 2000   # tiempo base tras hacer click en Buscar
EXTRA_RESULT_SLEEP = 1500   # colchón extra para que se pinte la grilla


async def consultar_sirna_inscritos_png(
    consulta_id: int,
    tipo_doc: str,     # "1" CC, "2" CE, "4" PPT, etc. o texto; normalizamos abajo
    numero: str
):
    """
    SIRNA – Profesionales del Derecho y Jueces de Paz:
    - Selecciona tipo de documento
    - Escribe número
    - Click en 'Buscar'
    - Espera resultados
    - **Reescribe el número** en el input para que salga en el screenshot
    - Toma captura y registra en BD
    """
    browser = None

    # normalizar tipo_doc a los valores del <select>
    # acepta números o texto aproximado
    v = (str(tipo_doc) or "").strip().lower()
    if v in ("1", "cc", "cédula de ciudadanía", "cedula de ciudadania"):
        tipo_value = "1"
    elif v in ("2", "ce", "cédula de extranjería", "cedula de extranjeria"):
        tipo_value = "2"
    elif v in ("4", "ppt", "permiso por protección temporal"):
        tipo_value = "4"
    elif v in ("3", "nit"):
        tipo_value = "3"
    else:
        # por defecto CC
        tipo_value = "1"

    # buscar fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="error", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    try:
        # carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_num = re.sub(r"\s+", "_", (numero or "").strip()) or "consulta"
        png_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"
        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="es-CO")
            page = await ctx.new_page()

            await page.goto("https://sirna.ramajudicial.gov.co/Paginas/Inscritos.aspx",
                            wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # seleccionar tipo de documento
            await page.wait_for_selector(SEL_TIPO_DOC, state="visible", timeout=15000)
            await page.select_option(SEL_TIPO_DOC, value=tipo_value)

            # número
            await page.wait_for_selector(SEL_NUMERO, state="visible", timeout=15000)
            inp = page.locator(SEL_NUMERO)
            await inp.click(force=True)
            try:
                await inp.fill("")
            except Exception:
                pass
            await inp.type(numero or "", delay=25)

            # buscar
            await page.locator(SEL_BTN_BUSCAR).click()
            # esperar que carguen resultados
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_CLICK)
            except Exception:
                pass
            # alguna señal de contenido
            for sel in SEL_RESULTADOS_HINTS:
                try:
                    await page.wait_for_selector(sel, timeout=2000)
                    break
                except Exception:
                    continue
            await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)

            # *** RE-ESCRIBIR EL NÚMERO PARA QUE SE VEA EN EL PANTALLAZO ***
            try:
                await inp.fill("")     # volver a llenar
                await inp.type(numero or "", delay=10)
                await asyncio.sleep(0.2)
            except Exception:
                # si por el postback el handle cambió, volvemos a buscar el input
                try:
                    inp2 = page.locator(SEL_NUMERO)
                    await inp2.fill("")
                    await inp2.type(numero or "", delay=10)
                    await asyncio.sleep(0.2)
                except Exception:
                    pass

            # captura
            await page.screenshot(path=abs_png, full_page=True)

            await ctx.close()
            await browser.close()
            browser = None

        # registrar OK
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="ok",
            mensaje="",
            archivo=rel_png,
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="error",
                mensaje=str(e),
                archivo="",
            )
        finally:
            try:
                if browser is not None:
                    await browser.close()
            except Exception:
                pass
