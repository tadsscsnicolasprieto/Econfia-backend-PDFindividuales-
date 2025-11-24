# core/bots/boletin_policia.py
import os
import re
import unicodedata
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Fuente, Resultado

nombre_sitio = "boletin_policia"

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_boletin_policia(cedula: str, consulta_id: int, nombre: str):
    """
    Busca en el portal de la Policía por 'nombre'.
    - Si hay filas en el listado: score=10, mensaje="Se han encontrado hallazgos".
    - Si no hay filas: score=0,  mensaje="No se encontraron hallazgos".
    Guarda un único Resultado con el screenshot del listado.
    """
    url = f"https://www.policia.gov.co/buscador?aggregated_field={nombre}"

    navegador = None
    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()
            await pagina.goto(url, timeout=60000)

            # da tiempo a que la vista cargue
            await pagina.wait_for_timeout(4000)

            # carpeta resultados:  resultado/<consulta_id>
            relative_folder = os.path.join("resultado", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            # nombre de archivo
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
            absolute_path = os.path.join(absolute_folder, screenshot_name)
            relative_path = os.path.join(relative_folder, screenshot_name)

            # intentar recortar al contenedor de resultados; si no, página completa
            cont = pagina.locator(".view-content")
            if await cont.count() > 0:
                await cont.first.screenshot(path=absolute_path)
            else:
                await pagina.screenshot(path=absolute_path, full_page=True)

            # contar filas (cada resultado es .views-row)
            total_rows = 0
            try:
                total_rows = await pagina.locator(".view-content .views-row").count()
            except Exception:
                total_rows = 0

            if total_rows > 0:
                mensaje = "Se han encontrado hallazgos"
                score = 10
            else:
                mensaje = "No se encontraron hallazgos"
                score = 0

            try:
                fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
            except Exception:
                fuente_obj = None

            # guardar único resultado resumen
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validada",
                mensaje=mensaje,
                archivo=relative_path,
            )

            await navegador.close()
            navegador = None

    except Exception as e:
        try:
            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
        except Exception:
            fuente_obj = None

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje=str(e),
            archivo="",
        )
        try:
            if navegador:
                await navegador.close()
        except Exception:
            pass
