# core/bots/colombiacompra_boletin_digital.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "colombiacompra_boletin_digital"
URL = "https://operaciones.colombiacompra.gov.co/sala-de-prensa/boletin-digital"

SEL_INPUT = "#edit-combine"  # campo de búsqueda

WAIT_AFTER_NAV = 12000
WAIT_AFTER_SEARCH = 3000


async def consultar_colombiacompra_boletin_digital(consulta_id: int, nombre: str, apellido: str):
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
        q = " ".join([nombre or "", apellido or ""]).strip() or "consulta"
        safe_q = re.sub(r"\s+", "_", q)
        png_name = f"{NOMBRE_SITIO}_{safe_q}_{ts}.png"
        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name)

        # 3) Navegación + interacción
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="es-CO")
            page = await ctx.new_page()

            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # Asegurar visibilidad del buscador
            try:
                await page.wait_for_selector(SEL_INPUT, state="visible", timeout=8000)
            except Exception:
                for _ in range(3):
                    await page.mouse.wheel(0, 600)
                    await asyncio.sleep(0.2)
                await page.wait_for_selector(SEL_INPUT, state="visible", timeout=8000)

            # Escribir "Nombre Apellido" y Enter
            inp = page.locator(SEL_INPUT)
            await inp.click()
            try:
                await inp.fill("")
            except Exception:
                pass
            await inp.type(q, delay=20)
            await inp.press("Enter")

            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_SEARCH)
            except Exception:
                pass
            await asyncio.sleep(WAIT_AFTER_SEARCH / 1000)

            # --------- DETECCIÓN DE RESULTADOS ---------
            # --------- DETECCIÓN DE RESULTADOS ---------
            html_low = (await page.content()).lower()
            query_low = q.lower()

            # Buscar coincidencias exactas en todo el DOM
            coincidencias = re.findall(rf"\b{re.escape(query_low)}\b", html_low)

            if len(coincidencias) > 1:
                hay_resultados = True
            else:
                hay_resultados = False

            # Screenshot (full page para dejar evidencia)
            await page.screenshot(path=abs_png, full_page=False)

            await ctx.close()
            await browser.close()
            browser = None

            # 4) Registrar según hallazgos
            if hay_resultados:
                mensaje = f"Se encontraron {len(coincidencias)} hallazgos de '{q}'"
                score = 10
                estado = "Validada"
            else:
                mensaje = f"No hay hallazgos para '{q}'"
                score = 0
                estado = "Validada"

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score,
            estado=estado,
            mensaje=mensaje,
            archivo=rel_png,
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin validar",
                mensaje=str(e),
                archivo="",
            )
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
