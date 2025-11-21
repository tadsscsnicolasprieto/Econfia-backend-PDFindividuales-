import os
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente

url = "https://www.libretamilitar.mil.co/modules/consult/militarycardcertificate"
nombre_sitio = "libreta_militar"

TIPO_DOC_MAP = {
    'CC': '100000001',
    'TI': '100000000',
    'NIUP': '100000002'
}

# Mensaje fallback cuando no se puede leer el texto del portal
NOT_FOUND_FALLBACK = (
    "El Ciudadano consultado no se encuentra en nuestro sistema. "
    "Si su Libreta Militar no se encontró o fue expedida antes del año de 1990, "
    "por favor ingrese a Contáctenos e infórmenos para solicitar la consulta manual de su documento."
)

# ----- Opcional: Poppler (para pdf2image en Windows) -----
POPPLER_PATH = getattr(settings, "POPPLER_PATH", os.getenv("POPPLER_PATH"))

# ---------- Render helpers (SOLO documento) ----------
def _render_pdf_primera_pagina_pymupdf(path_pdf: str, path_png: str, zoom: float = 2.0) -> bool:
    """Render nítido con PyMuPDF (preferido)."""
    try:
        import fitz  # PyMuPDF
        with fitz.open(path_pdf) as doc:
            if doc.page_count == 0:
                return False
            pg = doc[0]
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pix.save(path_png)
        return os.path.exists(path_png) and os.path.getsize(path_png) > 0
    except Exception:
        return False

def _render_pdf_primera_pagina_pdf2image(path_pdf: str, path_png: str, dpi: int = 300) -> bool:
    """Render con pdf2image (requiere Poppler)."""
    try:
        from pdf2image import convert_from_path
        kwargs = {"dpi": dpi, "first_page": 1, "last_page": 1}
        if POPPLER_PATH:
            kwargs["poppler_path"] = POPPLER_PATH
        imgs = convert_from_path(path_pdf, **kwargs)
        if imgs:
            imgs[0].save(path_png, "PNG")
            return True
        return False
    except Exception:
        return False

async def _screenshot_pdf_embed(page, path_png: str) -> bool:
    """
    Fallback: capturar el <embed> del visor PDF (sin miniaturas/toolbar).
    Sirve cuando el PDF se abre en un popup/pestaña con el visor de Chrome.
    """
    try:
        embed = page.locator("embed#pdf-embed, embed[type='application/x-google-chrome-pdf'], embed[type*='pdf']").first
        await embed.wait_for(state="visible", timeout=12000)
        await embed.screenshot(path=path_png)
        return True
    except Exception:
        return False

# ---------- Lanzador robusto para headless ----------
async def _launch_browser(p):
    """
    Intenta lanzar Chrome real en headless; si no está disponible, cae a chromium.
    """
    args_comunes = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1366,900",
    ]
    # 1) Intentar canal chrome
    try:
        return await p.chromium.launch(headless=True, channel="chrome", args=args_comunes)
    except Exception:
        # 2) Fallback: chromium por defecto
        return await p.chromium.launch(headless=True, args=args_comunes)

