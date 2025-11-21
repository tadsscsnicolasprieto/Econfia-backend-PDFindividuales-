# core/bots/colelectro_directorio.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "colelectro_directorio"
URL = "https://www.colelectrofisiologia.com/directorio-medico/"

WAIT_AFTER_NAV     = 15000
EXTRA_SLEEP        = 1200


async def consultar_colelectro_directorio(
    consulta_id: int,
    nombre: str,     # p.e. "William José"
    apellido: str,   # p.e. "Benítez Pinto"
):
    """
    Colegio Colombiano de Electrofisiología – Directorio Médico:
    - Abre el directorio.
    - Busca por texto (nombre + apellido) de forma robusta.
    - Si encuentra, desplaza al primer match, lo resalta y hace screenshot.
    - Si no encuentra, screenshot de toda la página.
    - Guarda archivo y registra en BD.
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
        base_tag = re.sub(r"\s+", "_", f"{nombre}_{apellido}".strip()) or "busqueda"
        png_name = f"{NOMBRE_SITIO}_{base_tag}_{ts}.png"
        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name)

        buscado = " ".join(x for x in [nombre or "", apellido or ""] if x).strip()
        buscado_simple = re.sub(r"\s+", " ", buscado)
        apellido_simple = (apellido or "").strip()
        nombre_simple = (nombre or "").strip()

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="es-CO"
            )
            page = await ctx.new_page()

            # 3) Ir al sitio
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # 4) (opcional) aceptar cookies si aparece
            for sel in [
                "button:has-text('Aceptar')",
                "button:has-text('Accept')",
                "button#onetrust-accept-btn-handler",
                ".cky-btn-accept",
            ]:
                try:
                    btn = page.locator(sel)
                    if await btn.count() > 0:
                        await btn.first.click()
                        await asyncio.sleep(0.3)
                        break
                except Exception:
                    pass

            # 5) Intentar localizar por texto (apellido + nombre)
            #    Preferimos coincidir por apellido y filtrar con nombre (más flexible)
            found_locator = None
            try:
                if apellido_simple:
                    cand = page.locator(f"text={apellido_simple}")
                    if nombre_simple:
                        cand = cand.filter(has_text=re.compile(re.escape(nombre_simple), re.I))
                    if await cand.count() > 0:
                        found_locator = cand.first
                # fallback: buscar por el string completo
                if not found_locator and buscado_simple:
                    cand2 = page.locator(f"text={buscado_simple}")
                    if await cand2.count() > 0:
                        found_locator = cand2.first
                # fallback 2: buscar por nombre a secas
                if not found_locator and nombre_simple:
                    cand3 = page.locator(f"text={nombre_simple}")
                    if await cand3.count() > 0:
                        found_locator = cand3.first
            except Exception:
                pass

            mensaje_res = ""
            if found_locator:
                # Desplazar y resaltar el resultado
                try:
                    await found_locator.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                try:
                    await found_locator.evaluate(
                        """(el) => {
                            el.style.outline = '4px solid #ffcc00';
                            el.style.backgroundColor = 'rgba(255, 255, 0, 0.15)';
                        }"""
                    )
                except Exception:
                    pass

                await asyncio.sleep(EXTRA_SLEEP / 1000)
                await page.screenshot(path=abs_png, full_page=False)
                mensaje_res = f"Resultado encontrado para: '{buscado_simple or (nombre_simple or '') + ' ' + (apellido_simple or '')}'."
            else:
                # No se encontró: screenshot completo para evidencia
                await asyncio.sleep(0.5)
                await page.screenshot(path=abs_png, full_page=True)
                mensaje_res = f"No se encontró coincidencia para: '{buscado_simple or (nombre_simple or '') + ' ' + (apellido_simple or '')}'."

            await ctx.close()
            await browser.close()
            browser = None

        # 6) Registro OK en BD
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Validada",
            mensaje=mensaje_res,
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
