import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "ramajudicial_corte_constitucional_magistrados_anteriores"
URL = "https://www.ramajudicial.gov.co/web/corte-constitucional/portal/corporacion/magistrados/magistrados-anteriores"

# Selectores
SEL_INPUT = "#barra_busqueda"
RESULT_HINTS = [
    ".searchcontainer",  # el más específico cuando hay resultados
    "main .portlet-content-container .portlet-body",
    ".portlet-content-container .portlet-body",
    "main #content", "#content",
    "main .layout-content", ".layout-content",
    "main", "article"
]

WAIT_NAV   = 15000
WAIT_POST  = 2200
MAX_INTENTOS = 3

# ------------------ helpers de presentación ------------------ #
async def _clean_page_css(page):
    """
    Limpia la página para que el pantallazo quede centrado en el contenido
    (sin header/footer ni barras flotantes que lo tapen).
    """
    css = """
      /* esconder chrome y barras flotantes */
      header, footer, nav, .cookie-banner, #onetrust-banner-sdk,
      .eu-cookie-compliance, .eu-cookie-compliance-banner,
      .portal-top, .portal-bottom, .portlet-breadcrumb, .portlet-topper,
      .banner, .toolbar, .sticky-header, .stick-header,
      [class*="accesi"], [id*="accesi"], .accesibilidad, .accesibilidad-flotante,
      .social, .socialbar, .fab, .floating, .float, .chatbot, .boton-flotante {
        display: none !important;
        visibility: hidden !important;
        height: 0 !important;
        overflow: hidden !important;
      }
      /* evita que algo fixed tape el contenido */
      *[style*="position:fixed"] { position: static !important; }
      html, body { margin:0 !important; padding:0 !important; }
      main, #content, .layout-content, .portlet-content-container .portlet-body {
        max-width: 100% !important;
      }
    """
    try:
        await page.add_style_tag(content=css)
    except Exception:
        pass
    try:
        await page.evaluate("window.scrollTo(0,0)")
    except Exception:
        pass

def _norm_rel(path: str) -> str:
    """Evita 404 en Django cuando corre en Windows: usa slashes forward."""
    return path.replace("\\", "/")

async def expandir_y_capturar(page, abs_png: str) -> bool:
    """
    Intenta capturar el contenido COMPLETO:
    - Quita overflow/alturas en contenedores comunes
    - Expande a scrollHeight
    - Toma screenshot del primer contenedor que exista
    - Fallback: página completa
    """
    # 1) intentar cada selector "candidato"
    for sel in RESULT_HINTS:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue

            # Remueve restricciones (overflow/max-height) del contenedor y sus padres
            await page.evaluate("""
            sel => {
              const el = document.querySelector(sel);
              if (!el) return false;
              const relax = (node) => {
                if (!node || node === document.documentElement) return;
                const cs = getComputedStyle(node);
                // si recorta o scrollea, abre
                if (['hidden','auto','scroll','clip'].includes(cs.overflowY) || cs.maxHeight !== 'none') {
                  node.style.overflow = 'visible';
                  node.style.overflowY = 'visible';
                  node.style.maxHeight = 'none';
                }
                // quita transforms que puedan cortar screenshots
                if (cs.transform !== 'none') node.style.transform = 'none';
                relax(node.parentElement);
              };
              relax(el.parentElement);

              // expande el propio elemento
              el.style.overflow = 'visible';
              el.style.overflowY = 'visible';
              el.style.maxHeight = 'none';
              const h = el.scrollHeight;
              if (h > 0) el.style.height = h + 'px';

              // vuelve al tope para que no haya saltos
              window.scrollTo(0,0);
              return true;
            }
            """, sel)

            # pequeño delay para que el layout se re-pinte
            await asyncio.sleep(0.2)

            # 2) screenshot del elemento expandido
            await loc.screenshot(path=abs_png, type="png")
            return True
        except Exception:
            continue

    # 3) último recurso: página completa
    try:
        await page.screenshot(path=abs_png, full_page=True, type="png")
        return True
    except Exception:
        return False
# ------------------------------------------------------------- #

async def consultar_ramajudicial_corte_constitucional_magistrados_anteriores(
    consulta_id: int,
    nombre: str,
    apellido: str = "",
):
    """
    Bot con reintentos y solo dos estados:
      - "Validado"  cuando consulta exitosa
      - "Sin validar" cuando falla incluso tras 3 intentos
    """
    # 1) Buscar fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    # 2) Preparar carpeta de resultados
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    query = (" ".join([(nombre or "").strip(), (apellido or "").strip()]) or "consulta").strip()
    safe_q = re.sub(r"\s+", "_", query)

    intento = 0
    exito = False
    mensaje_error = ""
    rel_png = ""

    while intento < MAX_INTENTOS and not exito:
        intento += 1
        browser = None
        ctx = None
        page = None
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            png_name = f"{NOMBRE_SITIO}_{safe_q}_{ts}_try{intento}.png"
            abs_png  = os.path.join(absolute_folder, png_name)
            rel_png  = _norm_rel(os.path.join(relative_folder, png_name))

            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled", "--start-maximized"]
                )
                ctx = await browser.new_context(
                    viewport={"width": 1680, "height": 1800},
                    device_scale_factor=2,
                    locale="es-CO"
                )
                page = await ctx.new_page()

                # Navegar
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=WAIT_NAV)
                except Exception:
                    pass

                # Buscar campo y escribir
                await page.wait_for_selector(SEL_INPUT, state="visible", timeout=20000)
                inp = page.locator(SEL_INPUT).first
                await inp.click()
                await inp.fill("")
                await inp.type(query, delay=25)
                await inp.press("Enter")

                # Esperar resultado / estabilización de DOM
                try:
                    await page.wait_for_load_state("networkidle", timeout=WAIT_NAV)
                except Exception:
                    pass
                await asyncio.sleep(WAIT_POST / 1000)

                # Limpiar chrome visual
                await _clean_page_css(page)

                # -------- Capturar sin cortes --------
                tomado = await expandir_y_capturar(page, abs_png)

                # Cerrar contextos
                await ctx.close()
                await browser.close()
                browser, ctx, page = None, None, None

            if not tomado:
                raise RuntimeError("No fue posible capturar el pantallazo sin recortes.")

            # Éxito
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Validado",
                mensaje="No se encuentran coincidencias",
                archivo=rel_png,
            )
            exito = True

        except Exception as e:
            mensaje_error = str(e)
            # Guardar pantallazo de último estado si es posible
            try:
                if page:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    error_png = os.path.join(absolute_folder, f"ERROR_{safe_q}_{ts}_try{intento}.png")
                    await page.screenshot(path=error_png, full_page=True)
                    rel_png = _norm_rel(os.path.join(relative_folder, os.path.basename(error_png)))
            except Exception:
                pass
            finally:
                # cierra con seguridad
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

    # Si tras 3 intentos no se logró
    if not exito:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje=f"Fallo tras {MAX_INTENTOS} intentos: {mensaje_error}",
            archivo=rel_png,
        )
