import os
from datetime import datetime, date
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente  # Ajusta según tu app

url = "https://srvcnpc.policia.gov.co/PSC/frm_cnp_consulta.aspx"
nombre_sitio = "rnmc"

TIPO_DOC_MAP = {
    "CC": "55",
    "CE": "57",
    "NIT": "1",
    "NUIP": "0",
    "PAS": "58",
    "PEP": "848"
}

# ---- helper para formatear fecha ----
def _formatear_fecha_ddmmaaaa(fecha_expedicion) -> str:
    """
    Acepta date/datetime o str en varios formatos y retorna 'DD/MM/YYYY'.
    """
    if isinstance(fecha_expedicion, (datetime, date)):
        return fecha_expedicion.strftime("%d/%m/%Y")

    s = (fecha_expedicion or "").strip()
    if not s:
        raise ValueError("fecha_expedicion vacía")

    # Si ya está en DD/MM/YYYY, la devolvemos
    try:
        datetime.strptime(s, "%d/%m/%Y")
        return s
    except Exception:
        pass

    # Intentos comunes
    formatos = [
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d%m%Y",
        "%Y%m%d",
    ]
    for fmt in formatos:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%d/%m/%Y")
        except Exception:
            continue

    # Último intento: dividir por no-dígitos y deducir posiciones
    import re
    nums = [p for p in re.split(r"\D+", s) if p]
    if len(nums) == 3:
        # heurística: si el primero tiene 4 dígitos => YYYY M D; si el tercero => D M YYYY
        if len(nums[0]) == 4:  # YYYY M D
            y, m, d = nums
        elif len(nums[2]) == 4:  # D M YYYY
            d, m, y = nums
        else:
            # asumir D M Y por defecto
            d, m, y = nums
        try:
            dt = datetime(int(y), int(m), int(d))
            return dt.strftime("%d/%m/%Y")
        except Exception:
            pass

    # Si nada funcionó, lanza error claro
    raise ValueError(f"Formato de fecha inválido: {s}. Usa DD/MM/YYYY, YYYY-MM-DD, etc.")

import asyncio
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

