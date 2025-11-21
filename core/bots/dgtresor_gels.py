# bots/dgtresor_gels.py
import os, re, asyncio, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "dgtresor_gels_avoirs"
URL = "https://gels-avoirs.dgtresor.gouv.fr/List"
GOTO_TIMEOUT_MS = 180_000

# Selectores
SEL_INPUT       = "#QueryNomPrenomAlias"
SEL_NORES       = "h4.text-center.ml-4"  # "Aucun registre correspondant à la recherche"
SEL_TABLE       = "#tableGels"
SEL_TABLE_ROWS  = "#tableGels tbody tr"
SEL_FIRST_CELL  = "td:nth-child(1)"      # suele contener el nombre

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_dgtresor_gels(consulta_id: int, nombre: str, apellido: str):
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

    mensaje_final = "No hay coincidencias."
    success = False
    last_error = None
    score_final = 1
    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="fr-FR",
                timezone_id="Europe/Paris",
            )
            page = await context.new_page()

            # 3) Ir a la página y ejecutar búsqueda
            await page.goto(URL, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)

            await page.fill(SEL_INPUT, full_name)
            # simular Enter
            await page.press(SEL_INPUT, "Enter")

            # Esperar a que aparezca alguno de los resultados (mensaje o tabla)
            # Primero intentamos detectar el mensaje de "Aucun registre..."
            nores_loc = page.locator(SEL_NORES)
            table_loc = page.locator(SEL_TABLE)

            # Damos tiempo a que renderice uno u otro
            try:
                await page.wait_for_function(
                    """([selNo, selTbl]) => !!document.querySelector(selNo) || !!document.querySelector(selTbl)""",
                    arg=[SEL_NORES, SEL_TABLE],
                    timeout=60_000
                )
            except Exception:
                pass

            # 4) Caso: No hay registros
            if await nores_loc.count() > 0:
                try:
                    txt = (await nores_loc.first.inner_text()).strip()
                    # Confirmamos el texto esperado:
                    if "Aucun registre" in txt:
                        mensaje_final = txt
                    else:
                        mensaje_final = "Aucun registre correspondant à la recherche"
                except Exception:
                    mensaje_final = "Aucun registre correspondant à la recherche"

                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True  # consulta válida, sin hallazgos (score 1)

            # 5) Caso: Hay tabla de resultados
            elif await table_loc.count() > 0:
                rows = page.locator(SEL_TABLE_ROWS)
                n = await rows.count()
                exact_hit = False

                for i in range(n):
                    row = rows.nth(i)
                    # Preferimos primera celda; si falla, usamos todo el texto de la fila
                    try:
                        title = (await row.locator(SEL_FIRST_CELL).first.inner_text(timeout=2_000)).strip()
                    except Exception:
                        try:
                            title = (await row.inner_text(timeout=2_000)).strip()
                        except Exception:
                            title = ""

                    if title and _norm(title) == norm_query:
                        exact_hit = True
                        break

                if exact_hit:
                    score_final = 5
                    mensaje_final = f"Coincidencia exacta con el nombre buscado: '{full_name}'."
                else:
                    score_final = 1
                    mensaje_final = "Se encontraron resultados, pero sin coincidencia exacta del nombre."

                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True

            else:
                # Ni mensaje ni tabla visibles
                mensaje_final = "No fue posible determinar resultados (no se encontró mensaje ni tabla)."

            # 6) Cerrar navegador
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 7) Guardar resultado
        if success:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=score_final,
                estado="Validada",
                mensaje=mensaje_final,
                archivo=relative_png
            )
        else:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1,
                estado="Sin Validar",
                mensaje=last_error or "No fue posible obtener resultados.",
                archivo=relative_png
            )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1,
                estado="Sin Validar",
                mensaje=str(e),
                archivo=""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
