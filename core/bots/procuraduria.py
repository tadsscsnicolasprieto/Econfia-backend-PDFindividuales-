import os
import re
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente  # ajusta import según tu proyecto

PAGE_URL = "https://www.procuraduria.gov.co/Pages/Consulta-de-Antecedentes.aspx"
nombre_sitio = "procuraduria"

TIPO_DOC_MAP = {
    'CC': '1',
    'PEP': '0',
    'NIT': '2',
    'CE': '5',
    'PPT': '10'
}

PREGUNTAS_RESPUESTAS = {
    '¿ Cuanto es 9 - 2 ?': '7',
    '¿ Cuanto es 3 X 3 ?': '9',
    '¿ Cuanto es 6 + 2 ?': '8',
    '¿ Cuanto es 2 X 3 ?': '6',
    '¿ Cuanto es 3 - 2 ?': '1',
    '¿ Cuanto es 4 + 3 ?': '7'
}

# --- Helper: screenshot de página completa, sin cortar nada ---
async def fullpage_screenshot(page, path):
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass

    # Dimensiones máximas del documento
    width = await page.evaluate("""
        () => Math.max(
          document.documentElement.scrollWidth,
          document.body ? document.body.scrollWidth : 0,
          document.documentElement.clientWidth
        )
    """)
    height = await page.evaluate("""
        () => Math.max(
          document.documentElement.scrollHeight,
          document.body ? document.body.scrollHeight : 0,
          document.documentElement.clientHeight
        )
    """)

    # Asegurar viewport suficientemente grande (cap por seguridad)
    vp = page.viewport_size or {"width": 1280, "height": 720}
    target_w = max(vp["width"], min(int(width), 1920))   # no más de 1920 de ancho
    target_h = min(int(height), 20000)                   # cap alto 20k

    await page.set_viewport_size({"width": target_w, "height": min(target_h, 1400)})
    await page.screenshot(path=path, full_page=True)


