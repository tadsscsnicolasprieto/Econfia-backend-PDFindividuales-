# bots/repet.py
import os, re, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "repet"  # agrega/asegura esta Fuente en tu tabla
URL_HOME = "https://repet.jus.gob.ar/"
GOTO_TIMEOUT_MS = 180_000

# Selectores
SEL_INPUT_Q     = "input.quicksearch#edit-keys[name='keys'][placeholder*='Ingresá el nombre a buscar']"
SEL_NORES_P     = "p.lead"  # Validaremos texto exacto
SEL_RESULT_ITEM = "div.alert.alert-warning"

def _norm(s: str) -> str:
    """Normaliza para comparación exacta: lower, sin tildes, espacios comprimidos."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_repet(consulta_id: int, nombre: str, apellido: str):
    navegador = None
    full_name = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=1,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    if not full_name:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=1,
            estado="Sin Validar",
            mensaje="Nombre y/o apellido vacíos para la consulta.",
            archivo=""
        )
        return

    # 2) Carpeta / archivo
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^\w\.-]+", "_", full_name)
    png_name = f"{NOMBRE_SITIO}_{safe_name}_{ts}.png"
    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    # 3) Defaults
    mensaje_final = "No hay resultados para su búsqueda de Personas."
    score_final = 1
    success = False
    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="es-AR",
                timezone_id="America/Bogota",
            )
            page = await context.new_page()

            # 4) Ir a la home
            await page.goto(URL_HOME, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)

            # 5) Escribir el nombre completo en el buscador (actualiza en tiempo real)
            input_q = page.locator(SEL_INPUT_Q)
            await input_q.fill("")  # limpia por si acaso
            await input_q.type(full_name, delay=50)  # tipeo humano

            # 6) Esperar a que se estabilice la búsqueda
            # Damos chances a que aparezca "sin resultados" o resultados
            nores_loc = page.locator(SEL_NORES_P)
            items_loc = page.locator(SEL_RESULT_ITEM)

            # Esperas condicionales (hasta 10s): o aparece el <p.lead> "No hay resultados..." o aparecen items
            try:
                await page.wait_for_function(
                    """(noresSel, itemsSel) => {
                        const nores = document.querySelector(noresSel);
                        const hasNores = nores && nores.textContent.trim().includes('No hay resultados para su búsqueda de Personas.');
                        const items = document.querySelectorAll(itemsSel);
                        return hasNores || (items && items.length > 0);
                    }""",
                    arg=(SEL_NORES_P, SEL_RESULT_ITEM),
                    timeout=10_000
                )
            except Exception:
                # si no se cumple, seguimos igual y evaluamos abajo
                pass

            # 7) Evaluar estado de resultados
            # Caso A: no hay resultados (mensaje p.lead exacto)
            nores_text = ""
            try:
                if await nores_loc.count() > 0:
                    nores_text = (await nores_loc.first.inner_text(timeout=2_000)).strip()
            except Exception:
                nores_text = ""

            if "No hay resultados para su búsqueda de Personas." in nores_text:
                mensaje_final = "No hay resultados para su búsqueda de Personas."
                success = True

            else:
                # Caso B: hay resultados en alert-warning
                n = await items_loc.count()
                if n == 0:
                    # Ni p.lead ni items -> lo tomamos como consulta válida sin hallazgos visibles
                    mensaje_final = "No hay resultados para su búsqueda de Personas."
                    success = True
                else:
                    exact_hit = False
                    for i in range(n):
                        try:
                            txt = (await items_loc.nth(i).inner_text(timeout=3_000)).strip()
                        except Exception:
                            txt = ""
                        if txt and _norm(txt) == norm_query:
                            exact_hit = True
                            break

                    if exact_hit:
                        score_final = 5
                        mensaje_final = f"Coincidencia exacta con el nombre buscado: '{full_name}'."
                    else:
                        score_final = 1
                        mensaje_final = "Se encontraron resultados, pero sin coincidencia exacta del nombre."

                    success = True

            # 8) Screenshot
            try:
                await page.screenshot(path=absolute_png, full_page=True)
            except Exception:
                pass

            # 9) Cerrar navegador
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 10) Guardar Resultado
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj,
            score=score_final,
            estado="Validada" if success else "Sin Validar",
            mensaje=mensaje_final,
            archivo=relative_png if success else ""
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1, estado="Sin Validar",
                mensaje=str(e), archivo=""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
