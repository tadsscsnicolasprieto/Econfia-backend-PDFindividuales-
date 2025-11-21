import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "ramajudicial_corte_constitucional_magistrados"
URL = "https://www.ramajudicial.gov.co/web/corte-constitucional/portal/corporacion/magistrados/magistrados-actuales"

SEL_INPUT = "#barra_busqueda"
RESULT_HINTS = ["main", "#content", ".portlet-body", "article", ".layout-content", ".searchcontainer"]

WAIT_NAV   = 15000
WAIT_POST  = 2000

async def consultar_ramajudicial_corte_constitucional_magistrados(
    consulta_id: int,
    nombre: str,
    apellido: str = "",
):
    """
    Intenta hasta 3 veces consultar magistrados.
    Guarda pantallazo en caso de éxito o error.
    Estados válidos: 'Validado' | 'Sin validar'
    """
    # 1) Obtener fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo="",
        )
        return

    # 2) Preparar carpeta
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    query = (" ".join([(nombre or "").strip(), (apellido or "").strip()])).strip() or "consulta"
    safe_q = re.sub(r"\s+", "_", query)

    # 3) Intentar hasta 3 veces
    for intento in range(1, 4):
        browser = None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        png_name = f"{NOMBRE_SITIO}_{safe_q}_{ts}_try{intento}.png"
        abs_png  = os.path.join(absolute_folder, png_name)
        rel_png  = os.path.join(relative_folder, png_name)

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled", "--start-maximized"]
                )
                ctx = await browser.new_context(viewport=None, locale="es-CO")
                page = await ctx.new_page()

                # Navegar
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=WAIT_NAV)
                except Exception:
                    pass

                # Intentar cerrar banners
                try:
                    for sel in [
                        "button:has-text('Aceptar')",
                        ".eu-cookie-compliance-default-button",
                        "[data-ebox-cmd='close']",
                        "button.close, .close[aria-label='Close']",
                    ]:
                        btns = page.locator(sel)
                        for i in range(await btns.count()):
                            b = btns.nth(i)
                            if await b.is_visible():
                                try:
                                    await b.click(timeout=1000)
                                except Exception:
                                    pass
                except Exception:
                    pass

                # Buscar
                await page.wait_for_selector(SEL_INPUT, state="visible", timeout=20000)
                inp = page.locator(SEL_INPUT).first
                await inp.click()
                try:
                    await inp.fill("")
                except Exception:
                    pass
                await inp.type(query, delay=25)
                await inp.press("Enter")

                # Esperar resultados
                try:
                    await page.wait_for_load_state("networkidle", timeout=WAIT_NAV)
                except Exception:
                    pass
                await asyncio.sleep(WAIT_POST / 1000)

                # Centrar resultados
                try:
                    focus = None
                    for sel in RESULT_HINTS:
                        try:
                            await page.wait_for_selector(sel, state="visible", timeout=1200)
                            focus = page.locator(sel).first
                            break
                        except Exception:
                            continue
                    if focus:
                        el = await focus.element_handle()
                        if el:
                            await page.evaluate(
                                "(el)=>{const r=el.getBoundingClientRect();window.scrollTo({top:r.top+window.scrollY-120,behavior:'instant'});}",
                                el
                            )
                            await asyncio.sleep(0.2)
                except Exception:
                    pass

                # Extraer mensaje de resultados
                try:
                    container = page.locator(".portlet-content-container .portlet-body").first
                    text_container = await container.inner_text()

                    if "No se ha encontrado ningún resultado" in text_container:
                        score = 0
                        mensaje = text_container.strip()
                    else:
                        try:
                            results_text = await page.locator("p.search-total-label").inner_text()
                            score = 10
                            mensaje = results_text.strip()
                        except Exception:
                            score = 0
                            mensaje = "No fue posible determinar los resultados"
                except Exception:
                    score = 0
                    mensaje = "No fue posible obtener el contenedor de resultados"

                # Screenshot completo
                await page.screenshot(path=abs_png, full_page=True)

                await ctx.close()
                await browser.close()
                browser = None

            # Guardar resultado
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validado",
                mensaje=mensaje,
                archivo=rel_png,
            )
            return

        except Exception as e:
            # Screenshot de error
            if browser:
                try:
                    page = (await browser.contexts())[0].pages[0]
                    await page.screenshot(path=abs_png, full_page=True)
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass

            # Si aún no es el último intento, reintentar
            if intento < 3:
                await asyncio.sleep(2)
                continue

            # Ya es el tercer intento, registrar error
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin validar",
                mensaje="Ocurrió un problema al obtener la información de la fuente",
                archivo=rel_png if os.path.exists(abs_png) else "",
            )
            return
