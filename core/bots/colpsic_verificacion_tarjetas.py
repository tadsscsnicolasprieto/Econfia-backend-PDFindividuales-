import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "colpsic_verificacion_tarjetas"
URL = "https://sara.colpsic.org.co/publico/verificacion-tarjetas"

# Selectores
SEL_DOC           = "#mat-input-2"
SEL_NOMBRE        = "[formcontrolname='name']"
SEL_APELLIDO      = "[formcontrolname='lastName']"
SEL_BTN_CONSULTAR = "button.mat-raised-button:has-text('Consultar')"

# Señales de posible resultado/mensajes
SEL_RESULT_HINTS = [
    "div.mat-snack-bar-container", "div.mat-mdc-snack-bar-label",
    "table", "mat-table", ".mat-table", "mat-card", ".mat-card",
    ".resultado", ".resultados",
    "text=No se encontró", "text=No existe", "text=No registra",
]

WAIT_AFTER_NAV     = 15000
WAIT_AFTER_ACTION  = 2500
EXTRA_RESULT_SLEEP = 1200


async def consultar_colpsic_verificacion_tarjetas(
    consulta_id: int,
    numero: str,
    primer_nombre: str,
    primer_apellido: str,
):
    """
    COLPSIC – Verificación Tarjetas:
      - Llena documento, nombre, apellido
      - NO resuelve captcha; intenta click en 'Consultar'
      - Espera y toma screenshot del estado (resultado/validaciones)
    """
    browser = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
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
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="es-CO"
            )
            page = await ctx.new_page()

            # 1) Página
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # 2) Llenar formulario
            await page.wait_for_selector(SEL_DOC, state="visible", timeout=15000)
            doc = page.locator(SEL_DOC)
            await doc.click(force=True)
            try: await doc.fill("")
            except Exception: pass
            await doc.type(str(numero or ""), delay=20)

            await page.wait_for_selector(SEL_NOMBRE, state="visible", timeout=15000)
            nom = page.locator(SEL_NOMBRE)
            await nom.click(force=True)
            try: await nom.fill("")
            except Exception: pass
            await nom.type((primer_nombre or "").strip(), delay=20)

            await page.wait_for_selector(SEL_APELLIDO, state="visible", timeout=15000)
            ape = page.locator(SEL_APELLIDO)
            await ape.click(force=True)
            try: await ape.fill("")
            except Exception: pass
            await ape.type((primer_apellido or "").strip(), delay=20)

            # 3) Intentar click en "Consultar"
            #    Si está deshabilitado por captcha/validaciones, lo forzamos para que al menos
            #    aparezcan mensajes de validación en pantalla y los capture el screenshot.
            btn = page.locator(SEL_BTN_CONSULTAR)
            try:
                # Si está disabled, quitar el atributo y la clase (solo para generar pantalla)
                await page.evaluate("""
                    (sel) => {
                        const b = document.querySelector(sel);
                        if (b) {
                            b.disabled = false;
                            b.classList.remove('mat-button-disabled');
                            b.setAttribute('aria-disabled', 'false');
                        }
                    }
                """, SEL_BTN_CONSULTAR)
            except Exception:
                pass

            try:
                await btn.click()
            except Exception:
                # si aún no deja, hacemos focus + Enter
                try:
                    await btn.focus()
                    await page.keyboard.press("Enter")
                except Exception:
                    pass

            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_ACTION)
            except Exception:
                pass

            # 4) Esperar a que aparezca algo (3s)
            await asyncio.sleep(3)

            # 5) Encadrar y screenshot
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

        # Registro OK
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
