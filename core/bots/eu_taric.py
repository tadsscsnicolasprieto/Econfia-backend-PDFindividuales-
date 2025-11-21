# consulta/eu_taric.py
import os, re, asyncio, html
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://ec.europa.eu/taxation_customs/dds2/taric/taric_consultation.jsp?Lang=en"
NOMBRE_SITIO = "eu_taric"

async def consultar_eu_taric(consulta_id: int, nombre_completo: str):
    navegador = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    nombre_completo = (nombre_completo or "").strip()
    if not nombre_completo:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje="Nombre vacío para la consulta.", archivo=""
        )
        return

    # 2) Rutas de salida
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = re.sub(r"\s+", "_", nombre_completo) or "consulta"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    img_name = f"{NOMBRE_SITIO}_{safe}_{ts}.png"
    absolute_path = os.path.join(absolute_folder, img_name)
    relative_path = os.path.join(relative_folder, img_name)

    score_final = 0
    mensaje_final = "No results found"  # default

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            page = await navegador.new_page()

            # 3.1 Abrir
            await page.goto(URL, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # 3.2 Cookies (best-effort)
            for sel in [
                "button:has-text('Accept all cookies')",
                "button:has-text('I accept')",
                "button#accept-all-cookies",
                "button.ecl-button--primary:has-text('Accept')",
            ]:
                try:
                    await page.locator(sel).first.click(timeout=1500)
                    break
                except Exception:
                    pass

            # 3.3 Buscar
            await page.wait_for_selector("#search-input-id", timeout=15000)
            await page.fill("#search-input-id", nombre_completo)
            await page.locator('button[aria-label="Search"]').first.click()

            # 3.4 Esperar resultados o mensaje de no-coincidencia
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            await asyncio.sleep(1.0)

            # ---------- 1) ¿Mensaje explícito "No results matching the search terms ..." ? ----------
            # Ejemplo:
            # <div class="ecl-tag ecl-tag--facet-close ecl-u-mv-xxxs">
            #   <p class="ecl-u-mv-xxxs">No results matching the search terms <b>xxx</b> using the selected options. ...</p>
            # </div>
            nores_locator = page.locator("div.ecl-tag.ecl-tag--facet-close p.ecl-u-mv-xxxs").first
            tiene_mensaje_nores = False
            try:
                if await nores_locator.count() > 0 and await nores_locator.is_visible():
                    # Queremos el MENSAJE con etiquetas escapadas (&lt;b&gt;...&lt;/b&gt;)
                    inner_html = (await nores_locator.inner_html()) or ""
                    inner_html = inner_html.strip()
                    # escapar todo el HTML para que queden las etiquetas como entidades
                    mensaje_final = html.escape(inner_html, quote=False)
                    score_final = 0
                    tiene_mensaje_nores = True
            except Exception:
                pass

            # ---------- 2) Si NO hubo mensaje explícito, inspeccionamos la lista de resultados ----------
            if not tiene_mensaje_nores:
                # El contenedor principal
                cont = page.locator("section.ecl-col-lg-9")
                # Títulos de cada resultado (anchor con clase ecl-link dentro del listado)
                # Nota: filtramos los anchors que no sean de paginación.
                anchors = cont.locator("a.ecl-link").filter(
                    has_not=page.locator(".ecl-pager__link, .ecl-pager__item")
                )

                textos = []
                try:
                    n = await anchors.count()
                    for i in range(n):
                        try:
                            t = (await anchors.nth(i).inner_text()).strip()
                            if t:
                                textos.append(t)
                        except Exception:
                            pass
                except Exception:
                    textos = []

                if textos:
                    # ¿Existe coincidencia EXACTA con el nombre buscado?
                    hay_match_exacto = any(t.strip() == nombre_completo for t in textos)
                    if hay_match_exacto:
                        score_final = 10
                        mensaje_final = "se encontraron coincidencias"
                    else:
                        score_final = 0
                        mensaje_final = "no se encontraron coincidencias"
                else:
                    # Sin lista => tratar como no hay resultados
                    score_final = 0
                    mensaje_final = "no se encontraron coincidencias"

            # 3.5 Captura
            try:
                await page.screenshot(path=absolute_path, full_page=True)
            except Exception:
                pass

            await navegador.close()
            navegador = None

        # 4) Registrar
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=score_final,
            estado="Validada", mensaje=mensaje_final, archivo=relative_path
        )

    except Exception as e:
        try:
            if navegador is not None:
                await navegador.close()
        except Exception:
            pass
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje=str(e), archivo=""
        )
