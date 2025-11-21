# bots/sirna_sanciones_png.py
import os
import re
import asyncio
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "sirna_sanciones_png"

# Selectores estables del formulario
SEL_TIPO_DOC   = "select.ddlTipoDocumento"
SEL_NUMERO     = "input.txtNumDocumento"
SEL_BTN_BUSCAR = "#btnBuscar"

# Señales de resultados (la página carga una tabla/zona debajo)
SEL_RESULTADOS_HINTS = [
    "table", "#ContentPlaceHolder1", "div[data-role='grid']"
]

# Tiempos
WAIT_AFTER_NAV     = 15000
WAIT_AFTER_CLICK   = 2500
EXTRA_RESULT_SLEEP = 1500


def _norm_tipo(tipo_doc: str) -> str:
    v = (str(tipo_doc) or "").strip().lower()
    if v in ("1", "cc", "cédula de ciudadanía", "cedula de ciudadania"):
        return "1"
    if v in ("2", "ce", "cédula de extranjería", "cedula de extranjeria"):
        return "2"
    if v in ("4", "ppt", "permiso por protección temporal"):
        return "4"
    if v in ("3", "nit"):
        return "3"
    return "1"  # por defecto CC


async def consultar_sirna_sanciones_png(
    consulta_id: int,
    tipo_doc: str,   # "1"|"2"|"4"|"3" o texto ("CC","CE","PPT","NIT")
    numero: str
):
    """
    SIRNA – Sanciones por calidad:
    - Selecciona tipo doc, ingresa número y busca
    - Espera el render de resultados
    - Reescribe el número para que salga en el screenshot
    - Toma pantallazo y registra en BD
    """
    browser = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="error", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    try:
        # Carpeta resultados/<consulta_id>
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

            await page.goto("https://sirna.ramajudicial.gov.co/Paginas/Sanciones.aspx",
                            wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # Tipo de documento
            await page.wait_for_selector(SEL_TIPO_DOC, state="visible", timeout=15000)
            await page.select_option(SEL_TIPO_DOC, value=_norm_tipo(tipo_doc))

            # Número de cédula
            await page.wait_for_selector(SEL_NUMERO, state="visible", timeout=15000)
            inp = page.locator(SEL_NUMERO)
            await inp.click(force=True)
            try:
                await inp.fill("")
            except Exception:
                pass
            await inp.type(numero or "", delay=25)

            # Buscar
            await page.locator(SEL_BTN_BUSCAR).click()
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_CLICK)
            except Exception:
                pass

            # Señales de resultados + colchón
            for sel in SEL_RESULTADOS_HINTS:
                try:
                    await page.wait_for_selector(sel, timeout=2000)
                    break
                except Exception:
                    continue
            await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)

            # Reescribir número para que aparezca visible en el screenshot
            try:
                await inp.fill("")
                await inp.type(numero or "", delay=10)
                await asyncio.sleep(0.2)
            except Exception:
                try:
                    inp2 = page.locator(SEL_NUMERO)
                    await inp2.fill("")
                    await inp2.type(numero or "", delay=10)
                    await asyncio.sleep(0.2)
                except Exception:
                    pass

            # Captura
            await page.screenshot(path=abs_png, full_page=True)

            await ctx.close()
            await browser.close()
            browser = None

        # Registro OK
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
