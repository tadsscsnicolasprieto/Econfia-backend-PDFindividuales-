# consulta/eeas.py
import os
import re
import asyncio
import unicodedata
import urllib.parse
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from core.models import Resultado, Fuente

NOMBRE_SITIO = "eeas"  # Asegúrate de tener esta Fuente creada en tu BD

BASE_URL = "https://www.eeas.europa.eu/_en"
SEARCH_URL = "https://www.eeas.europa.eu/search_en?fulltext={q}&created=&created_1="

GOTO_TIMEOUT_MS = 90_000

# Selector del botón “Accept all cookies”
SEL_ACCEPT_COOKIES = "a.wt-ecl-button.wt-ecl-button--primary.wt-cck--actions-button[href='#accept']"

# Selector de las tarjetas de resultado solicitadas
SEL_RESULT_CARD = "div.node.card.node--type-topic-page.node--view-mode-search-result.clearfix"
SEL_CARD_TITLE_ANCHOR = ".card-body .card-title a"

def _norm(txt: str) -> str:
    """
    Normaliza para comparación exacta pero robusta:
    - quita diacríticos
    - colapsa espacios
    - lower-case
    - recorta extremos
    """
    if not txt:
        return ""
    txt = unicodedata.normalize("NFD", txt)
    txt = "".join(ch for ch in txt if unicodedata.category(ch) != "Mn")
    txt = re.sub(r"\s+", " ", txt).strip().lower()
    return txt

async def consultar_eeas(consulta_id: int, nombre: str, apellido: str):
    navegador = None

    # ------ 1) Obtener Fuente ------
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    nombre = (nombre or "").strip()
    apellido = (apellido or "").strip()
    nombre_completo = f"{nombre} {apellido}".strip()
    if not nombre_completo:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar",
            mensaje="Nombre y/o apellido vacíos para la consulta.",
            archivo=""
        )
        return

    # ------ 2) Rutas de evidencia ------
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_q = re.sub(r"[^\w\.-]+", "_", nombre_completo) or "consulta"
    png_name = f"{NOMBRE_SITIO}{safe_q}{ts}.png"
    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    # ------ 3) Navegación y scraping ------
    score_final = 1  # según requerimiento, siempre 1
    mensaje_final = "No se encontraron coincidencias."
    success = False
    last_error = None

    query = urllib.parse.quote_plus(nombre_completo)
    url = SEARCH_URL.format(q=query)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="en-US",
                timezone_id="Europe/Brussels",
            )
            page = await context.new_page()

            # Pre-carga al dominio base (ayuda a que cargue el banner de cookies)
            try:
                await page.goto(BASE_URL, timeout=GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
                # Aceptar cookies si aparece
                btn = page.locator(SEL_ACCEPT_COOKIES).first
                if await btn.count() > 0:
                    try:
                        if await btn.is_visible():
                            await btn.click()
                            # pequeña espera para que desaparezca el overlay
                            await page.wait_for_timeout(400)
                    except Exception:
                        pass
            except Exception:
                # si falla el warm-up, seguimos igual
                pass

            # Ir a la URL de búsqueda
            await page.goto(url, timeout=GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
            # intentar esperar resultados o el propio body
            try:
                await page.wait_for_selector(f"{SEL_RESULT_CARD}, body", timeout=10_000)
            except Exception:
                pass

            # Extraer títulos de tarjetas
            items = page.locator(SEL_RESULT_CARD)
            n = 0
            try:
                n = await items.count()
            except Exception:
                n = 0

            objetivo = _norm(nombre_completo)
            encontrado = False

            for i in range(n):
                item = items.nth(i)
                try:
                    title = (await item.locator(SEL_CARD_TITLE_ANCHOR).first.inner_text(timeout=1500)).strip()
                except Exception:
                    title = ""

                if _norm(title) == objetivo:
                    encontrado = True
                    break

            if encontrado:
                mensaje_final = "Se han encontrado coincidencias."
            else:
                mensaje_final = "No se encontraron coincidencias."

            # Captura de pantalla (siempre)
            try:
                await page.screenshot(path=absolute_png, full_page=True)
            except Exception:
                pass

            success = True

            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # ------ 4) Persistencia en BD ------
        if success:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score_final,                 # siempre 1
                estado="Validada",
                mensaje=mensaje_final,
                archivo=relative_png
            )
        else:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=last_error or "No fue posible completar la consulta.",
                archivo=relative_png
            )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
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