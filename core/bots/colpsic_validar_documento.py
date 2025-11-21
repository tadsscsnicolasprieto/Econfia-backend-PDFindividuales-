import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "colpsic_validar_documento"
URL = "https://sara.colpsic.org.co/publico/validar-documento"

# Selectores estables (Angular Material)
SEL_DOC      = "#mat-input-0"          # N° Documento
SEL_CODIGO   = "#mat-input-1"          # Código
SEL_BTN      = "button.mat-raised-button:has-text('Buscar')"

# Hints para encuadrar resultado/mensajes
SEL_RESULT_HINTS = [
    "div.mat-snack-bar-container", "div.mat-mdc-snack-bar-label",
    "mat-card", ".mat-card", ".resultado", ".resultados",
    "table", ".mat-table", "mat-table",
    "text=No se encontró", "text=No existe", "text=No registra", "text=Código inválido",
]

WAIT_AFTER_NAV     = 15000
WAIT_AFTER_ACTION  = 2500
EXTRA_RESULT_SLEEP = 1200


async def consultar_colpsic_validar_documento(
    consulta_id: int,
    numero: str,
    codigo: str,
):
    """
    COLPSIC – Validación de documento (QR):
      - Deja 'Formulario' por defecto
      - Llena N° Documento y Código
      - Click en 'Buscar'
      - Espera ~3s y toma screenshot centrado en el resultado o en el formulario
    """
    browser = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
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
                locale="es-CO",
            )
            page = await ctx.new_page()

            # 3) Navegar
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # 4) Llenar campos (Formulario es el tab por defecto)
            await page.wait_for_selector(SEL_DOC, state="visible", timeout=15000)
            inp_doc = page.locator(SEL_DOC)
            await inp_doc.click(force=True)
            try: await inp_doc.fill("")
            except Exception: pass
            await inp_doc.type(str(numero or ""), delay=20)

            await page.wait_for_selector(SEL_CODIGO, state="visible", timeout=15000)
            inp_cod = page.locator(SEL_CODIGO)
            await inp_cod.click(force=True)
            try: await inp_cod.fill("")
            except Exception: pass
            await inp_cod.type((codigo or "").strip(), delay=20)

            # 5) Buscar (si quedara disabled por validación, lo habilitamos para forzar mensaje)
            try:
                await page.evaluate("""
                    (sel) => {
                        const b = document.querySelector(sel);
                        if (b) {
                            b.disabled = false;
                            b.classList.remove('mat-button-disabled');
                            b.setAttribute('aria-disabled', 'false');
                        }
                    }
                """, SEL_BTN)
            except Exception:
                pass

            try:
                await page.locator(SEL_BTN).click()
            except Exception:
                try:
                    await page.locator(SEL_BTN).focus()
                    await page.keyboard.press("Enter")
                except Exception:
                    pass

            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_ACTION)
            except Exception:
                pass

            # 6) Espera a que pinte mensaje/resultado
            await asyncio.sleep(3)

            # 7) Encadrar y screenshot
            try:
                result_loc = None
                for sel in SEL_RESULT_HINTS:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=1200)
                        result_loc = page.locator(sel).first
                        break
                    except Exception:
                        continue

                center_target = result_loc or page.locator("form").first
                handle = await center_target.element_handle()
                if handle:
                    await page.evaluate(
                        """(el) => {
                            const r = el.getBoundingClientRect();
                            const y = r.top + window.scrollY - 180;
                            window.scrollTo({ top: y, behavior: 'instant' });
                        }""",
                        handle
                    )
                    await asyncio.sleep(0.2)
            except Exception:
                pass

            await page.screenshot(path=abs_png, full_page=False)

            await ctx.close()
            await browser.close()
            browser = None

        # 8) Registro OK
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
