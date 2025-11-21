# bots/portal_transparencia_leniencia.py
import os, re, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "portal_transparencia_leniencia"  # agrega este nombre en tu tabla Fuente

# Página informativa del dataset (referencia); la búsqueda real usa /busca?termo=
URL_DATASET = "https://portaldatransparencia.gov.br/download-de-dados/acordos-leniencia"
URL_SEARCH  = "https://portaldatransparencia.gov.br/busca?termo={q}"

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
                headless=True,  # visible, como sueles trabajar
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="pt-BR",
                timezone_id="America/Bogota",
            )
            page = await context.new_page()

            # 3) Ir primero al dataset (por si dispara banner de cookies) y aceptar cookies
            try:
                await page.goto(URL_DATASET, timeout=GOTO_TIMEOUT_MS)
                await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                # aceptar cookies si aparece (una o varias veces)
                for _ in range(3):
                    btn = page.locator(SEL_COOKIE_BTN)
                    if await btn.count() > 0 and await btn.first.is_visible():
                        try:
                            await btn.first.click(timeout=5_000)
                            # dar un respiro para que desaparezca
                            await page.wait_for_timeout(600)
                        except Exception:
                            pass
                    else:
                        break
            except Exception:
                # si falla, seguimos de largo a la búsqueda
                pass

            # 4) Ir a la URL de búsqueda con el nombre
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)
            await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

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
