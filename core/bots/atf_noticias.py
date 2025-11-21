# core/bots/atf_noticias.py
import os
import re
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente

nombre_sitio = "atf_noticias"

NO_RESULTS_TEXT = "No hay resultados de noticias para los filtros que ha seleccionado"
RESULTS_COUNT_SEL = ".results-count, .results-count-wrapper .results-count"

# --- helpers de presentación / paths ---
async def _limpiar_cromado(page):
    """Oculta headers/footers/banners para que el screenshot enfoque el contenido."""
    try:
        await page.add_style_tag(content="""
            header, footer,
            .usa-banner, .usa-footer, .site-footer, .l-footer,
            #footer, .region-footer, .region--footer,
            #onetrust-banner-sdk, .ot-sdk-container,
            .cookie-banner, .eu-cookie-compliance-banner,
            .sticky-footer, .sticky-header {
                display: none !important;
                visibility: hidden !important;
                height: 0 !important; min-height: 0 !important;
                overflow: hidden !important;
            }
        """)
    except Exception:
        pass
    try:
        await page.evaluate("""
        () => {
          const sels = [
            '#onetrust-banner-sdk','.ot-sdk-container',
            '.cookie-banner','.eu-cookie-compliance-banner'
          ];
          for (const s of sels) {
            const n = document.querySelector(s);
            if (n) n.remove();
          }
        }
        """)
    except Exception:
        pass

def _norm_rel(path: str) -> str:
    """Evita 404 en Windows al servir /media con Django."""
    return path.replace("\\", "/")

async def consultar_atf_noticias(consulta_id: int, nombre: str):
    navegador = None
    fuente_obj = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{nombre_sitio}': {e}",
            archivo=""
        )
        return

    try:
        nombre_q = (nombre or "").strip().replace(" ", "+")
        url = (
            "https://www.atf.gov/es/news/press-releases"
            f"?combine={nombre_q}"
            "&field_field_division_target_id=All"
            "&field_news_type_target_id_1=All"
            "&field_date_published_value=All"
        )

        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            # viewport amplio para nitidez
            ctx = await navegador.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
            pagina = await ctx.new_page()
            await pagina.goto(url, wait_until="domcontentloaded")

            try:
                await pagina.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # limpia UI ruidosa y fuerza cargar lazy content
            await _limpiar_cromado(pagina)
            try:
                await pagina.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await pagina.wait_for_timeout(300)
                await pagina.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass

            # --- detectar mensaje / conteo de resultados ---
            mensaje = ""
            score = 0

            # 1) Mensaje "no hay resultados" (tolerante)
            nores_visible = False
            try:
                nores = pagina.get_by_text("No hay resultados", exact=False)
                nores_visible = await nores.is_visible()
            except Exception:
                nores_visible = False

            if nores_visible:
                mensaje = f"{NO_RESULTS_TEXT}. Seleccione un filtro diferente arriba."
                score = 0
            else:
                # 2) Intentar contador "X resultados"
                count_text = ""
                try:
                    cnt = pagina.locator(RESULTS_COUNT_SEL).first
                    if await cnt.count() > 0 and await cnt.is_visible():
                        count_text = (await cnt.inner_text() or "").strip()
                except Exception:
                    pass

                if count_text:
                    # si prefieres mostrar el conteo exacto, usa: mensaje = count_text
                    mensaje = "se han encontrado hallazgos"
                    score = 10
                else:
                    # 3) Fallback: ¿existen tarjetas/listado de resultados?
                    found_items = 0
                    for sel in [
                        ".view-content .views-row",
                        ".search-results .result-item",
                        "ul.search-results li",
                        "article.node--type-news-release"
                    ]:
                        try:
                            found_items = await pagina.locator(sel).count()
                            if found_items:
                                break
                        except Exception:
                            continue
                    if found_items > 0:
                        mensaje = "se han encontrado hallazgos"
                        score = 10
                    else:
                        # si no hay contador ni items ni mensaje explícito, asumimos hallazgos por prudencia
                        mensaje = "se han encontrado hallazgos"
                        score = 10

            # --- preparar carpeta / screenshot ---
            relative_folder = os.path.join("resultados", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_nombre = (nombre or "").strip().replace(" ", "_") or "consulta"
            screenshot_name = f"{nombre_sitio}_{safe_nombre}_{timestamp}.png"
            absolute_path = os.path.join(absolute_folder, screenshot_name)
            relative_path = _norm_rel(os.path.join(relative_folder, screenshot_name))

            # ***** CAPTURA CONTENIDO COMPLETO (como recompensas) *****
            tomado = False
            for sel in ["main", "article", "#block-atf-content", ".region-content", "#content", ".l-main"]:
                try:
                    loc = pagina.locator(sel).first
                    if await loc.count() > 0:
                        await loc.screenshot(path=absolute_path, type="png")
                        tomado = True
                        break
                except Exception:
                    continue
            if not tomado:
                await pagina.screenshot(path=absolute_path, full_page=True, type="png")

            await ctx.close()
            await navegador.close()
            navegador = None

        # Registrar
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score,
            estado="Validada",
            mensaje=mensaje,
            archivo=relative_path
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=str(e),
                archivo=""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