async def consultar_rnmc(consulta_id, cedula, tipo_doc, fecha_expedicion):
    MAX_INTENTOS = 3

    # Normalizar fecha a 'DD/MM/YYYY'
    fecha_expedicion_str = _formatear_fecha_ddmmaaaa(fecha_expedicion)

    # Carpetas
    relative_folder = os.path.join('resultados', str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin validar",
            mensaje=f"No se encontró la Fuente '{nombre_sitio}': {e}",
            archivo="",
        )
        return

    for intento in range(1, MAX_INTENTOS + 1):
        navegador = None
        pagina = None
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            async with async_playwright() as p:
                tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())
                if not tipo_doc_val:
                    raise ValueError(f"Tipo de documento no válido: {tipo_doc}")

                navegador = await p.chromium.launch(headless=True)
                pagina = await navegador.new_page()
                await pagina.goto(url, wait_until="domcontentloaded", timeout=90000)
                try:
                    await pagina.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass

                # Seleccionar tipo de documento
                await pagina.select_option('select[id="ctl00_ContentPlaceHolder3_ddlTipoDoc"]', tipo_doc_val)
                try:
                    await pagina.wait_for_load_state('networkidle', timeout=5000)
                except Exception:
                    pass
                await pagina.wait_for_timeout(500)

                # Llenar campos
                await pagina.fill('input[id="ctl00_ContentPlaceHolder3_txtExpediente"]', str(cedula))
                await pagina.fill('input[id="txtFechaexp"]', fecha_expedicion_str)

                # Consultar
                await pagina.click('#ctl00_ContentPlaceHolder3_btnConsultar2')

                # --- Esperas inteligentes ---
                RESP_SEL   = '#ctl00_ContentPlaceHolder3_respuesta'
                MODAL_ERR  = "div.modal-body .alert.alert-danger"
                MODAL_TXT  = "div.modal-body #ctl00_ContentPlaceHolder3_lblcontenidomodal"
                ALERT_PROC = "div.alert.alert-warning:has-text('El sistema esta procesando su solicitud')"

                # 1) Si sale el modal de error de fecha -> score=0, Offline
                modal_err_visible = False
                try:
                    await pagina.wait_for_selector(MODAL_ERR, timeout=5000)
                    modal_err_visible = True
                except PWTimeoutError:
                    modal_err_visible = False

                if modal_err_visible:
                    # Screenshot y guardar
                    screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}_modal_error.png"
                    absolute_path = os.path.join(absolute_folder, screenshot_name)
                    relative_path = os.path.join(relative_folder, screenshot_name).replace("\\", "/")
                    try:
                        await pagina.screenshot(path=absolute_path, full_page=True)
                    except Exception:
                        pass

                    # Extra: leer texto del modal si existe
                    try:
                        modal_text = (await pagina.locator(MODAL_TXT).inner_text()).strip()
                    except Exception:
                        modal_text = "La fecha de expedición de la Cédula de Ciudadanía no es correcta, por favor verifique."

                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=0,
                        estado="Offline",
                        mensaje=modal_text,
                        archivo=relative_path
                    )
                    await navegador.close()
                    return

                # 2) Si aparece alerta "procesando", esperamos hasta 10s por resultado
                procesando = False
                try:
                    await pagina.wait_for_selector(ALERT_PROC, timeout=3000)
                    procesando = True
                except PWTimeoutError:
                    procesando = False

                if procesando:
                    # Espera adicional a que salga la respuesta (hasta 10s)
                    got_response = False
                    try:
                        await pagina.wait_for_selector(RESP_SEL, timeout=10000)
                        got_response = True
                    except PWTimeoutError:
                        got_response = False

                    if not got_response:
                        # No salió respuesta en 10s -> Offline
                        screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}_procesando_timeout.png"
                        absolute_path = os.path.join(absolute_folder, screenshot_name)
                        relative_path = os.path.join(relative_folder, screenshot_name).replace("\\", "/")
                        try:
                            await pagina.screenshot(path=absolute_path, full_page=True)
                        except Exception:
                            pass

                        await sync_to_async(Resultado.objects.create)(
                            consulta_id=consulta_id,
                            fuente=fuente_obj,
                            score=0,
                            estado="Offline",
                            mensaje="Ha ocurrido un error, intente más tarde.",
                            archivo=relative_path
                        )
                        await navegador.close()
                        return

                # 3) Si no hubo modal error y (hay o no hubo alerta), intentamos leer respuesta normal
                try:
                    await pagina.wait_for_selector(RESP_SEL, timeout=15000)
                except PWTimeoutError:
                    # Último intento: dar un pequeño respiro
                    await pagina.wait_for_timeout(500)

                # Screenshot (full page)
                screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
                absolute_path = os.path.join(absolute_folder, screenshot_name)
                relative_path = os.path.join(relative_folder, screenshot_name).replace("\\", "/")
                try:
                    await pagina.screenshot(path=absolute_path, full_page=True)
                except Exception:
                    await pagina.screenshot(path=absolute_path, full_page=False)

                # Revisar contenido
                try:
                    texto_respuesta = (await pagina.locator(RESP_SEL).inner_text()).strip()
                except Exception:
                    texto_respuesta = (await pagina.inner_text("body")).strip()

                texto_upper = texto_respuesta.upper()
                if "NO TIENE MEDIDAS CORRECTIVAS PENDIENTES POR CUMPLIR" in texto_upper:
                    score = 0
                    mensaje = "NO TIENE MEDIDAS CORRECTIVAS PENDIENTES POR CUMPLIR"
                else:
                    score = 10
                    mensaje = "Tiene medidas correctivas pendientes"

                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validado",
                    mensaje=mensaje,
                    archivo=relative_path
                )

                await navegador.close()
                return  # exit si todo salió bien

        except Exception as e:
            print(f"[Intento {intento}] Error: {e}")
            error_screenshot = os.path.join(absolute_folder, f"{nombre_sitio}_{cedula}_{timestamp}_error.png")
            try:
                if pagina:
                    await pagina.screenshot(path=error_screenshot, full_page=True)
                else:
                    error_screenshot = ""
            except Exception:
                error_screenshot = ""
            try:
                if navegador:
                    await navegador.close()
            except Exception:
                pass

            if intento == MAX_INTENTOS:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin validar",
                    mensaje=f"Error tras {MAX_INTENTOS} intentos: {str(e)}",
                    archivo=error_screenshot if error_screenshot else ""
                )
