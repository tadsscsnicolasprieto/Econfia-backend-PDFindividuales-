# bots/portal_transparencia_cepim.py
import os
import re
import urllib.parse
import unicodedata
import asyncio
import logging
from datetime import datetime
from typing import Optional

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, Page

from core.models import Resultado, Fuente

logger = logging.getLogger(__name__)

NOMBRE_SITIO = "portal_transparencia_cepim"  # registra este nombre en tu tabla Fuente

URL_DATASET = "https://portaldatransparencia.gov.br/sancoes/cepim"
URL_SEARCH = "https://portaldatransparencia.gov.br/busca?termo={q}"

GOTO_TIMEOUT_MS = 180_000

# Selectores
SEL_COOKIE_BTN = "#accept-all-btn"
SEL_ZERO_H3 = "h3.busca-portal-title-text-1.busca-portal-dmb-10"
SEL_LIST = "ul#resultados.lista-resultados"
SEL_ITEM = f"{SEL_LIST} .busca-portal-block-searchs__item"

SEL_ITEM_TITLE_CANDIDATES = [
    "a[title]", "a", "h2", "h3", "h4",
    ".titulo", ".title", ".nome",
    "strong", "header"
]


def _norm(s: str) -> str:
    """Normaliza texto: minúsculas, sin tildes, espacios comprimidos."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)


async def safe_goto(page: Page, url: str, folder: str, prefix: str, timeout: int = GOTO_TIMEOUT_MS, attempts: int = 3):
    """
    Navega con reintentos; guarda HTML si hay 403 u otros errores.
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


async def consultar_portal_transparencia_cepim(consulta_id: int, nombre: str, apellido: str):
    """
    Busca por nombre en la sección CEPIM del portal de transparencia y guarda Resultado.
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
    last_error = None

    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            # lanzar navegador con opciones stealth básicas
            navegador = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
            )

            # user-agent y cabeceras realistas para reducir bloqueos
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

            # pequeño stealth patch para navigator.webdriver y languages
            try:
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.navigator.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR','pt','en'] });
                """)
            except Exception:
                pass

            page = await context.new_page()

            # 1) Visitar la página CEPIM y aceptar cookies si aparecen (no crítico)
            try:
                await safe_goto(page, URL_DATASET, absolute_folder, f"dataset_{ts}")
                await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                for _ in range(3):
                    try:
                        btn = page.locator(SEL_COOKIE_BTN)
                        if await btn.count() > 0 and await btn.first.is_visible():
                            await btn.first.click(timeout=5_000)
                            await page.wait_for_timeout(500)
                        else:
                            break
                    except Exception:
                        break
            except Exception:
                # no fatal: continuamos hacia la búsqueda
                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass

            # 2) Búsqueda por URL con el nombre completo
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)
            try:
                await safe_goto(page, search_url, absolute_folder, f"search_{ts}")
                await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=30_000)
                except Exception:
                    pass
            except Exception:
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

            # 3) Leer H3 resumen (si existe) para detectar 0 resultados
            zero_h3 = page.locator(SEL_ZERO_H3)
            h3_text, h3_html = "", ""
            try:
                if await zero_h3.count() > 0:
                    h3_text = (await zero_h3.first.inner_text()).strip()
                    h3_html = await zero_h3.first.inner_html()
            except Exception:
                pass

            # si el H3 indica countResultados == 0, usamos ese mensaje
            if h3_text and re.search(r'id=["\']countResultados["\']\s*>\s*0\s*<', h3_html or "", flags=re.I):
                mensaje_final = h3_text
                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True

            else:
                # 4) Iterar resultados y buscar coincidencia exacta del nombre
                items = page.locator(SEL_ITEM)
                n = await items.count()
                exact_hit = False

                for i in range(n):
                    item = items.nth(i)
                    title_text = ""

                    # probar varios selectores candidatos para extraer título/nombre
                    for sel in SEL_ITEM_TITLE_CANDIDATES:
                        try:
                            loc = item.locator(sel).first
                            if await loc.count() > 0 and await loc.is_visible():
                                title_text = (await loc.inner_text(timeout=2_000)).strip()
                                if title_text:
                                    break
                        except Exception:
                            continue

                    # fallback: todo el texto del item
                    if not title_text:
                        try:
                            title_text = (await item.inner_text(timeout=2_000)).strip()
                        except Exception:
                            title_text = ""

                    if title_text and _norm(title_text) == norm_query:
                        exact_hit = True
                        break

                if exact_hit:
                    score_final = 5
                    mensaje_final = f"Coincidencia exacta con el nombre buscado: '{full_name}'."
                else:
                    score_final = 1
                    if h3_text:
                        mensaje_final = f"{h3_text} Se encontraron resultados, pero sin coincidencia exacta del nombre."
                    else:
                        mensaje_final = "Se encontraron resultados, pero sin coincidencia exacta del nombre."

                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass

                success = True

            # 5) Cerrar navegador/contexto
            try:
                await context.close()
            except Exception:
                pass
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 6) Persistir Resultado
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
                mensaje=last_error or "No fue posible obtener resultados.",
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
