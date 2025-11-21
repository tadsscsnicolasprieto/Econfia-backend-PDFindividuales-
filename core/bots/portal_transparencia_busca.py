# bots/portal_transparencia_busca.py
import os, re, unicodedata, urllib.parse
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "portal_transparencia_busca"  # agrega en tu tabla Fuente
URL_SEARCH   = "https://portaldatransparencia.gov.br/busca?termo={q}"
GOTO_TIMEOUT_MS = 180_000

# Cookies
SEL_COOKIE_BTN = "#accept-all-btn"

# Encabezado que muestra "Aproximadamente X resultados..."
SEL_H3_SUMMARY = "h3.busca-portal-title-text-1.busca-portal-dmb-10"
SEL_COUNT      = "#countResultados"   # dentro del H3
SEL_TERM       = "#infoTermo"         # dentro del H3

# Lista de resultados e items
SEL_LIST  = "ul#resultados.lista-resultados"
SEL_ITEM  = f"{SEL_LIST} .busca-portal-block-searchs__item"

# Candidatos de título/nombre dentro de cada item
SEL_ITEM_TITLE_CANDS = [
    "h2 a", "h3 a", "a[title]", "a",
    "h2", "h3", "h4",
    "strong", ".titulo", ".title", ".nome", ".busca-portal-text-1", "header"
]

def _norm(s: str) -> str:
    """Normaliza texto para comparación exacta: minúsculas, sin tildes, espacios comprimidos."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_portal_transparencia_busca(consulta_id: int, nombre: str, apellido: str):
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
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="pt-BR",
                timezone_id="America/Bogota",
            )
            page = await context.new_page()

            # 3) Ir directo a la búsqueda por URL
            q = urllib.parse.quote_plus(full_name)
            url = URL_SEARCH.format(q=q)
            await page.goto(url, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)

            # 4) Aceptar cookies si aparece (puede salir repetida)
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

            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

            # 5) Leer el encabezado resumen (si existe)
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

            # 6) Si el contador dice 0 -> usamos h3 como mensaje y score=1
            if (count_text or "").strip() == "0":
                mensaje_final = h3_text or f"Aproximadamente 0 resultados encontrados para {term_text}"
                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True

            else:
                # 7) Iterar resultados y buscar coincidencia exacta del nombre
                items = page.locator(SEL_ITEM)
                n = await items.count()
                exact = False

                for i in range(n):
                    it = items.nth(i)
                    item_title = ""

                    # probar candidatos
                    for sel in SEL_ITEM_TITLE_CANDS:
                        loc = it.locator(sel).first
                        try:
                            if await loc.count() > 0 and await loc.is_visible():
                                item_title = (await loc.inner_text(timeout=2000)).strip()
                                if item_title:
                                    break
                        except Exception:
                            continue

                    if not item_title:
                        # fallback: todo el texto del item
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

            # 8) Cerrar navegador
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # 9) Guardar Resultado
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
