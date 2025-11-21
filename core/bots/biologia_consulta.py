# core/bots/biologia_consulta.py
import os
import re
import asyncio
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright
from core.models import Resultado, Fuente

NOMBRE_SITIO = "biologia_consulta"
URL = "https://consejoprofesionaldebiologia.gov.co/servicios/consulta-estado-matricula-profesional/"

# Selectores
SEL_INPUT_CEDULA = "#campoCedula"
SEL_BTN_CONSULTAR = "#buttConsulta"

async def consultar_biologia_consulta(consulta_id: int, cedula: str):
    """
    Consejo Profesional de Biología – Consulta Estado de Matrícula:
      - Navegar a la página
      - Scroll hasta el campo de cédula
      - Ingresar número de documento
      - Dar clic en "Consultar"
      - Esperar 3s y tomar screenshot
    """
    browser = None

    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    try:
        # Crear carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_doc = re.sub(r"\s+", "_", (cedula or "").strip()) or "consulta"
        png_name = f"{NOMBRE_SITIO}_{safe_doc}_{ts}.png"
        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = await browser.new_context(viewport={"width": 1366, "height": 900}, locale="es-CO")
            page = await ctx.new_page()

            # Navegar
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            await asyncio.sleep(1.5)

            # Scroll para que aparezca el input
            await page.evaluate("window.scrollBy(0, 800);")
            await page.wait_for_selector(SEL_INPUT_CEDULA, timeout=15000)

            # Ingresar cédula
            await page.fill(SEL_INPUT_CEDULA, str(cedula))
            await page.locator(SEL_BTN_CONSULTAR).click()

            # Esperar resultados
            await asyncio.sleep(3)

            # Screenshot
            await page.screenshot(path=abs_png, full_page=True)

            await ctx.close()
            await browser.close()
            browser = None

        # Guardar en BD
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Validada",
            mensaje="",
            archivo=rel_png
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin validar",
                mensaje=str(e),
                archivo=""
            )
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
