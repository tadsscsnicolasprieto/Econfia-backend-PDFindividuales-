# core/bots/biologia_validacion_certificados.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "biologia_validacion_certificados"
URL = "https://consejoprofesionaldebiologia.gov.co/servicios/validacion-certificados/"

# Selectores
SEL_INPUT_CODIGO = "#campoCodigoCert"
SEL_BTN_CONSULTAR = "#btnConsultarCAV"

# Ayudas para centrar la vista / detectar contenido
SEL_HINTS = [
    "#resultado",     # por si existe un contenedor específico de resultados
    ".row", ".card", ".container", ".col", ".content",   # genéricos
]

WAIT_AFTER_NAV = 15000
WAIT_AFTER_CLICK = 3000


async def consultar_biologia_validacion_certificados(
    consulta_id: int,
    codigo: str,  # temporalmente usamos la cédula como código
):
    """
    Consejo Profesional de Biología – Validación de certificados:
      - Abre la página
      - Hace scroll hasta el formulario
      - Escribe el 'codigo' en #campoCodigoCert
      - Clic en 'Consultar' (#btnConsultarCAV)
      - Espera 3s y toma un screenshot
      - Guarda en resultados/<consulta_id> y registra en BD
    """
    browser = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    try:
        # 2) Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_code = re.sub(r"\s+", "_", (codigo or "").strip()) or "consulta"
        png_name = f"{NOMBRE_SITIO}_{safe_code}_{ts}.png"
        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="es-CO",
            )
            page = await ctx.new_page()

            # 3) Navegar
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # 4) Scroll hasta el formulario (el campo está más abajo)
            try:
                # Intento directo a input; si no, scroll gradual
                await page.wait_for_selector(SEL_INPUT_CODIGO, timeout=4000)
            except Exception:
                # Scroll suave hacia abajo un par de veces
                for _ in range(6):
                    await page.mouse.wheel(0, 800)
                    await asyncio.sleep(0.25)

            # 5) Completar el campo
            await page.wait_for_selector(SEL_INPUT_CODIGO, state="visible", timeout=10000)
            inp = page.locator(SEL_INPUT_CODIGO)
            await inp.click()
            try:
                await inp.fill("")  # limpiar
            except Exception:
                pass
            await inp.type(str(codigo or ""), delay=20)

            # 6) Click en Consultar
            await page.locator(SEL_BTN_CONSULTAR).click()
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_CLICK)
            except Exception:
                pass

            # 7) Esperar resultados
            await asyncio.sleep(WAIT_AFTER_CLICK / 1000)

            # 8) Centrar vista en algún bloque relevante, si existe
            try:
                target = None
                for sel in SEL_HINTS:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=1200)
                        target = page.locator(sel).first
                        break
                    except Exception:
                        continue

                if target:
                    el = await target.element_handle()
                    if el:
                        await page.evaluate(
                            """(el) => {
                                const r = el.getBoundingClientRect();
                                const y = r.top + window.scrollY - 180;
                                window.scrollTo({ top: y, behavior: 'instant' });
                            }""",
                            el,
                        )
                        await asyncio.sleep(0.2)
                else:
                    # Si no encontramos nada, al menos baja un poco más
                    for _ in range(2):
                        await page.mouse.wheel(0, 700)
                        await asyncio.sleep(0.15)
            except Exception:
                pass

            # 9) Screenshot
            await page.screenshot(path=abs_png, full_page=False)

            await ctx.close()
            await browser.close()
            browser = None

        # 10) Registro OK
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Validada",
            mensaje="",
            archivo=rel_png,
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
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
