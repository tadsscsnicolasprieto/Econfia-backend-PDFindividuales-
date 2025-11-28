# bots/portal_transparencia_ceis.py
import os, re, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, Page

from core.models import Resultado, Fuente

NOMBRE_SITIO = "portal_transparencia_ceis"  # crea/asegura esta Fuente en BD

# Se intentarán ambos dominios (algunos bloquean acceso directo sin www)
BASE_DOMAINS = [
    "https://www.portaldatransparencia.gov.br",
    "https://portaldatransparencia.gov.br",
]
PATH_DATASET = "/entenda-a-gestao-publica/ceis"
PATH_SEARCH = "/busca?termo={q}"

GOTO_TIMEOUT_MS = 180_000

# Cookies
SEL_COOKIE_BTN  = "#accept-all-btn"

# "0 resultados" (mismo patrón del portal)
SEL_ZERO_H3     = "h3.busca-portal-title-text-1.busca-portal-dmb-10"
# ejemplo HTML:
# <h3 ...><strong> Aproximadamente <strong id="countResultados">0</strong> resultados encontrados</strong> para
#   <span id="infoTermo">jaider leonardo barrera chacon</span></h3>

# Lista y items de resultados
SEL_LIST        = "ul#resultados.lista-resultados"
SEL_ITEM        = f"{SEL_LIST} .busca-portal-block-searchs__item"

# Candidatos para extraer el “nombre/título” dentro del item (robusto ante cambios)
SEL_ITEM_TITLE_CANDIDATES = [
    "a[title]", "a", "h2", "h3", "h4",
    ".titulo", ".title", ".nome",
    "strong", "header"
]

PLAYWRIGHT_HEADLESS = getattr(settings, "PLAYWRIGHT_HEADLESS", True)
NETWORK_IDLE_TIMEOUT_MS = 30_000

# Encabezados realistas para reducir bloqueos CloudFront / WAF
REALISTIC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
EXTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

def _is_cloudfront_block(html: str) -> bool:
    if not html:
        return False
    h = html.lower()
    return (
        "403 error" in h and "cloudfront" in h and "request blocked" in h
    )

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)

def _extract_count_from_h3_html(html: str) -> int | None:
    if not html:
        return None
    m = re.search(r'id=["\']countResultados["\']>\s*(\d+)\s*<', html)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None

async def _safe_screenshot(page: Page, path: str):
    try:
        await page.screenshot(path=path, full_page=True)
    except Exception:
        pass

async def consultar_portal_transparencia_ceis(consulta_id: int, nombre: str, apellido: str):
    navegador = None
    full_name = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()

    # 1) Fuente
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

    # 2) Carpeta / archivo
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
            navegador = await p.chromium.launch(
                headless=PLAYWRIGHT_HEADLESS,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="pt-BR",
                timezone_id="America/Bogota",
                user_agent=REALISTIC_UA,
                extra_http_headers=EXTRA_HEADERS,
            )
            # Pequeño script para ocultar webdriver (bypass básico)
            await context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                """
            )
            page = await context.new_page()

            # 3) Intentar visitar dataset en ambos dominios para establecer cookies/sesión
            dataset_loaded = False
            for base in BASE_DOMAINS:
                url_dataset = base + PATH_DATASET
                try:
                    resp = await page.goto(url_dataset, timeout=GOTO_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                    html_tmp = await page.content()
                    if resp and resp.status == 200 and not _is_cloudfront_block(html_tmp):
                        dataset_loaded = True
                except Exception:
                    continue
                # Aceptar cookies si aparece
                try:
                    for _ in range(3):
                        btn = page.locator(SEL_COOKIE_BTN)
                        if await btn.count() > 0 and await btn.first.is_visible():
                            try:
                                await btn.first.click(timeout=5_000)
                                await page.wait_for_timeout(500)
                            except Exception:
                                pass
                        else:
                            break
                except Exception:
                    pass
                if dataset_loaded:
                    break

            # Si no cargó dataset limpio, se continúa igualmente (puede aún permitir búsqueda)

            # 4) Intentar búsqueda en ambos dominios con reintentos si 403
            q = urllib.parse.quote_plus(full_name)
            search_loaded = False
            search_html = ""
            for base in BASE_DOMAINS:
                search_url = base + PATH_SEARCH.format(q=q)
                try:
                    resp = await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
                    except Exception:
                        pass
                    search_html = await page.content()
                    status_ok = resp and resp.status == 200
                    if status_ok and not _is_cloudfront_block(search_html):
                        search_loaded = True
                        break
                except Exception:
                    continue

            # Si se detecta bloqueo CloudFront mostrar mensaje apropiado
            if not search_loaded and _is_cloudfront_block(search_html):
                mensaje_final = "Bloqueado por CloudFront/WAF (403). Reintentos fallidos."
                await _safe_screenshot(page, absolute_png)
                success = True
                score_final = 1
                # Cierre anticipado y guardado
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await navegador.close()
                except Exception:
                    pass
                navegador = None
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj,
                    score=score_final,
                    estado="Validada",
                    mensaje=mensaje_final,
                    archivo=relative_png
                )
                return

            # 5) Chequear si el H3 indica 0 resultados (score 1 y mensaje del H3)
            zero_h3 = page.locator(SEL_ZERO_H3)
            zero_h3_txt = ""
            h3_html = ""
            count_resultados = None
            try:
                if await zero_h3.count() > 0:
                    zero_h3_txt = (await zero_h3.first.inner_text()).strip()
                    h3_html = await zero_h3.first.inner_html()
                    count_resultados = _extract_count_from_h3_html(h3_html)
            except Exception:
                pass

            is_zero = (count_resultados == 0)

            if is_zero:
                mensaje_final = zero_h3_txt or "Aproximadamente 0 resultados encontrados."
                await _safe_screenshot(page, absolute_png)
                success = True

            else:
                # 6) Iterar resultados y comparar exacto
                items = page.locator(SEL_ITEM)
                n = await items.count()
                exact_hit = False

                for i in range(n):
                    item = items.nth(i)
                    title_text = ""

                    for sel in SEL_ITEM_TITLE_CANDIDATES:
                        loc = item.locator(sel).first
                        try:
                            if await loc.count() > 0 and await loc.is_visible():
                                title_text = (await loc.inner_text(timeout=2_000)).strip()
                                if title_text:
                                    break
                        except Exception:
                            continue

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
                    if zero_h3_txt:
                        mensaje_final = f"{zero_h3_txt} Se encontraron resultados, pero sin coincidencia exacta del nombre."
                    else:
                        mensaje_final = "Se encontraron resultados, pero sin coincidencia exacta del nombre."

                await _safe_screenshot(page, absolute_png)

                success = True

            # 7) Cierre
            try:
                await context.close()
            except Exception:
                pass
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 8) Persistencia Resultado
        if success:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=score_final,
                estado="Validada",
                mensaje=mensaje_final,
                archivo=relative_png
            )
        else:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1, estado="Sin Validar",
                mensaje="No fue posible obtener resultados.",
                archivo=relative_png
            )

    except Exception as e:
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
