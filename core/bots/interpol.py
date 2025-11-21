# bots/interpol.py
import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente

INTERPOL_URL = "https://www.interpol.int/es/Como-trabajamos/Notificaciones/Notificaciones-rojas/Ver-las-notificaciones-rojas"
NOMBRE_SITIO = "interpol"

async def consultar_interpol(nombre: str, apellido: str, cedula: str, consulta_id: int):
    """
    Busca en INTERPOL Red Notices por nombre/apellido y:
      - Si NO hay resultados: score=0, mensaje="No hay resultados para su búsqueda. Seleccione otros criterios."
      - Si hay resultados:     score=10, mensaje="<contador>", p. ej. "14 results for luis"
    Siempre guarda un pantallazo y registra en BD.
    """
    navegador = None
    contexto = None

    # Buscar la fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    # Carpeta resultados/<consulta_id>
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_name = f"interpol_{cedula}_{ts}.png"
    absolute_path = os.path.join(absolute_folder, screenshot_name)
    relative_path = os.path.join(relative_folder, screenshot_name)

    # Defaults
    score_final = 0
    mensaje_final = "No hay resultados para su búsqueda. Seleccione otros criterios."

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            contexto = await navegador.new_context(viewport={"width": 1400, "height": 900}, locale="es-ES")
            page = await contexto.new_page()

            # 1) Cargar página
            await page.goto(INTERPOL_URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Aceptar cookies (best-effort)
            for sel in [
                "button#onetrust-accept-btn-handler",
                "button:has-text('Aceptar')",
                "button:has-text('Accept')",
            ]:
                try:
                    await page.locator(sel).first.click(timeout=1200)
                    break
                except Exception:
                    pass

            # 2) Rellenar filtros
            try:
                await page.locator("#forename").fill((nombre or "").strip())
            except Exception:
                pass
            try:
                await page.locator("#name").fill((apellido or "").strip())
            except Exception:
                pass

            # 3) Enviar búsqueda
            try:
                await page.locator("#submit").click(timeout=4000)
            except Exception:
                try:
                    await page.locator("#name").press("Enter")
                except Exception:
                    pass

            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            # 4) Leer mensaje/contador en p.lightText dentro del bloque de resultados
            results_block = page.locator(".search__resultsBlock--results.js-gallery").first
            texto = ""
            try:
                p_light = results_block.locator("p.lightText").first
                if await p_light.count() > 0 and await p_light.is_visible():
                    texto = (await p_light.inner_text() or "").strip()
            except Exception:
                texto = ""

            if texto:
                if "No hay resultados" in texto or "No results" in texto:
                    score_final = 0
                    mensaje_final = "No hay resultados para su búsqueda. Seleccione otros criterios."
                else:
                    score_final = 10
                    mensaje_final = texto  # ej: "14 results for luis"

            # 5) Pantallazo (del bloque si existe, si no, de toda la página)
            try:
                if await results_block.count() > 0 and await results_block.is_visible():
                    await results_block.screenshot(path=absolute_path)
                else:
                    await page.screenshot(path=absolute_path, full_page=True)
            except Exception:
                await page.screenshot(path=absolute_path, full_page=True)

            await contexto.close()
            await navegador.close()
            navegador = None
            contexto = None

        # 6) Registrar en BD (OK)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,  # texto plano
            archivo=relative_path,
        )

    except Exception as e:
        # Cierre defensivo
        try:
            if contexto is not None:
                await contexto.close()
        except Exception:
            pass
        try:
            if navegador is not None:
                await navegador.close()
        except Exception:
            pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje=str(e),
            archivo="",
        )
