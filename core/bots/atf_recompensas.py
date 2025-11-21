# core/bots/atf_recompensas.py
import os, re
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

nombre_sitio = "atf_recompensas"

# --- helper opcional: limpia banners/encabezados/pie ---
async def _limpiar_cromado(page):
    # cierra/oculta banners típicos de cookies y header/footer pegajosos
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
    # elimina nodos conocidos si existen
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

async def consultar_atf_recompensas(consulta_id: int, nombre: str):
    nombre_q = (nombre or "").strip().replace(" ", "+")
    navegador = None
    fuente_obj = None

    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    except Exception:
        fuente_obj = None  # igual registramos el resultado

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            # viewport amplio para que el render sea nítido
            ctx = await navegador.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
            pagina = await ctx.new_page()

            await pagina.goto(
                f"https://www.atf.gov/es/news/reward-notices"
                f"?combine={nombre_q}"
                f"&field_field_division_target_id=All"
                f"&field_date_published_value=All",
                wait_until="domcontentloaded"
            )
            try:
                await pagina.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            # limpia UI ruidosa y fuerza carga perezosa desplazando
            await _limpiar_cromado(pagina)
            try:
                await pagina.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await pagina.wait_for_timeout(300)
                await pagina.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass

            # --- prepara carpeta / screenshot ---
            relative_folder = os.path.join("resultados", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_name = f"{nombre_sitio}_{consulta_id}_{ts}.png"
            abs_path = os.path.join(absolute_folder, screenshot_name)
            rel_path = os.path.join(relative_folder, screenshot_name).replace("\\", "/")

            # ***** CAPTURA CONTENIDO COMPLETO (sin cortar) *****
            # Preferimos el <main>. Si no existe, probamos alternativas y luego full_page.
            tomado = False
            for sel in ["main", "article", "#block-atf-content", ".region-content", "#content", ".l-main"]:
                try:
                    loc = pagina.locator(sel).first
                    if await loc.count() > 0:
                        # locator.screenshot hace stitch automático de todo el bounding box (aunque sea más alto que el viewport)
                        await loc.screenshot(path=abs_path, type="png")
                        tomado = True
                        break
                except Exception:
                    continue

            if not tomado:
                # Fallback: toda la página
                await pagina.screenshot(path=abs_path, full_page=True, type="png")

            # --- detectar mensaje / conteo de resultados ---
            mensaje = ""
            score = 0

            # 1) Intentar contador tipo "1 resultado" / "X resultados"
            try:
                cnt = pagina.locator(".results-count, .results-count-wrapper .results-count").first
                if await cnt.count() > 0:
                    txt = (await cnt.inner_text() or "").strip()
                    m = re.search(r"(\d+)\s+resultado", txt, flags=re.I)
                    if m:
                        n = int(m.group(1))
                        mensaje = txt
                        score = 10 if n > 0 else 0
            except Exception:
                pass

            # 2) Mensaje explícito "No hay resultados..."
            if not mensaje:
                nores = pagina.locator("xpath=//*[contains(text(),'No hay resultados de Avisos de recompensa')]").first
                if await nores.count() > 0:
                    mensaje = (await nores.inner_text() or "").strip()
                    score = 0

            # 3) Fallback por HTML
            if not mensaje:
                html = await pagina.content()
                m2 = re.search(r"No hay resultados de Avisos de recompensa[^<]+", html, flags=re.I)
                if m2:
                    mensaje = m2.group(0).strip()
                    score = 0

            # 4) Último recurso
            if not mensaje:
                mensaje = "Se han encontrado hallazgos"
                score = 10

            await ctx.close()
            await navegador.close()
            navegador = None

        # Registrar en BD
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score,
            estado="Validada",
            mensaje=mensaje,
            archivo=rel_path,
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=str(e),
                archivo="",
            )
        finally:
            if navegador:
                try:
                    await navegador.close()
                except Exception:
                    pass
