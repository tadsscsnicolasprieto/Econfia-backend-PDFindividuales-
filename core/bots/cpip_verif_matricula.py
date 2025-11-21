# core/bots/cpip_verif_matricula.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "cpip_verif_matricula"
URL = "https://sits.cpip.gov.co/verifmatricula.php"

# Selectores del formulario
SEL_INPUT_DOC  = "#doc"
SEL_BTN_ENVIAR = "#iqsend"

# Pistas de resultado para centrar la vista
SEL_RESULT_HINTS = [
    "table", ".tabla", ".alert", "[role='alert']", ".panel", ".result", ".resultado"
]

WAIT_AFTER_NAV    = 15000
WAIT_AFTER_SUBMIT = 3000


async def _find_form_context(page):
    """
    Devuelve una tupla (context_type, ctx) donde:
      - context_type: "page" si el form está en el documento principal,
                      "frame" si está dentro de un frame
      - ctx: page o frame Playwright con el formulario.
    """
    # 1) Espera a que aparezca el iframe conocido o el input directamente
    try:
        await page.wait_for_selector("iframe#main_frm, " + SEL_INPUT_DOC, state="attached", timeout=15000)
    except Exception:
        # fallback: por si llega un nombre de iframe distinto
        pass

    # 2) Si el input está visible en el documento principal, úsalo
    try:
        if await page.locator(SEL_INPUT_DOC).is_visible():
            return "page", page
    except Exception:
        pass

    # 3) Probar con iframe específico
    try:
        frame = page.frame(name="main_frm") or page.frame_locator("#main_frm").frame
    except Exception:
        frame = None

    if frame:
        try:
            await frame.wait_for_selector(SEL_INPUT_DOC, state="visible", timeout=5000)
            return "frame", frame
        except Exception:
            pass

    # 4) Último recurso: buscar en cualquier frame el input #doc
    for fr in page.frames:
        try:
            if await fr.locator(SEL_INPUT_DOC).count() > 0:
                # esperamos a que esté visible
                await fr.wait_for_selector(SEL_INPUT_DOC, state="visible", timeout=5000)
                return "frame", fr
        except Exception:
            continue

    raise RuntimeError("No se encontró el formulario (#doc) ni en la página ni en iframes.")


async def consultar_cpip_verif_matricula(
    consulta_id: int,
    numero: str,  # cédula
):
    """
    CPIP – Verificación de matrícula o licencia:
      - Detecta si el form está en la página o en un iframe (p.ej. #main_frm)
      - Llena #doc con 'numero'
      - Click en #iqsend
      - Espera 3s
      - Screenshot y registro en BD
    """
    browser = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    try:
        # 2) Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_num = re.sub(r"\s+", "_", (numero or "").strip()) or "consulta"
        png_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"
        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name)

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

            # 3) Navegar
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # 4) Encontrar el contexto del formulario (página o frame)
            ctx_type, form_ctx = await _find_form_context(page)

            # 5) Llenar y enviar
            await form_ctx.locator(SEL_INPUT_DOC).fill(str(numero or ""))
            await form_ctx.locator(SEL_BTN_ENVIAR).click()

            # 6) Esperar resultados
            await asyncio.sleep(WAIT_AFTER_SUBMIT / 1000)

            # 7) Intento de centrar el scroll en resultados (sobre el documento principal)
            try:
                result_loc = None
                for sel in SEL_RESULT_HINTS:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=1200)
                        result_loc = page.locator(sel).first
                        break
                    except Exception:
                        continue

                if result_loc:
                    el = await result_loc.element_handle()
                    if el:
                        await page.evaluate(
                            """(el)=>{
                                const r = el.getBoundingClientRect();
                                const y = r.top + window.scrollY - 160;
                                window.scrollTo({top:y, behavior:'instant'});
                            }""",
                            el
                        )
                        await asyncio.sleep(0.2)
            except Exception:
                pass

            # 8) Screenshot
            await page.screenshot(path=abs_png, full_page=False)

            await ctx.close()
            await browser.close()
            browser = None

        # 9) Registro OK
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Validada",
            mensaje=f"Contexto del form: {ctx_type}",
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
