import os
import re
import asyncio
import unicodedata
from datetime import datetime
from urllib.parse import quote

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "presidencia_gabinete_busqueda"
BASE_URL = "https://www.presidencia.gov.co/paginas/busqueda.aspx?k="

WAIT_NAV = 15000
WAIT_POST = 2500
MAX_INTENTOS = 3


def _norm(s: str) -> str:
    """minúsculas, sin acentos, solo alfanumérico y espacios, colapsar espacios."""
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


async def consultar_presidencia_gabinete_busqueda(
    consulta_id: int,
    nombre: str,
    apellido: str,
):
    """
    Abre la URL de búsqueda con el nombre completo.
    - #NoResult visible  -> score=1
    - resultados con coincidencia EXACTA (título) -> score=5
    - resultados sin coincidencia exacta -> score=1
    Guarda 1 screenshot full-page y registra el resultado.
    """
    # 1) Fuente
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

    # 2) Carpeta/archivo
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    query = (f"{(nombre or '').strip()} {(apellido or '').strip()}").strip() or "consulta"
    q_norm = _norm(query)

    safe_query = re.sub(r"\s+", "_", query)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_name = f"{NOMBRE_SITIO}_{safe_query}_{ts}.png"
    abs_png = os.path.join(absolute_folder, png_name)
    rel_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    # 3) URL
    url = f"{BASE_URL}{quote(query)}"

    ultimo_error = None
    for intento in range(1, MAX_INTENTOS + 1):
        browser = None
        ctx = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,  # cambia a True en server si no necesitas ver la ventana
                    args=["--disable-blink-features=AutomationControlled", "--start-maximized"]
                )
                ctx = await browser.new_context(viewport=None, locale="es-CO")
                page = await ctx.new_page()

                # Navegar
                await page.goto(url, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=WAIT_NAV)
                except Exception:
                    pass
                await asyncio.sleep(WAIT_POST / 1000)

                # Esperar contenedores probables (no bloqueante)
                for sel in ["#NoResult", ".ms-srch-group-content", ".ms-srch-item"]:
                    try:
                        await page.wait_for_selector(sel, timeout=4000)
                        break
                    except Exception:
                        continue

                # ===== Decidir score/mensaje =====
                score = 1
                mensaje = "No se encontraron resultados"

                # 1) ¿Bloque de 'No se encontró'?
                try:
                    nores = page.locator("#NoResult .ms-srch-result-noResultsTitle").first
                    if await nores.count() > 0 and await nores.is_visible():
                        # Mantener score=1 y mensaje del sitio si existe
                        txt = (await nores.inner_text() or "").strip()
                        if txt:
                            mensaje = txt
                        # Screenshot full-page
                        await page.screenshot(path=abs_png, full_page=True)
                        await ctx.close(); await browser.close()
                        await sync_to_async(Resultado.objects.create)(
                            consulta_id=consulta_id,
                            fuente=fuente_obj,
                            score=score,
                            estado="Validado",
                            mensaje=mensaje,
                            archivo=rel_png,
                        )
                        return
                except Exception:
                    pass

                # 2) Resultados: buscar coincidencia EXACTA del nombre en títulos
                exact_matches = 0
                total_items = 0
                try:
                    # Cada item de resultado
                    items = page.locator(".ms-srch-group-content .ms-srch-item, .ms-srch-item")
                    total_items = await items.count()
                    for i in range(total_items):
                        it = items.nth(i)
                        # título del item (innerText o atributo title del enlace)
                        title_link = it.locator(".ms-srch-item-title a.ms-srch-item-link").first
                        title_txt = ""
                        if await title_link.count() > 0:
                            try:
                                title_txt = (await title_link.get_attribute("title")) or ""
                            except Exception:
                                title_txt = ""
                            if not title_txt:
                                try:
                                    title_txt = (await title_link.inner_text()) or ""
                                except Exception:
                                    title_txt = ""
                        else:
                            # fallback: texto del h3
                            try:
                                title_txt = (await it.locator(".ms-srch-item-title").inner_text()) or ""
                            except Exception:
                                title_txt = ""

                        if _norm(title_txt) == q_norm:
                            exact_matches += 1
                except Exception:
                    pass

                if total_items > 0:
                    if exact_matches > 0:
                        score = 5
                        mensaje = f"Se encontraron {exact_matches} coincidencia(s) exacta(s) para '{query}'."
                    else:
                        score = 1
                        mensaje = "Se encontraron resultados, pero ninguna coincidencia exacta con el nombre completo."
                else:
                    score = 1
                    mensaje = "No se encontraron resultados"

                # Screenshot (full-page para no cortar)
                await page.screenshot(path=abs_png, full_page=True)

                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validado",
                    mensaje=mensaje,
                    archivo=rel_png,
                )

                await ctx.close(); await browser.close()
                return

        except Exception as e:
            ultimo_error = str(e)
            try:
                if ctx:
                    await ctx.close()
            except Exception:
                pass
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
            await asyncio.sleep(2)

    # 4) Falló todo
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=0,
        estado="Sin validar",
        mensaje=f"Ocurrió un problema al obtener la información: {ultimo_error}",
        archivo=rel_png if os.path.exists(abs_png) else "",
    )