async def consultar_libreta_militar(consulta_id: int, cedula: str, tipo_doc: str):
    navegador = None
    ctx = None
    fuente_obj = None

    # Buscar la fuente
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
        # Validar tipo de documento
        tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())
        if not tipo_doc_val:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=f"Tipo de documento no soportado: {tipo_doc}",
                archivo=""
            )
            return

        # Carpeta resultados/<consulta_id>
        relative_folder = os.path.join('resultados', str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"{nombre_sitio}_{cedula}_{timestamp}"
        png_name = f"{base}.png"
        pdf_name = f"{base}.pdf"

        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name).replace("\\", "/")
        abs_pdf = os.path.join(absolute_folder, pdf_name)

        async with async_playwright() as p:
            # ===== Navegador y contexto (HEADLESS con stealth) =====
            navegador = await _launch_browser(p)

            ctx = await navegador.new_context(
                accept_downloads=True,
                viewport={"width": 1366, "height": 900},
                locale="es-CO",
                timezone_id="America/Bogota",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
                ),
            )

            # Stealth básico
            await ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => false});
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
                Object.defineProperty(navigator, 'languages', { get: () => ['es-CO','es','en'] });
                const origQuery = window.navigator.permissions && window.navigator.permissions.query;
                if (origQuery) {
                  window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications'
                      ? Promise.resolve({ state: Notification.permission })
                      : origQuery(parameters)
                  );
                }
            """)

            # Tracing para depurar si algo falla en headless
            trace_path = os.path.join(absolute_folder, "trace.zip")
            try:
                await ctx.tracing.start(screenshots=True, snapshots=True, sources=True)
            except Exception:
                pass  # si tracing no está disponible, continuar

            pagina = await ctx.new_page()
            pagina.set_default_timeout(45000)

            await pagina.goto(url, wait_until="networkidle")

            # Cerrar posibles banners/cookies/modales que tapen el formulario
            for sel in [
                "button#onetrust-accept-btn-handler",
                "button:has-text('Aceptar')",
                "button:has-text('Aceptar todo')",
                "div[role='dialog'] button:has-text('Cerrar')",
            ]:
                try:
                    loc = pagina.locator(sel)
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click()
                except Exception:
                    pass

            # Helper para encontrar el select en la página o dentro de iframes
            async def _find_select_and_host():
                sel = "select#ctl00_MainContent_drpDocumentType"
                # principal
                if await pagina.locator(sel).count() > 0:
                    return pagina, pagina.locator(sel)
                # iframes
                for fr in pagina.frames:
                    try:
                        loc = fr.locator(sel)
                        if await loc.count() > 0:
                            return fr, loc
                    except Exception:
                        continue
                return None, None

            host, select_loc = await _find_select_and_host()
            if not select_loc:
                # espera activa a que el select exista
                await pagina.wait_for_function(
                    """() => !!document.querySelector('select#ctl00_MainContent_drpDocumentType')""",
                    timeout=30000,
                )
                host, select_loc = await _find_select_and_host()

            # Asegurar visible y con opciones cargadas
            await select_loc.wait_for(state="visible", timeout=30000)
            await (host or pagina).wait_for_function(
                """() => {
                    const el = document.querySelector('select#ctl00_MainContent_drpDocumentType');
                    return el && el.options && el.options.length > 1;
                }""",
                timeout=20000,
            )

            # ===== Rellenar formulario =====
            await select_loc.scroll_into_view_if_needed()
            await select_loc.select_option(value=tipo_doc_val)

            num_loc = (host or pagina).locator('input#ctl00_MainContent_txtNumberDocument')
            await num_loc.wait_for(state="visible", timeout=20000)
            await num_loc.fill(str(cedula))

            btn = (host or pagina).locator('input#ctl00_MainContent_btnGenerate')
            await btn.wait_for(state="visible", timeout=20000)
            await btn.click()

            # Esperar a que cargue resultado (éxito o error)
            await (host or pagina).wait_for_timeout(1200)
            await (host or pagina).wait_for_load_state("networkidle", timeout=20000)

            # ----- Detección de estado y mensajes -----
            score_final = 0
            mensaje_final = "El ciudadano cuenta con tarjeta militar"  # por defecto: hallazgo (existe)

            try:
                error_box = (host or pagina).locator("#divErrorMessages").first
                if await error_box.count() > 0 and await error_box.is_visible():
                    # NO encontrado
                    span_error = (host or pagina).locator("#ctl00_MainContent_lblError").first
                    if await span_error.count() > 0:
                        raw_text = (await span_error.inner_text() or "").strip()
                    else:
                        raw_text = (await error_box.inner_text() or "").strip()
                    raw_text = " ".join(raw_text.replace("×", " ").split()) or NOT_FOUND_FALLBACK
                    mensaje_final = raw_text
                    score_final = 1
            except Exception:
                pass
            # ------------------------------------------

            # Si NO hay error, intentamos ver/descargar el certificado
            if score_final == 0:
                btn_ver = (host or pagina).locator('#ctl00_MainContent_imgBtnSeeCertificate').first
                try:
                    if await btn_ver.count() > 0 and await btn_ver.is_enabled():
                        # Primero intentamos descarga directa
                        try:
                            async with pagina.expect_download(timeout=25000) as dl_info:
                                await btn_ver.click()
                            download = await dl_info.value
                            await download.save_as(abs_pdf)

                            # Render SOLO documento
                            if _render_pdf_primera_pagina_pymupdf(abs_pdf, abs_png, zoom=2.0) or \
                               _render_pdf_primera_pagina_pdf2image(abs_pdf, abs_png, dpi=300):
                                pass
                            else:
                                # Como último recurso, abrir file:// y capturar <embed>
                                viewer = await ctx.new_page()
                                await viewer.goto(Path(abs_pdf).resolve().as_uri(), wait_until="load")
                                await viewer.wait_for_timeout(700)
                                ok = await _screenshot_pdf_embed(viewer, abs_png)
                                await viewer.close()
                                if not ok:
                                    # fallback final: screenshot general de la página (no ideal)
                                    await pagina.screenshot(path=abs_png, full_page=True)
                        except Exception:
                            # Puede que se abra en popup el visor PDF
                            try:
                                await btn_ver.click()
                                popup = await pagina.wait_for_event("popup", timeout=15000)
                                await popup.wait_for_load_state()
                                await popup.wait_for_timeout(800)
                                ok = await _screenshot_pdf_embed(popup, abs_png)
                                if not ok:
                                    await popup.screenshot(path=abs_png, full_page=True)
                                await popup.close()
                            except Exception:
                                # Último recurso: capturar el contenedor principal
                                cont = (host or pagina).locator('div.container-fluid[style*="min-height: 40vh;"]').first
                                if await cont.count() > 0:
                                    await cont.screenshot(path=abs_png)
                                else:
                                    await pagina.screenshot(path=abs_png, full_page=True)
                    else:
                        # No hay botón de certificado; capturar resultado visible
                        cont = (host or pagina).locator('div.container-fluid[style*="min-height: 40vh;"]').first
                        if await cont.count() > 0:
                            await cont.screenshot(path=abs_png)
                        else:
                            await pagina.screenshot(path=abs_png, full_page=True)
                except Exception:
                    # Si algo falla en el flujo del PDF, deja evidencia general
                    await pagina.screenshot(path=abs_png, full_page=True)

            else:
                # Caso NO encontrado: capturamos el error
                try:
                    contenedor = (host or pagina).locator("#divErrorMessages").first
                    if await contenedor.count() > 0:
                        await contenedor.screenshot(path=abs_png)
                    else:
                        await pagina.screenshot(path=abs_png, full_page=True)
                except Exception:
                    await pagina.screenshot(path=abs_png, full_page=True)

            # Cerrar navegador
            try:
                await ctx.tracing.stop(path=trace_path)
            except Exception:
                pass
            await navegador.close()
            navegador = None
            ctx = None

        # Registrar resultado (apuntamos SIEMPRE al PNG generado)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=rel_png
        )

    except Exception as e:
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
            if ctx is not None:
                # guardar trace si aún estaba activo
                try:
                    await ctx.tracing.stop(path=os.path.join(absolute_folder, "trace.zip"))
                except Exception:
                    pass
                await ctx.close()
        except Exception:
            pass
        try:
            if navegador is not None:
                await navegador.close()
        except Exception:
            pass
