import os
import re
from datetime import datetime
from urllib.parse import quote

from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

NOMBRE_SITIO = "secretservice_mostwanted"
# Página de búsqueda del sitio (usa el bloque que me mostraste: #search-block-wrap)
URL_SEARCH = "https://www.secretservice.gov/search"

MAX_INTENTOS = 3


def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"


async def consultar_secretservice_mostwanted_pdf(consulta_id: int, nombre: str, cedula, apellido: str = ""):
    """
    Usa el BUSCADOR INTERNO del sitio:
    - Va a /search
    - Escribe el nombre en el input interno (clase .gsc-input)
    - Click en el botón (clase .gsc-search-button)
    - Espera resultados o "No Results"
    - Siempre toma captura y crea Resultado
    """
    nombre = (nombre or "").strip()
    apellido = (apellido or "").strip()
    nombre_completo = f"{nombre} {apellido}".strip()
    nombre_busqueda = nombre_completo if nombre_completo else nombre
    nombre_lower = nombre_busqueda.lower()

    # Carpetas
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_filename = f"{NOMBRE_SITIO}_{cedula}_{timestamp}.png"
    screenshot_path = os.path.join(absolute_folder, screenshot_filename)
    screenshot_rel = os.path.join(relative_folder, screenshot_filename)

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo="",
        )
        return

    for intento in range(1, MAX_INTENTOS + 1):
        browser = None
        page = None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                ctx = await browser.new_context(viewport={"width": 1440, "height": 1024})
                page = await ctx.new_page()

                # 1) Ir a la página de búsqueda
                await page.goto(URL_SEARCH, wait_until="domcontentloaded", timeout=120000)

                # 2) Esperar que aparezca el bloque del buscador interno
                await page.wait_for_selector("#search-block-wrap", timeout=20000)
                await page.wait_for_load_state("networkidle")

                # 3) Esperar el input del buscador interno (clase gsc-input)
                input_locator = page.locator("#search-block-wrap input.gsc-input")
                await input_locator.wait_for(timeout=20000)

                # Limpiar y escribir el nombre
                await input_locator.fill("")
                await input_locator.type(nombre_busqueda)

                # 4) Click en el botón de buscar
                search_btn = page.locator(
                    "#search-block-wrap button.gsc-search-button, "
                    "#search-block-wrap button.gsc-search-button-v2"
                )
                await search_btn.click()

                # 5) Esperar a que salgan resultados o el mensaje de "No Results"
                #   le damos un tiempo generoso porque CSE a veces se demora.
                await page.wait_for_timeout(4000)

                # Forzar scroll un poquito para que carguen cosas perezosas
                await page.mouse.wheel(0, 1000)
                await page.wait_for_timeout(2000)

                # Recolectar resultados (Google CSE normalmente usa .gsc-webResult o .gsc-result)
                results_locator = page.locator(".gsc-webResult, .gsc-result")
                results_count = await results_locator.count()

                # También revisamos si aparece el texto "No Results"
                no_results_text = await page.locator("text=No Results").count()

                if results_count > 0:
                    score = 10
                    mensaje = f"Se encontraron {results_count} resultados en el buscador interno."
                elif no_results_text > 0:
                    score = 0
                    mensaje = "No se encontraron resultados en el buscador interno."
                else:
                    # Caso ambiguo: ni resultados ni 'No Results', lo tomamos como sin coincidencias
                    score = 0
                    mensaje = "No fue posible determinar resultados (sin coincidencias visibles)."

                # 6) Siempre tomar captura, haya o no haya resultados
                await page.screenshot(path=screenshot_path, full_page=True)

                # 7) Guardar en BD
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validado",
                    mensaje=mensaje,
                    archivo=screenshot_rel,
                )

                await ctx.close()
                await browser.close()
                return

        except Exception as e:
            # Intentar captura incluso si hay error
            try:
                if page:
                    await page.screenshot(path=screenshot_path, full_page=True)
            except:
                pass

            if intento == MAX_INTENTOS:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin validar",
                    mensaje=f"Error al usar el buscador interno: {e}",
                    archivo=screenshot_rel if os.path.exists(screenshot_path) else "",
                )

            try:
                if browser:
                    await browser.close()
            except:
                pass
