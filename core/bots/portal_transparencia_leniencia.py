# bots/portal_transparencia_leniencia.py
import os, re, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "portal_transparencia_leniencia"  # agrega este nombre en tu tabla Fuente

# Dominios alternos para reducir bloqueos CloudFront/WAF
BASE_DOMAINS = [
    "https://www.portaldatransparencia.gov.br",
    "https://portaldatransparencia.gov.br",
]
# Rutas del dataset y de búsqueda
PATH_DATASET = "/download-de-dados/acordos-leniencia"
PATH_SEARCH  = "/busca?termo={q}"

GOTO_TIMEOUT_MS = 180_000

# Selectores
SEL_COOKIE_BTN  = "#accept-all-btn"
SEL_ZERO_H3     = "h3.busca-portal-title-text-1.busca-portal-dmb-10"
# Ejemplo de H3 (cuando no hay resultados):
# <h3 ...><strong> Aproximadamente <strong id="countResultados">0</strong> resultados encontrados</strong> para
#   <span id="infoTermo">jaider leonardo barrera chacon</span></h3>

SEL_LIST        = "ul#resultados.lista-resultados"
SEL_ITEM        = f"{SEL_LIST} .busca-portal-block-searchs__item"
# Dentro del item, el "nombre" puede estar en distintos tags; probamos varios comunes:
SEL_ITEM_TITLE_CANDIDATES = [
    "a[title]",                       # enlaces con title
    "a",                              # enlace principal
    "h2", "h3", "h4",                 # títulos
    ".titulo", ".title", ".nome",     # clases frecuentes
    "strong", "header"                # fallback
]

# Anti bloqueo: User-Agent y headers realistas
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
    return ("403 error" in h and "cloudfront" in h and "request blocked" in h)

def _norm(s: str) -> str:
    """Normaliza para comparación exacta: minúsculas, sin tildes, espacios comprimidos."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_portal_transparencia_leniencia(consulta_id: int, nombre: str, apellido: str):
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
    last_error = None

    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="pt-BR",
                timezone_id="America/Bogota",
                user_agent=REALISTIC_UA,
                extra_http_headers=EXTRA_HEADERS,
            )
            await context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                """
            )
            page = await context.new_page()

            # 3) Visitar dataset en ambos dominios (cookies / sesión)
            dataset_loaded = False
            for base in BASE_DOMAINS:
                url_dataset = base + PATH_DATASET
                try:
                    resp = await page.goto(url_dataset, timeout=GOTO_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                    html_tmp = await page.content()
                    status_ok = resp and resp.status == 200
                    if status_ok and not _is_cloudfront_block(html_tmp):
                        dataset_loaded = True
                except Exception:
                    continue
                # aceptar cookies si aparece
                try:
                    for _ in range(3):
                        btn = page.locator(SEL_COOKIE_BTN)
                        if await btn.count() > 0 and await btn.first.is_visible():
                            try:
                                await btn.first.click(timeout=5_000)
                                await page.wait_for_timeout(550)
                            except Exception:
                                pass
                        else:
                            break
                except Exception:
                    pass
                if dataset_loaded:
                    break

            # 4) Búsqueda en ambos dominios con detección de bloqueo
            q = urllib.parse.quote_plus(full_name)
            search_loaded = False
            search_html = ""
            for base in BASE_DOMAINS:
                search_url = base + PATH_SEARCH.format(q=q)
                try:
                    resp = await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)
                    await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=30_000)
                    except Exception:
                        pass
                    # cookies otra vez si reaparece
                    for _ in range(3):
                        try:
                            btn = page.locator(SEL_COOKIE_BTN)
                            if await btn.count() > 0 and await btn.first.is_visible():
                                await btn.first.click(timeout=5_000)
                                await page.wait_for_timeout(350)
                            else:
                                break
                        except Exception:
                            break
                    search_html = await page.content()
                    status_ok = resp and resp.status == 200
                    if status_ok and not _is_cloudfront_block(search_html):
                        search_loaded = True
                        break
                except Exception as ex:
                    last_error = str(ex)
                    continue

            if not search_loaded and _is_cloudfront_block(search_html):
                mensaje_final = "Bloqueado por CloudFront/WAF (403). Reintentos fallidos."
                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True
                score_final = 1
                try:
                    await navegador.close()
                except Exception:
                    pass
                navegador = None
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj,
                    score=score_final, estado="Validada",
                    mensaje=mensaje_final, archivo=relative_png
                )
                return

            # 5) Revisar si el H3 indica 0 resultados y usarlo como mensaje
            zero_h3 = page.locator(SEL_ZERO_H3)
            zero_h3_txt = ""
            try:
                if await zero_h3.count() > 0:
                    zero_h3_txt = (await zero_h3.first.inner_text()).strip()
            except Exception:
                zero_h3_txt = ""

            if zero_h3_txt and re.search(r"id=['\"]countResultados['\"][^>]*>0<", await zero_h3.first.inner_html()):
                # explícitamente 0 resultados => mensaje del H3 y score 1
                mensaje_final = zero_h3_txt
                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True

            else:
                # 6) Hay resultados (o al menos no dice 0): iterar lista
                items = page.locator(SEL_ITEM)
                n = await items.count()
                exact_hit = False

                for i in range(n):
                    item = items.nth(i)

                    # obtener algún título/nombre dentro del item
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

                    # fallback: texto completo del item si no encontramos título
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
                    # Si existe el H3 y no mostró 0, úsalo como parte del mensaje
                    if zero_h3_txt:
                        mensaje_final = f"{zero_h3_txt} Se encontraron resultados, pero sin coincidencia exacta del nombre."
                    else:
                        mensaje_final = "Se encontraron resultados, pero sin coincidencia exacta del nombre."

                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass

                success = True

            # 7) Cerrar navegador
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 8) Persistir Resultado
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
                mensaje=last_error or "No fue posible obtener resultados.",
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
