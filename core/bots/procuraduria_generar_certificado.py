import os
import re
import asyncio
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

GEN_URL = "https://www.procuraduria.gov.co/Pages/Generacion-de-antecedentes.aspx"
NOMBRE_SITIO = "procuraduria_certificado"

TIPO_DOC_MAP = {
    "CC": "1",
    "PEP": "0",
    "NIT": "2",
    "CE": "5",
    "PPT": "10",
}

PREGUNTAS_RESPUESTAS = {
    "¿ Cuanto es 9 - 2 ?": "7",
    "¿ Cuanto es 3 X 3 ?": "9",
    "¿ Cuanto es 6 + 2 ?": "8",
    "¿ Cuanto es 2 X 3 ?": "6",
    "¿ Cuanto es 3 - 2 ?": "1",
    "¿ Cuanto es 4 + 3 ?": "7",
}

# --- helpers de screenshot / render ---


async def _fullpage_screenshot(page, path):
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    await page.screenshot(path=path, full_page=True)


# Poppler opcional para pdf2image (Windows)
POPPLER_PATH = getattr(settings, "POPPLER_PATH", os.getenv("POPPLER_PATH"))


def _render_pdf_primera_pagina_pymupdf(
    path_pdf: str, path_png: str, zoom: float = 3.0
) -> bool:
    """Render nítido SOLO del documento con PyMuPDF (preferido)."""
    try:
        import fitz  # PyMuPDF

        with fitz.open(path_pdf) as doc:
            if doc.page_count == 0:
                return False
            pg = doc[0]
            matrix = fitz.Matrix(zoom, zoom)
            pix = pg.get_pixmap(matrix=matrix, alpha=False)
            pix.save(path_png)
        return os.path.exists(path_png) and os.path.getsize(path_png) > 0
    except Exception:
        return False


def _render_pdf_primera_pagina_pdf2image(
    path_pdf: str, path_png: str, dpi: int = 300
) -> bool:
    """Render SOLO del documento con pdf2image (requiere Poppler)."""
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


async def _screenshot_pdf_element(context, abs_pdf: str, abs_png: str) -> None:
    """
    Fallback final: abrir file://<pdf> y capturar el <embed> del visor Chrome
    (evita miniaturas/toolbar del visor).
    """
    viewer = await context.new_page()
    file_url = Path(abs_pdf).resolve().as_uri()
    await viewer.goto(file_url, wait_until="load")
    # el <embed> puede variar según versión de Chromium
    embed = viewer.locator(
        "embed#pdf-embed, embed[type='application/x-google-chrome-pdf'], embed[type*='pdf']"
    ).first
    await embed.wait_for(state="visible", timeout=10000)
    await embed.screenshot(path=abs_png)
    await viewer.close()


# --- helpers de análisis del PDF ---


def _extraer_texto_pdf(path_pdf: str) -> str:
    """Extrae texto completo del certificado usando PyMuPDF."""
    try:
        import fitz

        texto_final = ""
        with fitz.open(path_pdf) as doc:
            for page in doc:
                texto_final += page.get_text()
        return texto_final.strip()
    except Exception:
        return ""


def _clasificar_certificado(texto: str) -> tuple[str, int]:
    """
    Determina si el certificado tiene sanciones.
    Retorna: (mensaje, score)
        score = 1 → NEGATIVO (no registra sanciones)
        score = 0 → POSITIVO (registra sanciones/anotaciones)
    """
    texto_low = (texto or "").lower()

    negativos = [
        "no registra sanciones",
        "no registra sancione",
        "no tiene sanciones",
        "no presenta sanciones",
        "sin sanciones",
        "ni inhabilidades vigentes",
    ]

    positivos = [
        "registra sanciones",
        "registra sancione",
        "inhabilidad",
        "inhabilidades vigentes",
        "sanción",
        "sanciones disciplinarias",
        "antecedentes disciplinarios",
    ]

    # Revisar primero si hay señales claras de sanciones
    if any(p in texto_low for p in positivos):
        return ("Registra sanciones o anotaciones disciplinarias.", 0)

    # Luego revisar indicadores de no sanciones
    if any(n in texto_low for n in negativos):
        return ("No registra sanciones ni inhabilidades.", 1)

    # Si no se reconoce, dejar como sin determinar pero no bloquear el flujo
    return (
        "No se pudo determinar claramente el estado del certificado (revisar manualmente).",
        1,
    )


