# core/bots/bicibogota.py
import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Consulta, Resultado, Fuente  # ajusta 'core' si tu app se llama distinto

url = "https://registrobicibogota.movilidadbogota.gov.co/rdbici/#/consultarEstado"
nombre_sitio = "bicibogota"

TIPO_DOC_MAP = {
    'CC': 'CC',
    'CE': 'CE',
    'NIT': 'NIT',
    'NUIP': 'NUIP',
    'PAS': 'PAS',
    'PEP': 'PEP',
}

async def consultar_bicibogota(consulta_id: int, cedula: str, tipo_doc: str):
    """
    Abre Registro Bici Bogotá, llena tipo y número, envía y toma screenshot.
    Lógica:
      - Si se muestra modal "Usuario No Existe" => mensaje, score=0.
      - De lo contrario => "Se han encontrado hallazgos", score=10.
    """
    async def _get_fuente_by_nombre(nombre: str):
        return await sync_to_async(lambda: Fuente.objects.filter(nombre=nombre).first())()

    async def _crear_resultado(estado: str, archivo: str, mensaje: str, fuente, score: float):
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente,
            estado=estado,
            mensaje=mensaje,
            archivo=archivo,
            score=score
        )

    try:
        # Validar que exista la consulta
        await sync_to_async(Consulta.objects.get)(id=consulta_id)

        fuente = await _get_fuente_by_nombre(nombre_sitio)
        tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())

        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()
            await pagina.goto(url, wait_until="domcontentloaded", timeout=120_000)

            # Esperar a que monte el formulario (SPA)
            await pagina.wait_for_timeout(1500)
            await pagina.wait_for_selector('select[name="tipoDocumento"]', timeout=30_000)

            # Completar y enviar
            await pagina.select_option('select[name="tipoDocumento"]', tipo_doc_val or "")
            await pagina.fill('input[name="numeroDocumento"]', str(cedula))
            await pagina.click('button[type="submit"]')

            # Dar tiempo a la consulta / render
            try:
                await pagina.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            await pagina.wait_for_timeout(2500)

            # Detectar modal "Usuario No Existe"
            not_found = False
            try:
                # Espera breve por el modal de SweetAlert
                await pagina.wait_for_selector(".swal-modal .swal-title", timeout=3000)
                titulo = (await pagina.locator(".swal-modal .swal-title").inner_text() or "").strip().lower()
                if "usuario no existe" in titulo:
                    not_found = True
            except Exception:
                # No apareció el modal en el tiempo dado => asumimos hallazgos
                not_found = False

            # Guardar screenshot
            relative_folder = os.path.join('resultados', str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
            absolute_path = os.path.join(absolute_folder, screenshot_name)
            relative_path = os.path.join(relative_folder, screenshot_name).replace("\\", "/")

            await pagina.screenshot(path=absolute_path, full_page=True)
            await navegador.close()

        # Mensaje / score según detección
        if not_found:
            mensaje = "Usuario No Existe"
            score = 0
        else:
            mensaje = "Se han encontrado hallazgos"
            score = 10

        await _crear_resultado("Validada", relative_path, mensaje, fuente, score)

    except Exception as e:
        try:
            fuente = await _get_fuente_by_nombre(nombre_sitio)
        except Exception:
            fuente = None
        await _crear_resultado("Sin validar", "", str(e), fuente, score=0)
