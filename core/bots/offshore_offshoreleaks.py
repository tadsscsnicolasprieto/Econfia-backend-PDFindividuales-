# core/bots/offshore_offshoreleaks.py
import os
import re
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://offshoreleaks.icij.org/investigations/offshore-leaks"
NOMBRE_SITIO = "offshore_offshoreleaks"

async def consultar_offshore_offshoreleaks(consulta_id: int, nombre: str, cedula: str):
    """
    Abre Offshore Leaks (ICIJ), busca 'nombre' y toma hasta 3 capturas.
    Reintenta hasta 3 veces antes de guardar error en la BD.
    Siempre guarda pantallazo (éxito o error).
    """
    # Carpeta resultados/<consulta_id>
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = re.sub(r"\s+", "_", (nombre or "consulta").strip()) or "consulta"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)

    for intento in range(1, 4):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    locale="es-ES"
                )
                page = await context.new_page()

                # 1) Ir al sitio
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                # 2) Popup / consentimiento
                try:
                    await page.locator("input#accept").check(timeout=3000)
                    await page.locator("button.btn.btn-primary.btn-block.btn-lg").click(timeout=3000)
                    await asyncio.sleep(1.2)
                except Exception:
                    for sel in [
                        "button:has-text('I accept')",
                        "button:has-text('Accept all')",
                        "#onetrust-accept-btn-handler",
                    ]:
                        try:
                            await page.locator(sel).first.click(timeout=1200)
                            break
                        except Exception:
                            pass

                # 3) Buscar el nombre
                await page.wait_for_selector('input[name="q"]', timeout=15000)
                await page.fill('input[name="q"]', nombre or "")
                await page.keyboard.press("Enter")

                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await asyncio.sleep(2)

                # 4) Tomar hasta 3 capturas
                rel_paths = []
                for i in range(1, 4):
                    png_name = f"{NOMBRE_SITIO}_{cedula}_{ts}_page{i}.png"
                    absolute_path = os.path.join(absolute_folder, png_name)
                    relative_path = os.path.join(relative_folder, png_name)

                    await page.screenshot(path=absolute_path, full_page=True)
                    rel_paths.append(relative_path)

                    next_button = page.locator('a.page-link[aria-label="Next »"]')
                    try:
                        if await next_button.count() and await next_button.is_enabled():
                            await next_button.click()
                            try:
                                await page.wait_for_load_state("networkidle", timeout=15000)
                            except Exception:
                                pass
                            await asyncio.sleep(2)
                        else:
                            break
                    except Exception:
                        break

                await browser.close()

            # ✅ Si llegó hasta aquí, guardamos resultado y salimos del bucle
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Validado",
                mensaje="No se encontraron hallazgos",
                archivo=",".join(rel_paths)
            )
            return

        except Exception as e:
            # Si falló en este intento → pantallazo del error
            error_png = f"{NOMBRE_SITIO}_{cedula}_{ts}_error_intento{intento}.png"
            error_abs = os.path.join(absolute_folder, error_png)
            error_rel = os.path.join(relative_folder, error_png)

            try:
                if 'page' in locals():
                    await page.screenshot(path=error_abs, full_page=True)
            except Exception:
                error_rel = ""

            # Si es el tercer intento → guardar error en BD
            if intento == 3:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado ="Sin validar",
                    mensaje="Ocurrió un problema al obtener la información de la fuente",
                    archivo=error_rel
                )
