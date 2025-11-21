# core/bots/mintransporte_capacitaciones.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from core.models import Resultado, Fuente

NOMBRE_SITIO = "mintransporte_capacitaciones"
URL = "https://web.mintransporte.gov.co/sisconmp2/consultascapacitaciones/"

# Selectores
SEL_SELECT_NATIVO     = "#cboTDI"                 # select real (si existe)
SEL_SELECT2_TRIGGER   = ".select2-selection--single"
SEL_SELECT2_RESULTS   = ".select2-results__option"

SEL_INPUT_NUMERO      = "#txtNDI"
SEL_BTN_CONSULTAR     = "#btnConsultarMD"

# Pistas de resultado (para hacer scroll cerca del resultado)
SEL_RESULT_HINTS = [
    "#divInfo", "#divDetalle", "#divDatosBasicos",
    ".dataTables_wrapper", "table", ".panel", ".well", ".box", ".resultado", ".alert-success"
]

# Bloque de “no hay registros”
SEL_NO_ROOT = "#divNoHayRegistros"
SEL_NO_MSG  = "#divNoHayRegistros div[style*='color: red'][style*='font-weight: bold']"

# Tiempos
NAV_TIMEOUT_MS       = 120000
WAIT_AFTER_NAV_MS    = 1500
WAIT_UI_MS           = 600
EXTRA_RESULT_SLEEP   = 2000

# Mapeo de tipo_doc interno → etiqueta visible
DOC_TYPE_MAP = {"CC": "CC", "CE": "CE", "TI": "TI", "PA": "PA", "NIT": "NIT"}

async def _registrar(consulta_id, fuente, estado, mensaje, archivo, score: int = 0):
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente,
        score=score,
        estado=estado,
        mensaje=mensaje,
        archivo=archivo,
    )

async def consultar_mintransporte_capacitaciones(
    consulta_id: int,
    tipo_doc: str,   # "CC", "CE", "TI", "PA", "NIT"
    numero: str,
):
    browser = None

    # 1) Fuente
    try:
        fuente = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await _registrar(consulta_id, None, "Sin Validar",
                         f"No se encontró Fuente '{NOMBRE_SITIO}': {e}", "", score=0)
        return

    try:
        # 2) Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_num = re.sub(r"\s+", "_", (numero or "").strip()) or "doc"
        shot_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"
        abs_png   = os.path.join(absolute_folder, shot_name)
        rel_png   = os.path.join(relative_folder, shot_name).replace("\\", "/")

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

            # 3) Ir a la página
            await page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(WAIT_AFTER_NAV_MS)

            # 4) Seleccionar tipo de documento
            etiqueta = DOC_TYPE_MAP.get((tipo_doc or "").upper(), "CC")

            # Intento 1: select nativo
            try:
                await page.select_option(SEL_SELECT_NATIVO, label=etiqueta)
            except Exception:
                # Intento 2: Select2
                try:
                    trig = page.locator(SEL_SELECT2_TRIGGER).first
                    await trig.click()
                    await page.wait_for_timeout(WAIT_UI_MS)
                    await page.locator(f"{SEL_SELECT2_RESULTS} >> text='{etiqueta}'").first.click()
                except Exception:
                    pass

            await page.wait_for_timeout(WAIT_UI_MS)

            # 5) Digitar número
            await page.wait_for_selector(SEL_INPUT_NUMERO, timeout=15000)
            await page.fill(SEL_INPUT_NUMERO, "")
            await page.type(SEL_INPUT_NUMERO, str(numero), delay=20)

            # 6) Consultar
            await page.locator(SEL_BTN_CONSULTAR).click()
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass

            # 7) Esperar que aparezca algo de UI cercana al resultado (para centrar scroll)
            try:
                found = None
                for sel in SEL_RESULT_HINTS:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=1500)
                        found = page.locator(sel).first
                        break
                    except Exception:
                        continue

                if found:
                    el = await found.element_handle()
                    if el:
                        await page.evaluate(
                            """(el) => {
                                const r = el.getBoundingClientRect();
                                const y = r.top + window.scrollY - 140;
                                window.scrollTo({ top: y, behavior: 'instant' });
                            }""",
                            el
                        )
                await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)
            except Exception:
                pass

            # ===== LÓGICA PEDIDA =====
            score_final = 1  # Siempre 1
            mensaje_final = "Se encontrarón registros sobre el ciudadano."

            try:
                no_root = page.locator(SEL_NO_ROOT).first
                if await no_root.count() > 0 and await no_root.is_visible():
                    # Intentar leer el div rojo interior; si falla, usar literal solicitado
                    try:
                        red_msg = (await page.locator(SEL_NO_MSG).first.inner_text()).strip()
                        mensaje_final = red_msg or "No se encontrarón registros sobre el ciudadano."
                    except Exception:
                        mensaje_final = "No se encontrarón registros sobre el ciudadano."
                else:
                    # Si no existe el bloque de "no hay registros", asumimos que SÍ hay registros
                    mensaje_final = "Se encontrarón registros sobre el ciudadano."
            except Exception:
                # Ante cualquier problema, mantenemos el default (registros encontrados)
                mensaje_final = "Se encontrarón registros sobre el ciudadano."

            # 8) Screenshot (evidencia)
            await page.screenshot(path=abs_png, full_page=True)

            await ctx.close()
            await browser.close()
            browser = None

        # 9) Registrar
        await _registrar(
            consulta_id, fuente, "Validada",
            mensaje_final,
            rel_png,
            score=score_final
        )

    except Exception as e:
        try:
            await _registrar(consulta_id, fuente, "Sin Validar", str(e), "", score=0)
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass