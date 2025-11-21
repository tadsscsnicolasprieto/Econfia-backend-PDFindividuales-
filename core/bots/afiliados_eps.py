# core/bots/afiliados_eps.py
import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Consulta, Resultado, Fuente

URL = "https://www.minsalud.gov.co/paginas/consulta-afiliados.aspx"
NOMBRE_SITIO = "afiliados_eps"

TIPO_DOC_MAP = {
    "CC": "1",
    "TI": "2",
    "CE": "4",
    "PAS": "7",
    "PEP": "5",
}

def _normrel(p: str) -> str:
    return (p or "").replace("\\", "/")

async def consultar_afiliados_eps(consulta_id: int, cedula: str, tipo_doc: str):
    async def _get_fuente(nombre: str):
        return await sync_to_async(lambda: Fuente.objects.filter(nombre=nombre).first())()

    async def _crear_resultado(estado: str, archivo: str, mensaje: str, fuente, score: float = 0):
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente,
            estado=estado,
            archivo=_normrel(archivo),
            mensaje=mensaje,
            score=score,
        )

    # constantes de mensajes
    NO_ENCONTRADO_MSG = (
        "No se encontraron registros con los datos ingresados, "
        "por favor verifique la información e intente de nuevo"
    )
    ENCONTRADO_MSG = "Se encontraron registros con los datos ingresados"

    try:
        # Validar consulta y fuente
        await sync_to_async(Consulta.objects.get)(id=consulta_id)
        fuente = await _get_fuente(NOMBRE_SITIO)

        # Validar tipo doc
        tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "CC").upper())
        if not tipo_doc_val:
            await _crear_resultado("Sin Validar", "", f"Tipo de documento no soportado: {tipo_doc!r}", fuente, score=0)
            return

        # Rutas de salida
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shot_name = f"{NOMBRE_SITIO}_{cedula}_{ts}.png"
        abs_png = os.path.join(absolute_folder, shot_name)
        rel_png = os.path.join(relative_folder, shot_name)

        # Estado de alerta capturada
        alerta_texto = {"value": None}

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)  # pon True en server si quieres
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            await page.goto(URL, wait_until="domcontentloaded", timeout=120_000)

            # Handler de alert() nativo: registrar texto y cerrarlo para no bloquear
            async def on_dialog(dialog):
                alerta_texto["value"] = dialog.message
                try:
                    await dialog.dismiss()
                except Exception:
                    pass
            page.on("dialog", on_dialog)

            # Rellenar formulario
            await page.select_option('select[name="ddltipoidentificacion"]', tipo_doc_val)
            await page.fill('input[id="txtnumeroidentificacion"]', str(cedula))

            # Disparar consulta (click al botón "Consultar" con fallbacks)
            disparado = False
            for sel in [
                'button:has-text("Consultar")',
                'input[type="submit"][value*="Consultar"]',
                'a:has-text("Consultar")',
            ]:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click(timeout=2000)
                        disparado = True
                        break
                except Exception:
                    continue
            if not disparado:
                # fallback por teclado
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(300)  # pequeño colchón
                await page.keyboard.press("Enter")

            # === Esperar EXACTAMENTE 3s y tomar UN screenshot full-page ===
            await page.wait_for_timeout(3000)
            await page.screenshot(path=abs_png, full_page=True)

            # Determinar mensaje/score
            score = 0
            mensaje = NO_ENCONTRADO_MSG
            if alerta_texto["value"]:
                # usar texto de la alerta para decidir
                if "no se encontraron" in alerta_texto["value"].lower():
                    score = 0
                    mensaje = NO_ENCONTRADO_MSG
                else:
                    # si la alerta es otra cosa, dejarla como mensaje y score 1
                    score = 1
                    mensaje = alerta_texto["value"]
            else:
                # Sin alerta: revisar HTML por cadena de "no se encontraron"
                html_low = (await page.content()).lower()
                if "no se encontraron registros" in html_low:
                    score = 0
                    mensaje = NO_ENCONTRADO_MSG
                else:
                    score = 10
                    mensaje = ENCONTRADO_MSG

            await _crear_resultado("Validada", rel_png, mensaje, fuente, score=score)

            await browser.close()

    except Exception as e:
        try:
            fuente = await _get_fuente(NOMBRE_SITIO)
        except Exception:
            fuente = None
        await _crear_resultado("Sin Validar", "", str(e), fuente, score=0)