# ============ BOT PRINCIPAL ============


async def generar_certificado_procuraduria(
    consulta_id: int, cedula: str, tipo_doc: str
):
    """
    Genera el certificado y deja evidencia SOLO del documento:
      1) Descarga PDF.
      2) PNG con PyMuPDF (preferido) -> pdf2image -> screenshot del <embed>.
      3) Analiza el PDF para determinar si hay sanciones o no.
    """
    print(
        f"[procuraduria] Iniciando generación de certificado para consulta_id={consulta_id}, cedula={cedula}, tipo_doc={tipo_doc}"
    )
    browser = context = page = None
    evidencia_rel = ""
    try:
        # --- rutas de salida ---
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"procuraduria_cert_{cedula}_{ts}"
        abs_png = os.path.join(absolute_folder, f"{base}.png")
        rel_png = os.path.join(relative_folder, f"{base}.png").replace("\\", "/")
        abs_pdf = os.path.join(absolute_folder, f"{base}.pdf")
        err_png_abs = os.path.join(absolute_folder, f"{base}_error.png")
        err_png_rel = os.path.join(relative_folder, f"{base}_error.png").replace(
            "\\", "/"
        )

        # --- validaciones ---
        print(f"[procuraduria] Validando tipo de documento: {tipo_doc}")
        tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())
        if not tipo_doc_val:
            raise ValueError(f"Tipo de documento no válido: {tipo_doc}")
        print(f"[procuraduria] Tipo de documento válido: {tipo_doc_val}")

        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)

        async with async_playwright() as p:
            headless = os.getenv("PROCURADURIA_HEADLESS", "true").lower() == "true"
            slow_mo = int(os.getenv("PROCURADURIA_SLOW_MO", "0"))
            browser = await p.chromium.launch(headless=headless, slow_mo=slow_mo)
            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1600, "height": 1000},
                locale="es-CO",
            )
            page = await context.new_page()

            # 1) Cargar página
            print(f"[procuraduria] Navegando a {GEN_URL}")
            try:
                await page.goto(GEN_URL, wait_until="domcontentloaded", timeout=90000)
                print(
                    "[procuraduria] Página cargada con domcontentloaded"
                )
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    print("[procuraduria] Página en estado networkidle")
                except Exception:
                    pass

                # Detectar si la página responde con "No Disponible"
                body_text = ""
                try:
                    body_text = (await page.locator("body").inner_text()).strip()
                except Exception:
                    try:
                        body_text = (await page.content()).strip()
                    except Exception:
                        body_text = ""
                error_signals = [
                    "Página Web No Disponible",
                    "La página que consulta no se encuentra disponible",
                    "Página no disponible",
                ]
                if any(sig in body_text for sig in error_signals):
                    print(
                        "[procuraduria] Sitio devuelve página 'No Disponible'. Iniciando reintentos."
                    )
                    recovered = False
                    # Intentos de recarga simples
                    for attempt in range(2):
                        try:
                            print(
                                f"[procuraduria] Reintento {attempt+1}: recargando..."
                            )
                            await page.reload(
                                wait_until="domcontentloaded", timeout=30000
                            )
                            await page.wait_for_timeout(1000)
                            body_text = (
                                await page.locator("body").inner_text()
                            ).strip()
                            if not any(sig in body_text for sig in error_signals):
                                print(
                                    "[procuraduria] Reintento exitoso, continuando."
                                )
                                recovered = True
                                break
                        except Exception:
                            pass
                    if not recovered:
                        # Re-crear contexto con User-Agent distinto y reintentar una vez
                        ua = os.getenv(
                            "PROCURADURIA_USER_AGENT",
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/123 Safari/537.36",
                        )
                        try:
                            await context.close()
                        except Exception:
                            pass
                        print(
                            "[procuraduria] Intentando nuevo contexto con User-Agent alternativo"
                        )
                        context = await browser.new_context(
                            accept_downloads=True,
                            user_agent=ua,
                            viewport={"width": 1600, "height": 1000},
                            locale="es-CO",
                        )
                        page = await context.new_page()
                        try:
                            await page.goto(
                                GEN_URL,
                                wait_until="domcontentloaded",
                                timeout=90000,
                            )
                            await page.wait_for_load_state(
                                "networkidle", timeout=15000
                            )
                            body_text = (
                                await page.locator("body").inner_text()
                            ).strip()
                        except Exception:
                            body_text = ""
                        if any(sig in body_text for sig in error_signals):
                            print(
                                "[procuraduria] Sitio continúa devolviendo 'No Disponible' después de reintentos."
                            )
                            await sync_to_async(Resultado.objects.create)(
                                consulta_id=consulta_id,
                                fuente=fuente_obj,
                                score=1,
                                estado="Sin validar",
                                mensaje="Sitio de generación no disponible o devuelve página de error.",
                                archivo="",
                            )
                            return
                        else:
                            print(
                                "[procuraduria] Nuevo contexto recuperó la página, continuando."
                            )
            except PWTimeout:
                print("[procuraduria] TIMEOUT al cargar página")
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=1,
                    estado="Sin validar",
                    mensaje="La página de generación no cargó o está caída (timeout).",
                    archivo="",
                )
                return

            await page.wait_for_timeout(600)

            # 2) Localizar iframe del certificado
            print("[procuraduria] Buscando iframe del certificado...")
            frame = None
            for f in page.frames:
                if "/webcert/" in (f.url or "") or "Certificado" in (f.url or ""):
                    frame = f
                    print(f"[procuraduria] Iframe encontrado: {f.url}")
                    break
            if not frame and page.frames and len(page.frames) > 1:
                frame = page.frames[-1]
                print(f"[procuraduria] Usando último iframe: {frame.url}")
            if not frame:
                print("[procuraduria] ERROR: No se encontró iframe")
                raise Exception(
                    "No se encontró el iframe de generación del certificado."
                )

            # 3) Formulario
            print(
                f"[procuraduria] Completando formulario con tipo={tipo_doc_val}, cedula={cedula}"
            )
            await frame.wait_for_selector("#ddlTipoID", timeout=20000)
            await frame.select_option("#ddlTipoID", value=tipo_doc_val)
            await frame.fill("#txtNumID", str(cedula))
            print("[procuraduria] Formulario completado")

            # 4) Resolver pregunta
            print("[procuraduria] Intentando resolver pregunta de seguridad...")
            solved = False
            ultima_pregunta = ""
            for intento in range(12):
                try:
                    ultima_pregunta = (
                        await frame.locator(
                            "#lblPregunta, [id*=lblPregunta]"
                        ).inner_text()
                    ).strip()
                except Exception:
                    ultima_pregunta = ""
                resp = PREGUNTAS_RESPUESTAS.get(ultima_pregunta)
                if resp:
                    print(
                        f"[procuraduria] Pregunta encontrada: '{ultima_pregunta}' -> respuesta: '{resp}'"
                    )
                    try:
                        await frame.fill("#txtRespuestaPregunta", resp)
                    except Exception:
                        await frame.locator("input[id*=txtRespuesta]").fill(resp)
                    solved = True
                    print("[procuraduria] Respuesta enviada exitosamente")
                    break
                print(
                    f"[procuraduria] Intento {intento+1}: Pregunta no resuelta, refrescando..."
                )
                try:
                    await frame.click("#ImageButton1")  # refrescar
                except Exception:
                    pass
                await asyncio.sleep(1)

            if not solved:
                print(
                    "[procuraduria] ERROR: No se pudo resolver pregunta después de 12 intentos."
                    f" Última: '{ultima_pregunta}'"
                )
                raise Exception(
                    f"No se pudo resolver la pregunta. Última: '{ultima_pregunta}'"
                )

            # 5) Generar
            print("[procuraduria] Generando certificado...")
            prev_len = await frame.evaluate(
                "() => document.documentElement.outerHTML.length"
            )
            await frame.locator("#btnExportar").evaluate("b => b.click()")
            try:
                await frame.wait_for_function(
                    "prev => document.documentElement.outerHTML.length !== prev",
                    arg=prev_len,
                    timeout=30000,
                )
                print("[procuraduria] Certificado generado en el iframe")
            except Exception:
                print(
                    "[procuraduria] Timeout esperando cambio en certificado, continuando..."
                )

            # 6) Descargar PDF
            print("[procuraduria] Esperando descarga del certificado PDF...")
            try:
                # Esperar descarga del navegador
                async with page.expect_download(timeout=30000) as download_info:
                    # Click en botón descargar
                    try:
                        await frame.locator("#btnDescargar").click()
                    except Exception:
                        await frame.locator("#btnDescargar").evaluate("el => el.click()")
                
                # Guardar PDF descargado
                download = await download_info.value
                await download.save_as(abs_pdf)
                print(f"[procuraduria] PDF descargado: {abs_pdf}")

                # Verificar que existe y tiene contenido
                if not os.path.exists(abs_pdf) or os.path.getsize(abs_pdf) == 0:
                    raise Exception("El PDF descargado está vacío o no existe")

                # Convertir PDF a PNG para evidencia
                print("[procuraduria] Convirtiendo PDF a PNG...")
                if _render_pdf_primera_pagina_pymupdf(abs_pdf, abs_png, zoom=3.0):
                    print("[procuraduria] PNG generado con PyMuPDF (alta calidad)")
                elif _render_pdf_primera_pagina_pdf2image(abs_pdf, abs_png, dpi=300):
                    print("[procuraduria] PNG generado con pdf2image")
                else:
                    print("[procuraduria] PNG generado con screenshot de archivo")
                    await _screenshot_pdf_element(context, abs_pdf, abs_png)

                if not os.path.exists(abs_png) or os.path.getsize(abs_png) == 0:
                    raise Exception("No se pudo generar la imagen PNG del certificado")

                evidencia_rel = rel_png

                # Analizar texto del PDF
                print("[procuraduria] Analizando contenido del certificado...")
                texto_pdf = _extraer_texto_pdf(abs_pdf)
                mensaje_clasificacion, score = _clasificar_certificado(texto_pdf)
                print(f"[procuraduria] Clasificación: {mensaje_clasificacion} (score={score})")

                # Guardar resultado
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validada",
                    mensaje=mensaje_clasificacion,
                    archivo=evidencia_rel,
                )
                print("[procuraduria] ✓ Certificado descargado y procesado exitosamente")
                return

            except Exception as e:
                # Si falla la descarga, NO generar evidencia
                print(f"[procuraduria] ERROR: No se pudo descargar el certificado PDF: {str(e)}")
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=1,
                    estado="Sin validar",
                    mensaje=f"No se logró descargar el certificado PDF: {str(e)}",
                    archivo="",
                )
                return

    except Exception as e:
        print(f"[procuraduria] EXCEPCIÓN: {str(e)}")
        try:
            fuente_obj = await sync_to_async(Fuente.objects.get)(
                nombre=NOMBRE_SITIO
            )
        except Exception:
            fuente_obj = None
        if fuente_obj:
            print("[procuraduria] Creando resultado: ERROR")
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=1,
                estado="Sin validar",
                mensaje=f"Error en generación: {str(e)}",
                archivo="",
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


# Alias para compatibilidad con run_bot_single.py
# (para usar: --bot procuraduria_certificado)
consultar_procuraduria_certificado = generar_certificado_procuraduria