async def consultar_procuraduria(consulta_id, cedula, tipo_doc):
    browser = None
    context = None
    page = None
    evidencia_rel = ""
    try:
        # ---------- rutas de salida ----------
        relative_folder = os.path.join('resultados', str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f'{nombre_sitio}_{cedula}_{ts}'
        ok_png_abs = os.path.join(absolute_folder, f'{base_name}.png')
        ok_png_rel = os.path.join(relative_folder, f'{base_name}.png')
        err_png_abs = os.path.join(absolute_folder, f'{base_name}_error.png')
        err_png_rel = os.path.join(relative_folder, f'{base_name}_error.png')

        # ---------- validaciones previas ----------
        tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())
        if not tipo_doc_val:
            raise ValueError(f"Tipo de documento no válido: {tipo_doc}")

        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)

        # ---------- navegación ----------
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(1000)

            # Ubicar el iframe del formulario
            frame = None
            for f in page.frames:
                if "webcert/Certificado.aspx" in (f.url or ""):
                    frame = f
                    break
            if not frame and page.frames and len(page.frames) > 1:
                frame = page.frames[-1]
            if not frame:
                try:
                    await page.screenshot(path=err_png_abs, full_page=True)
                    evidencia_rel = err_png_rel
                except Exception:
                    evidencia_rel = ""
                raise Exception("No se encontró el iframe del formulario de consulta.")

            # Llenar formulario
            await frame.wait_for_selector('#ddlTipoID', timeout=15000)
            await frame.select_option('#ddlTipoID', value=tipo_doc_val)
            await frame.fill('#txtNumID', str(cedula))

            # Resolver pregunta
            solved = False
            pregunta = ""
            for _ in range(10):
                try:
                    pregunta = (await frame.inner_text('#lblPregunta')).strip()
                except Exception:
                    pregunta = ""
                respuesta = PREGUNTAS_RESPUESTAS.get(pregunta)
                if respuesta:
                    await frame.fill('#txtRespuestaPregunta', respuesta)
                    solved = True
                    break
                try:
                    await frame.click('#ImageButton1')  # refrescar
                except Exception:
                    pass
                await asyncio.sleep(1)

            if not solved:
                try:
                    await page.screenshot(path=err_png_abs, full_page=True)
                    evidencia_rel = err_png_rel
                except Exception:
                    evidencia_rel = ""
                raise Exception(f"No se pudo resolver la pregunta de seguridad. Última pregunta vista: '{pregunta}'")

            # Enviar
            await frame.click('#btnConsultar')

            # 1) Asegura que #divSec esté adjunto (aunque vacío)
            await frame.wait_for_selector("#divSec", state="attached", timeout=100000)

            # 2) Espera a que tenga texto o hijos típicos de resultado
            try:
                await frame.wait_for_function(
                    """
                    () => {
                        const el = document.querySelector('#divSec');
                        if (!el) return false;
                        const txt = (el.innerText || '').trim();
                        if (txt.length > 0) return true;
                        return !!el.querySelector(
                            '.alert, .card, .table, #divRespuesta, #divDescarga, #spnResultado, #lblCertificado, #lblContenido'
                        );
                    }
                    """,
                    timeout=25000
                )
            except Exception:
                pass  # seguimos con fallbacks

            # 3) Texto del contenedor
            try:
                div_text = (await frame.locator("#divSec").inner_text()).strip()
            except Exception:
                div_text = ""

            # 4) Fallback: HTML a texto plano si sigue vacío
            if not div_text:
                try:
                    html = await frame.content()
                except Exception:
                    html = ""
                flat = re.sub(r"<[^>]+>", " ", html or "")
                div_text = " ".join(flat.split())[:4000]

            # 5) Decide mensaje/score (inicializados para evitar UnboundLocalError)
            score = 0
            mensaje = "Resultado generado (revisar contenido en la evidencia)"
            low = (div_text or "").lower()

            if "no presenta antecedentes" in low:
                mensaje = "El ciudadano NO presenta antecedentes"
                score = 0
            elif "presenta antecedentes" in low:
                mensaje = "El ciudadano presenta antecedentes"
                score = 10
            elif "no se encontraron" in low or "no existen" in low:
                mensaje = "No se encontraron resultados"
                score = 0
            elif "antecedente" in low:
                score = 0 

            # 6) Evidencia: #divSec -> iframe -> página (FULL PAGE)
            saved = False
            try:
                # Pantallazo de TODA la página contenedora (como tu 2ª imagen)
                await fullpage_screenshot(page, ok_png_abs)
                evidencia_rel = ok_png_rel
                saved = True
            except Exception:
                saved = False

            if not saved:
                try:
                    # Fallback: capturar iframe completo (estirándolo a su scrollHeight)
                    iframe_el = await frame.frame_element()
                    content_h = await frame.evaluate(
                        "() => document.body.scrollHeight || document.documentElement.scrollHeight || 1200"
                    )
                    await iframe_el.evaluate(
                        "(el, h) => { el.style.height = h + 'px'; el.style.maxHeight = h + 'px'; }",
                        content_h
                    )
                    await iframe_el.screenshot(path=ok_png_abs)
                    evidencia_rel = ok_png_rel
                    saved = True
                except Exception:
                    saved = False

            if not saved:
                # Último recurso: tu captura anterior
                await page.screenshot(path=ok_png_abs, full_page=True)
                evidencia_rel = ok_png_rel


            # Guardar en BD
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validada",
                mensaje=mensaje,
                archivo=evidencia_rel
            )

    except Exception as e:
        if evidencia_rel == "" and page is not None:
            try:
                await page.screenshot(path=err_png_abs, full_page=True)
                evidencia_rel = err_png_rel
            except Exception:
                evidencia_rel = ""
        try:
            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
        except Exception:
            fuente_obj = None
        if fuente_obj:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin validar",
                mensaje=str(e),
                archivo=evidencia_rel
            )
    finally:
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
