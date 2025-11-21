# core/bots/mediacion_policia.py
import os
import re
import asyncio
from datetime import datetime, date
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL_MEDIACION = "https://srvcnpc.policia.gov.co/PSC/frm_consultaMediacion.aspx"
NOMBRE_SITIO = "consulta_mediacion"

TIPO_DOC_MAP = {
    "CC": "55", "CE": "57", "NIT": "1", "NUIP": "0", "PAS": "58", "PEP": "848",
}

MSG_NO_MEDIACION = "NO TIENE MEDIACIONES POLICIALES POR ASISTIR."

def _formatear_fecha_ddmmaaaa(fecha_expedicion) -> str:
    if isinstance(fecha_expedicion, (datetime, date)):
        return fecha_expedicion.strftime("%d/%m/%Y")
    s = (fecha_expedicion or "").strip()
    if not s:
        raise ValueError("fecha_expedicion vacía")
    try:
        datetime.strptime(s, "%d/%m/%Y")
        return s
    except Exception:
        pass
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%Y/%m/%d", "%d%m%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%d/%m/%Y")
        except Exception:
            continue
    import re as _re
    parts = [p for p in _re.split(r"\D+", s) if p]
    if len(parts) == 3:
        if len(parts[0]) == 4:
            y, m, d = parts
        elif len(parts[2]) == 4:
            d, m, y = parts
        else:
            d, m, y = parts
        return datetime(int(y), int(m), int(d)).strftime("%d/%m/%Y")
    raise ValueError(f"Formato de fecha inválido: {s}")

async def consultar_mediacion(consulta_id, cedula, tipo_doc, fecha_expedicion):
    MAX_INTENTOS = 3
    fecha_expedicion_str = _formatear_fecha_ddmmaaaa(fecha_expedicion)

    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    for intento in range(1, MAX_INTENTOS + 1):
        navegador = pagina = None
        try:
            async with async_playwright() as p:
                tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())
                if not tipo_doc_val:
                    raise ValueError(f"Tipo de documento no válido: {tipo_doc}")

                navegador = await p.chromium.launch(headless=True)
                pagina = await navegador.new_page()
                await pagina.goto(URL_MEDIACION, wait_until="domcontentloaded", timeout=90000)
                try:
                    await pagina.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass

                # 1) Tipo de documento
                await pagina.select_option(
                    '#ctl00_ContentPlaceHolder3_ddlTipoDoc, select[id*="ddlTipoDoc"]',
                    tipo_doc_val
                )
                await pagina.wait_for_timeout(1200)  # pausa intencional

                # 2) Número de documento
                num_sel_main = '#ctl00_ContentPlaceHolder3_txtNumeroDocumento'
                num_sel_fallback = (
                    'input[name="ctl00$ContentPlaceHolder3$txtNumeroDocumento"], '
                    'input[id*="txtNumeroDocumento"], input[id*="txtExpediente"], '
                    'input[id*="txtDocumento"], input[id*="txtIdentificacion"]'
                )
                try:
                    await pagina.fill(num_sel_main, str(cedula))
                except Exception:
                    await pagina.fill(num_sel_fallback, str(cedula))
                # refuerzo por JS (máscaras/postback)
                try:
                    await pagina.evaluate(
                        """(sel1, sel2, val) => {
                            const el = document.querySelector(sel1) || document.querySelector(sel2);
                            if (el) {
                                el.value = val;
                                el.dispatchEvent(new Event('input', {bubbles:true}));
                                el.dispatchEvent(new Event('change', {bubbles:true}));
                            }
                        }""",
                        num_sel_main, num_sel_fallback.split(",")[0], str(cedula)
                    )
                except Exception:
                    pass
                await pagina.wait_for_timeout(1200)  # pausa

                # 3) Fecha de expedición
                fecha_sel_main = '#txtFechaexp'
                fecha_sel_fallback = 'input[name="ctl00$ContentPlaceHolder3$txtFechaexp"], input[id*="txtFecha"]'
                try:
                    await pagina.click(fecha_sel_main, timeout=1500)
                    await pagina.fill(fecha_sel_main, fecha_expedicion_str)
                except Exception:
                    await pagina.fill(fecha_sel_fallback, fecha_expedicion_str)
                try:
                    await pagina.evaluate(
                        """(sel1, sel2, val) => {
                            const el = document.querySelector(sel1) || document.querySelector(sel2);
                            if (el) {
                                el.value = val;
                                el.dispatchEvent(new Event('input', {bubbles:true}));
                                el.dispatchEvent(new Event('change', {bubbles:true}));
                            }
                        }""",
                        fecha_sel_main, fecha_sel_fallback.split(",")[0], fecha_expedicion_str
                    )
                except Exception:
                    pass
                await pagina.wait_for_timeout(800)  # pequeña pausa

                # 4) Buscar (ancla con ícono de lupa)
                btn_sel = '#ctl00_ContentPlaceHolder3_btnConsultar2, a[onclick*="btnGuardar"][href*="__doPostBack"]'
                try:
                    await pagina.click(btn_sel, timeout=3000)
                except Exception:
                    # último recurso: Enter en fecha
                    try:
                        await pagina.press(fecha_sel_main, "Enter")
                    except Exception:
                        await pagina.press(num_sel_main, "Enter")

                # Esperas de carga (SweetAlert/POSTBACK)
                try:
                    await pagina.wait_for_selector("div.swal2-container", timeout=4000)
                except PWTimeoutError:
                    pass
                try:
                    await pagina.wait_for_selector("div.swal2-container", state="detached", timeout=25000)
                except PWTimeoutError:
                    await pagina.wait_for_timeout(1500)

                # Contenedor de respuesta (si existe)
                try:
                    await pagina.wait_for_selector('div#ctl00_ContentPlaceHolder3_respuesta', timeout=12000)
                except PWTimeoutError:
                    pass

                # 5) Evidencia
                shot = f"mediacion_{cedula}_{ts}.png"
                abs_png = os.path.join(absolute_folder, shot)
                rel_png = os.path.join(relative_folder, shot).replace("\\", "/")
                try:
                    await pagina.screenshot(path=abs_png, full_page=True)
                except Exception:
                    await pagina.screenshot(path=abs_png, full_page=False)

                # 6) Mensaje / score
                body_text = re.sub(r"\s+", " ", (await pagina.inner_text("body")).strip()).upper()
                if MSG_NO_MEDIACION in body_text:
                    score = 1
                    mensaje = MSG_NO_MEDIACION
                else:
                    score = 5
                    mensaje = "Registra información diferente a 'NO TIENE MEDIACIONES POLICIALES POR ASISTIR.' (ver captura)."

                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj,
                    score=score, estado="Validado", mensaje=mensaje, archivo=rel_png
                )

                await navegador.close()
                return

        except Exception as e:
            print(f"[Mediación intento {intento}] Error: {e}")
            err = os.path.join(absolute_folder, f"mediacion_{cedula}_{ts}_error.png")
            try:
                if pagina:
                    await pagina.screenshot(path=err, full_page=True)
                else:
                    err = ""
            except Exception:
                err = ""
            try:
                if navegador:
                    await navegador.close()
            except Exception:
                pass
            if intento == MAX_INTENTOS:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj, score=0,
                    estado="Sin validar", mensaje=f"Error tras {MAX_INTENTOS} intentos: {str(e)}",
                    archivo=err if err else ""
                )
