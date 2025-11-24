import os
import re
from datetime import datetime
from typing import Optional, Tuple

from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Consulta, Resultado, Fuente

url = "https://aplicaciones.adres.gov.co/SII_PRE_WEB/Formularios/frmReportes.aspx"
nombre_sitio = "adres_transito"

# --------- UMBRALES AJUSTABLES (COP) ---------
UMBRAL_ALTO = 200_000        # >= 200k  -> 6
UMBRAL_MUY_ALTO = 800_000    # >= 800k  -> 10


# ================== Helpers ==================

def _parse_cop(maybe_amount: str) -> Optional[float]:
    if not maybe_amount:
        return None
    txt = maybe_amount.strip()
    m = re.search(r"([\d\.,]+)", txt)
    if not m:
        return None
    num = m.group(1)

    if "." in num and "," in num:
        num = num.replace(".", "").replace(",", ".")
    elif "," in num and "." not in num:
        if num.count(",") == 1:
            partes = num.split(",")
            if len(partes[-1]) <= 2:
                num = num.replace(",", ".")
            else:
                num = num.replace(",", "")
        else:
            num = num.replace(",", "")
    elif "." in num and "," not in num:
        if num.count(".") == 1:
            partes = num.split(".")
            if len(partes[-1]) <= 2:
                pass
            else:
                num = num.replace(".", "")
        else:
            num = num.replace(".", "")

    try:
        return float(num)
    except Exception:
        return None


def _calcular_score_por_monto(monto: Optional[float]) -> int:
    if monto is None or monto <= 0:
        return 0
    if monto >= UMBRAL_MUY_ALTO:
        return 10
    if monto >= UMBRAL_ALTO:
        return 6
    return 2


async def _extraer_frase_y_monto_preciso(pagina) -> Tuple[str, Optional[float]]:
    """
    Localiza el ancla 'A la fecha la suma de $' y busca el primer monto
    cercano en el HTML que le sigue. Devuelve (mensaje_final, monto_float).
    - mensaje_final:
        * 'A la fecha la suma de $<monto_formato_original>' si encuentra monto
        * 'A la fecha la suma de $' si no encuentra
    """
    html = await pagina.content()
    # Quitar espacios múltiples para facilitar el slice (sin romper formatos de número)
    html_min = re.sub(r"\s+", " ", html)

    # Buscar ancla (case-insensitive)
    ancla_pat = re.compile(r"a la fecha la suma de\s*\$", re.I)
    ancla = ancla_pat.search(html_min)
    if not ancla:
        # Fallback: intentar localizar un <span> que contenga la frase
        # y luego tomar un trozo del html completo
        try:
            locator = pagina.locator("//span[contains(., 'A la fecha la suma de $')]").first
            if await locator.count() > 0:
                # Si el locator existe, igual trabajamos con html_min para buscar a partir del texto
                ancla = re.search(r"A la fecha la suma de\s*\$", html_min, re.I)
        except Exception:
            ancla = None

    # Si no se encuentra el ancla, no fabricamos frase
    if not ancla:
        return "", None

    start_idx = ancla.end()
    # Tomar una ventana después del ancla (suele estar cerca en el DOM, pero no en el mismo span)
    window = html_min[start_idx:start_idx + 1500]
    # Limpiar tags y entidades simples
    window_txt = re.sub(r"<[^>]+>", " ", window)
    window_txt = re.sub(r"&nbsp;", " ", window_txt, flags=re.I)
    window_txt = re.sub(r"\s+", " ", window_txt).strip()

    # Buscar primer monto después del ancla, preservando el formato original capturado
    m_val = re.search(r"\$\s*([\d\.\,]+)", window_txt)
    if not m_val:
        # No se ve número cercano -> mensaje sin monto y score 0 por regla
        return "A la fecha la suma de $", None

    monto_str_con_signo = m_val.group(0)  # incluye el $
    monto_float = _parse_cop(monto_str_con_signo)

    # Mensaje exacto con el formato original del número
    # Si quieres, puedes normalizar con miles y decimales; por ahora preservamos lo que ADRES muestra.
    mensaje_final = f"A la fecha la suma de {monto_str_con_signo}"
    return mensaje_final, monto_float


# ================== Función principal ==================

async def consultar_adres_transito(consulta_id: int, cedula: str):
    async def _get_fuente_by_nombre(nombre: str):
        return await sync_to_async(lambda: Fuente.objects.filter(nombre=nombre).first())()

    async def _crear_resultado(consulta_id: int, fuente, estado: str, mensaje: str, archivo: str, score: int):
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente,
            estado=estado,
            mensaje=mensaje,
            archivo=archivo,
            score=score,
        )

    try:
        # Verifica que exista la consulta
        await sync_to_async(Consulta.objects.get)(id=consulta_id)

        fuente = await _get_fuente_by_nombre(nombre_sitio)

        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()
            await pagina.goto(url, wait_until="load")

            # Llenar y enviar
            await pagina.fill(
                'input[id="ctl00_ContentPlaceHolder1_ReportViewer1_ctl04_ctl03_txtValue"]',
                cedula
            )
            await pagina.click('input[type="submit"]')

            # Esperar estabilización del ReportViewer
            await pagina.wait_for_load_state("networkidle")

            # ===== Extracción ultra específica del mensaje y monto =====
            mensaje_final, monto = await _extraer_frase_y_monto_preciso(pagina)
            score = _calcular_score_por_monto(monto)

            # Carpetas y captura
            relative_folder = os.path.join('resultados', str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
            absolute_path = os.path.join(absolute_folder, screenshot_name)
            relative_path = os.path.join(relative_folder, screenshot_name)

            await pagina.screenshot(path=absolute_path, full_page=True)
            await navegador.close()

        # Fallback si no encontramos el ancla/mensaje
        if not mensaje_final:
            mensaje_final = "Resultado obtenido (revisar captura)."
            # Si no hubo ancla, probablemente no hay deuda -> score 0 por prudencia
            if score is None:
                score = 0

        await _crear_resultado(
            consulta_id=consulta_id,
            fuente=fuente,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=relative_path,
            score=score
        )

    except Exception as e:
    # Intentar tomar screenshot antes de registrar el error
        try:    
            if 'pagina' in locals() and pagina:
                relative_folder = os.path.join('resultados', str(consulta_id))
                absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
                os.makedirs(absolute_folder, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_name = f"ERROR_{nombre_sitio}_{cedula}_{timestamp}.png"
                absolute_path = os.path.join(absolute_folder, screenshot_name)
                relative_path = os.path.join(relative_folder, screenshot_name)
                await pagina.screenshot(path=absolute_path, full_page=True)
            else:
                relative_path = ""
        except Exception:
            relative_path = ""

        try:
            fuente = await _get_fuente_by_nombre(nombre_sitio)
        except Exception:
            fuente = None

        await _crear_resultado(
            consulta_id=consulta_id,
            fuente=fuente,
            estado="Sin Validar",
            mensaje=str(e),
            archivo=relative_path,
            score=0
        )
