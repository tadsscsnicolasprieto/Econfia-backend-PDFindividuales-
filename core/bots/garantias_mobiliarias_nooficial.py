import os
import re
import asyncio
from datetime import datetime
from urllib.parse import quote_plus
from django.conf import settings
from playwright.async_api import async_playwright
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente

nombre_sitio = "garantias_mobiliarias_nooficial"

NEG_PATTERNS = [
    "No se han encontrado resultados",
    "No existen resultados",
    "No se encontró",
    "No hay resultados",
    "No results",
]

def _safe_filename(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

async def consultar_garantias_mobiliarias_nooficial(consulta_id, nombre, nro_bien):
    q_nombre = quote_plus(nombre or "")
    url = f"https://www.garantiasmobiliarias.com.co/rgm/Garantias/ConsultaGarantia.aspx?NombreDeudor={q_nombre}&ConsultaOficial=false"

    navegador = None
    fuente_obj = None

    # Buscar la fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{nombre_sitio}': {e}",
            archivo=""
        )
        return

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            page = await navegador.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=120000)
            await asyncio.sleep(5)  # espera para que cargue la página

            # Carpetas para screenshots
            relative_folder = os.path.join("resultados", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_nom = _safe_filename(nombre)
            screenshot_name = f"{nombre_sitio}_{safe_nom}_{timestamp}.png"
            absolute_path = os.path.join(absolute_folder, screenshot_name)
            relative_path = os.path.join(relative_folder, screenshot_name)

            # ===== DETECCIÓN DEL SPAN ESPECÍFICO =====
            score = 6
            mensaje = "Se encontraron varios resultados."

            try:
                span_loc = page.locator(
                    "#ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_lblMensajeBusqueda"
                )
                if await span_loc.count() > 0 and await span_loc.is_visible():
                    texto_span = (await span_loc.inner_text() or "").strip()
                    if texto_span:
                        score = 0
                        mensaje = texto_span
            except Exception:
                # Si falla la detección, se mantiene score=6
                pass

            # Captura completa de la página
            await page.screenshot(path=absolute_path, full_page=True)

            await navegador.close()
            navegador = None

            # Registrar resultado
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validada",
                mensaje=mensaje,
                archivo=relative_path
            )

    except Exception as e:
        # Inicializar rutas para evitar errores si ocurre excepción antes de su definición
        absolute_path = locals().get('absolute_path', 'error_screenshot.png')
        relative_path = locals().get('relative_path', '')
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje="No fue posible obtener resultados (todas las URLs fallaron).",
                archivo=relative_path
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
