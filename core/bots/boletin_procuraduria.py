import os
import re
import unicodedata
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Fuente, Resultado

nombre_sitio = "boletin_procuraduria"

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

def _score_por_coincidencias(n: int) -> int:
    if n <= 0:
        return 0   # bajo
    if n == 1:
        return 6   # alto
    return 10      # muy alto

async def consultar_boletin_procuraduria(nombre: str, consulta_id: int, cedula: str):
    nombre_encoded = (nombre or "").replace(" ", "%20")
    url = (
        "https://www.procuraduria.gov.co/_layouts/15/osssearchresults.aspx"
        f"?u=https%3A%2F%2Fwww%2Eprocuraduria%2Egov%2Eco&k={nombre_encoded}"
    )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=60000)
            await page.wait_for_timeout(4000)

            # Carpeta resultados
            relative_folder = os.path.join("resultado", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            result_div = await page.query_selector("#Result")

            # Screenshot (preferir contenedor)
            abs_path = os.path.join(absolute_folder, f"{nombre_sitio}_{cedula}_result.png")
            if result_div:
                await result_div.screenshot(path=abs_path)
            else:
                await page.screenshot(path=abs_path, full_page=True)

            # ⇢ ruta relativa para guardar en BD
            rel_path = os.path.relpath(abs_path, settings.MEDIA_ROOT).replace("\\", "/")

            # Conteo de coincidencias exactas (normalizadas)
            coincidencias = 0
            if result_div:
                contenido = await result_div.inner_text()
                nombre_n = _norm(nombre)
                if nombre_n:
                    coincidencias = _norm(contenido).count(nombre_n)

            score = _score_por_coincidencias(coincidencias)
            mensaje = "se encontraron hallazgos" if coincidencias > 0 else "no se encontraron hallazgos"

            # ⇢ Estado SIEMPRE "Validada" si el flujo terminó bien
            estado = "Validada"

            # Guardar en BD
            try:
                fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
            except Exception:
                fuente_obj = None

            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado=estado,
                mensaje=mensaje,
                archivo=rel_path,
            )

            await browser.close()

    except Exception as e:
        # Solo aquí marcamos "Sin Validar"
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
