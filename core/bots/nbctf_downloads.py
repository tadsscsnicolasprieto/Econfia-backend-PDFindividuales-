# bots/nbctf_downloads.py
import os, re, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO   = "nbctf_downloads"
# Restringimos la búsqueda a la página que pediste (u=)
U_SCOPE        = "https://nbctf.mod.gov.il/en/Minister%20Sanctions/Designation/Pages/downloads.aspx"
URL_SEARCH_TPL = (
    "https://nbctf.mod.gov.il/en/pages/searchResults.aspx"
    "?u={u}&k={q}"
)
GOTO_TIMEOUT_MS = 180_000

# Selectores (SharePoint)
SEL_NORES             = "div.ms-textLarge.ms-srch-result-noResultsTitle"  # "Nothing here matches your search"
SEL_ITEM_BODY_STRICT  = "div#ctl00_ctl81_g_1363b887_176a_4e5a_aafb_40a57fcdc3de_csr2_item_itemBody"
SEL_ITEM_BODY_LOOSE   = "div[id$='_csr2_item_itemBody']"  # fallback robusto

def _norm(s: str) -> str:
    """Normaliza: minúsculas, sin tildes, espacios comprimidos."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_nbctf_downloads(consulta_id: int, nombre: str, apellido: str):
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
    last_error = None
    score_final = 1
    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await navegador.new_context(
                viewport={"width": 1400, "height": 900},
                locale="en-US",
                timezone_id="America/Bogota",
            )
            page = await context.new_page()

            # 3) Construir URL de búsqueda con u=descargas y k=full_name
            search_url = URL_SEARCH_TPL.format(
                u=urllib.parse.quote(U_SCOPE, safe=""),
                q=urllib.parse.quote_plus(full_name)
            )

            await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

            # 4) Sin resultados
            nores = page.locator(SEL_NORES, has_text="Nothing here matches your search")
            if await nores.count() > 0 and await nores.first.is_visible():
                try:
                    mensaje_final = (await nores.first.inner_text()).strip()
                except Exception:
                    mensaje_final = "Nothing here matches your search"
                try:
                    await page.screenshot(path=absolute_png, full_page=True)
                except Exception:
                    pass
                success = True  # consulta válida, sin hallazgos (score 1)

            else:
                # 5) Con resultados: iterar contenedores
                items = page.locator(SEL_ITEM_BODY_STRICT)
                if await items.count() == 0:
                    items = page.locator(SEL_ITEM_BODY_LOOSE)

                n = await items.count()
                exact_hit = False

                for i in range(n):
                    item = items.nth(i)
                    try:
                        blob = (await item.inner_text(timeout=3_000)).strip()
                    except Exception:
                        blob = ""

                    if not blob:
                        continue

                    # Búsqueda exacta por líneas (título/encabezado suele ir en su propia línea)
                    for line in re.split(r"[\r\n]+", blob):
                        if _norm(line) == norm_query:
                            exact_hit = True
                            break
                    if exact_hit:
                        break

                if exact_hit:
                    score_final = 5
                    mensaje_final = f"Coincidencia exacta con el nombre buscado: '{full_name}'."
                else:
                    score_final = 1
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

        # 7) Guardar Resultado
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
