import os
import asyncio
from datetime import datetime
from django.conf import settings
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente

nombre_sitio = "garantias_mobiliarias_oficial"

UA_DESKTOP_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


async def _goto_resiliente(page, url, intentos=4, base_sleep=1.5):
    ultimo_err = None
    for i in range(1, intentos + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return
        except Exception as e:
            ultimo_err = e
            await asyncio.sleep(base_sleep * (2 ** (i - 1)) + 0.5)
    if ultimo_err:
        raise ultimo_err


async def consultar_garantias_mobiliarias_oficial(consulta_id, cedula):
    target_url = (
        f"https://www.garantiasmobiliarias.com.co/rgm/Garantias/"
        f"ConsultaGarantia.aspx?NumeroIdentificacion={cedula}&ConsultaOficial=true"
    )

    navegador = None
    fuente_obj = None

    # Buscar fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{nombre_sitio}': {e}", archivo=""
        )
        return

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            contexto = await navegador.new_context(
                user_agent=UA_DESKTOP_CHROME,
                locale="es-CO",
                viewport={"width": 1366, "height": 768},
            )
            pagina = await contexto.new_page()

            # Navegar con reintentos
            await _goto_resiliente(pagina, target_url)
            await asyncio.sleep(2)

            # ===== DETECCIÓN DEL SPAN =====
            score = 6
            mensaje = "Se encontraron varios resultados."

            try:
                span_loc = pagina.locator(
                    "#ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_lblMensajeBusqueda"
                )
                if await span_loc.count() > 0 and await span_loc.is_visible():
                    texto_span = (await span_loc.inner_text() or "").strip()
                    if texto_span:
                        score = 0
                        mensaje = texto_span
            except Exception:
                # Si falla la detección, mantenemos score=6
                pass

            # Captura de pantalla completa del viewport
            relative_folder = os.path.join("resultados", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
            absolute_path = os.path.join(absolute_folder, screenshot_name)
            relative_path = os.path.join(relative_folder, screenshot_name)

            await pagina.screenshot(path=absolute_path, full_page=True)

            # Registrar resultado
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validada",
                mensaje=mensaje,
                archivo=relative_path
            )

            await navegador.close()
            navegador = None

    except (PWTimeoutError, Exception) as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=f"No fue posible obtener resultados: {e}",
                archivo=""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
