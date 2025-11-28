# bots/portal_transparencia_busca.py
import os
import re
import unicodedata
import urllib.parse
import asyncio
import logging
from datetime import datetime
from typing import Optional

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, Page

from core.models import Resultado, Fuente

logger = logging.getLogger(__name__)

NOMBRE_SITIO = "portal_transparencia_busca"
URL_SEARCH = "https://portaldatransparencia.gov.br/busca?termo={q}"
GOTO_TIMEOUT_MS = 180_000

# Selectores
SEL_COOKIE_BTN = "#accept-all-btn"
SEL_H3_SUMMARY = "h3.busca-portal-title-text-1.busca-portal-dmb-10"
SEL_COUNT = "#countResultados"
SEL_TERM = "#infoTermo"
SEL_LIST = "ul#resultados.lista-resultados"
SEL_ITEM = f"{SEL_LIST} .busca-portal-block-searchs__item"

SEL_ITEM_TITLE_CANDS = [
    "h2 a", "h3 a", "a[title]", "a",
    "h2", "h3", "h4",
    "strong", ".titulo", ".title", ".nome", ".busca-portal-text-1", "header"
]


def _norm(s: str) -> str:
    """Normaliza texto para comparación exacta."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)


async def safe_goto(page: Page, url: str, folder: str, prefix: str, timeout: int = GOTO_TIMEOUT_MS, attempts: int = 3):
    """
    Navega con reintentos y guarda HTML si hay 403 u otros errores.
    Devuelve la Response si tuvo éxito.
    """
    last_exc: Optional[Exception] = None
    delay = 1.0
    for i in range(1, attempts + 1):
        try:
            resp = await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            status = resp.status if resp else None
            if status == 403:
                # guardar body para inspección
                try:
                    body = await resp.text()
                    path = os.path.join(folder, f"{prefix}_403_{i}.html")
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(body)
                except Exception:
                    pass
                last_exc = Exception(f"HTTP 403 en intento {i}")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return resp
        except Exception as e:
            last_exc = e
            await asyncio.sleep(delay)
            delay *= 2
    raise last_exc


async def consultar_portal_transparencia_busca(consulta_id: int, nombre: str, apellido: str):
    """
    Busca por nombre en portaldatransparencia.gov.br y guarda Resultado con captura.
    """
    navegador = None
    full_name = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()

    # obtener Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=1,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    if not full_name:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=1,
            estado="Sin Validar",
            mensaje="Nombre y/o apellido vacíos para la consulta.",
            archivo=""
        )
        return

    # preparar carpeta y nombres
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^\w\.-]+", "_", full_name)
    png_name = f"{NOMBRE_SITIO}_{safe_name}_{ts}.png"
    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    mensaje_final = "No hay coincidencias."
    success = False
    score_final = 1
    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            # lanzar navegador con opciones stealth básicas
            navegador = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
            )

            # cabeceras y user-agent realista
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            extra_headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,es-419;q=0.8,es;q=0.7,en;q=0.6",
                "Referer": "https://www.google.com/",
            }

            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="pt-BR",
                timezone_id="America/Bogota",
                user_agent=ua,
                extra_http_headers=extra_headers,
                ignore_https_errors=True,
            )

            # pequeño stealth patch
            try:
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.navigator.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR','pt','en'] });
                """)
            except Exception:
                pass

            page = await context.new_page()

            # 1) Ir directo a la búsqueda por URL
            q = urllib.parse.quote_plus(full_name)
            url = URL_SEARCH.format(q=q)

            try:
                await safe_goto(page, url, absolute_folder, f"search_{ts}")
                await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=30_000)
                except Exception:
                    pass
            except Exception as e:
                # guardar HTML y screenshot para diagnóstico
                try:
                    html = await page.content()
                    with open(os.path.join(absolute_folder, f"search_html_{ts}.html"), "w", encoding="utf-8") as fh:
                        fh.write(html)
                except Exception:
                    pass
                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                raise

            # 2) Aceptar cookies si aparece
            for _ in range(3):
                try:
                    btn = page.locator(SEL_COOKIE_BTN)
                    if await btn.count() > 0 and await btn.first.is_visible():
                        await btn.first.click(timeout=5_000)
                        await page.wait_for_timeout(400)
                    else:
                        break
                except Exception:
                    break

            # 3) Leer encabezado resumen
            h3 = page.locator(SEL_H3_SUMMARY).first
            h3_text = ""
            count_text = ""
            term_text = full_name
            try:
                if await h3.count() > 0:
                    h3_text = (await h3.inner_text()).strip()
                cnt = page.locator(SEL_COUNT).first
                if await cnt.count() > 0:
                    count_text = (await cnt.inner_text()).strip()
                term = page.locator(SEL_TERM).first
                if await term.count() > 0:
                    term_text = (await term.inner_text()).strip()
            except Exception:
                pass

            # 4) Si contador es 0 -> guardar y salir
            if (count_text or "").strip() == "0":
                mensaje_final = h3_text or f"Aproximadamente 0 resultados encontrados para {term_text}"
                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True

            else:
                # 5) Iterar resultados y buscar coincidencia exacta
                items = page.locator(SEL_ITEM)
                n = await items.count()
                exact = False

                for i in range(n):
                    it = items.nth(i)
                    item_title = ""

                    # probar candidatos de título
                    for sel in SEL_ITEM_TITLE_CANDS:
                        try:
                            loc = it.locator(sel).first
                            if await loc.count() > 0 and await loc.is_visible():
                                item_title = (await loc.inner_text(timeout=2000)).strip()
                                if item_title:
                                    break
                        except Exception:
                            continue

                    if not item_title:
                        try:
                            item_title = (await it.inner_text(timeout=2000)).strip()
                        except Exception:
                            item_title = ""

                    if item_title and _norm(item_title) == norm_query:
                        exact = True
                        break

                if exact:
                    score_final = 5
                    mensaje_final = f"Coincidencia exacta con el nombre buscado: '{full_name}'."
                else:
                    score_final = 1
                    if h3_text:
                        mensaje_final = f"{h3_text} Sin coincidencia exacta del nombre."
                    else:
                        mensaje_final = "Se encontraron resultados, pero sin coincidencia exacta del nombre."

                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass

                success = True

            # 6) Cerrar navegador
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 7) Persistir Resultado
        if success:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=score_final, estado="Validada",
                mensaje=mensaje_final, archivo=relative_png
            )
        else:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1, estado="Sin Validar",
                mensaje="No fue posible obtener resultados.",
                archivo=relative_png
            )

    except Exception as e:
        # guardar error y cerrar navegador si está abierto
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1, estado="Sin Validar",
                mensaje=str(e), archivo=""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
