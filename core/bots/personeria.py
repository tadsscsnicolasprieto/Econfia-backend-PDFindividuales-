# core/bots/personeria.py
import os
import re
import traceback
import asyncio
from datetime import datetime
from typing import Optional

from playwright.async_api import async_playwright, Page, Browser, BrowserContext
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

import PyPDF2
import fitz  # PyMuPDF

PAGE_URL = "https://antecedentes.personeriabogota.gov.co/expedicion-antecedentes"
NOMBRE_SITIO = "personeria"
TEXTO_OK = "NO REGISTRA SANCIONES NI INHABILIDADES VIGENTES"

TIPOS_DOC = {
    "CC": "1",
    "CE": "2",
    "PEP": "10",
    "PPT": "11",
    "TI": "3",
}


def _normalize_fecha(fecha_str: Optional[str]) -> Optional[str]:
    if not fecha_str:
        return None
    f = fecha_str.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(f, fmt)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            continue
    return None


async def _save_debug_html(page: Page, folder: str, prefix: str):
    try:
        html = await page.content()
        path = os.path.join(folder, f"{prefix}.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"[PERSONERIA][DEBUG] HTML guardado en: {path}")
    except Exception as e:
        print(f"[PERSONERIA][WARN] No se pudo guardar HTML de debug: {e}")


async def _goto_with_retries(page: Page, url: str, folder: str, ts: str, attempts: int = 3, base_delay: float = 2.0, timeout: int = 90000):
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            await page.goto(url, timeout=timeout, wait_until="load")
            return True
        except Exception as e:
            last_exc = e
            print(f"[PERSONERIA][WARN] goto intento {i} falló: {e}")
            try:
                await _save_debug_html(page, folder, f"goto_debug_{ts}_attempt{i}")
            except Exception:
                pass
            if i < attempts:
                await asyncio.sleep(base_delay * (2 ** (i - 1)))
    raise last_exc


async def _dismiss_swals_or_overlays(page: Page):
    try:
        swal_confirm = page.locator(".swal2-container .swal2-confirm")
        if await swal_confirm.count() > 0 and await swal_confirm.is_visible():
            try:
                await swal_confirm.first.click(timeout=3000)
                await page.wait_for_timeout(300)
                print("[PERSONERIA] Cerrado modal swal2 con .swal2-confirm")
                return
            except Exception:
                pass
        await page.evaluate("""
            () => {
                const sel = ['.swal2-container', '.swal2-backdrop', '.modal-backdrop', '.overlay', '.swal2-shown'];
                sel.forEach(s => document.querySelectorAll(s).forEach(e => { try { e.remove(); } catch(e){} }));
                document.querySelectorAll('[style*="pointer-events: none"]').forEach(e => { try { e.style.pointerEvents = 'auto'; } catch(e){} });
            }
        """)
        await page.wait_for_timeout(200)
        print("[PERSONERIA] Intento de remover overlays por JS realizado")
    except Exception as e:
        print(f"[PERSONERIA][WARN] _dismiss_swals_or_overlays falló: {e}")


async def _safe_screenshot(page: Optional[Page], path: str):
    if page is None:
        print("[PERSONERIA][WARN] No hay página para tomar screenshot")
        return False
    try:
        await page.screenshot(path=path, full_page=True)
        print(f"[PERSONERIA] Pantallazo guardado en: {path}")
        return True
    except Exception as e:
        print(f"[PERSONERIA][WARN] No se pudo tomar pantallazo: {e}")
        return False


async def _save_pdf_bytes(path: str, data: bytes):
    try:
        with open(path, "wb") as fh:
            fh.write(data)
        print(f"[PERSONERIA] PDF guardado en: {path}")
        return True
    except Exception as e:
        print(f"[PERSONERIA][WARN] No se pudo guardar PDF: {e}")
        return False


async def consultar_personeria(
    consulta_id: Optional[int] = None,
    cedula: Optional[str] = None,
    tipo_doc: Optional[str] = None,
    fecha_expedicion: Optional[str] = None,
    headless: bool = True,
    slow_mo: Optional[int] = None,
    proxy: Optional[dict] = None,
    codigo_verificacion: Optional[str] = None,
):
    if isinstance(headless, str):
        headless = headless.strip().lower() in ("1", "true", "yes", "y", "t")
    else:
        headless = bool(headless)

    print(f"[PERSONERIA] Inicio consulta id={consulta_id} cedula={cedula} tipo={tipo_doc} fecha_raw={fecha_expedicion} headless={headless}")
    print(f"[PERSONERIA] Parámetros recibidos: fecha_expedicion_raw={fecha_expedicion} codigo_verificacion={codigo_verificacion}")

    fecha_iso = _normalize_fecha(fecha_expedicion) if fecha_expedicion else None
    if fecha_expedicion and not fecha_iso:
        msg = f"Fecha inválida: {fecha_expedicion}. Usa YYYY-MM-DD o DD/MM/YYYY o DD-MM-YYYY."
        print(f"[PERSONERIA][ERROR] {msg}")
        try:
            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
        except Exception:
            fuente_obj = None
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje=msg,
            archivo=""
        )
        return

    if not fecha_iso:
        fecha_iso = datetime.now().strftime("%Y-%m-%d")
        print(f"[PERSONERIA] No se proporcionó fecha_expedicion. Usando fecha actual: {fecha_iso}")

    tipo_doc_value = TIPOS_DOC.get((tipo_doc or "").upper())
    if not tipo_doc_value:
        msg = f"Tipo de documento no soportado: {tipo_doc}"
        print(f"[PERSONERIA][ERROR] {msg}")
        try:
            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
        except Exception:
            fuente_obj = None
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje=msg,
            archivo=""
        )
        return

    relative_folder = os.path.join("resultados", str(consulta_id or "no_consulta"))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_name = f"personeria_{cedula}_{ts}.pdf"
    png_name = f"personeria_{cedula}_{ts}.png"
    absolute_pdf = os.path.join(absolute_folder, pdf_name)
    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        print(f"[PERSONERIA][WARN] No se encontró Fuente '{NOMBRE_SITIO}': {e}")
        fuente_obj = None

    intentos = 0
    exito = False
    last_exception = None

    while intentos < 3 and not exito:
        intentos += 1
        print(f"[PERSONERIA] Intento {intentos}/3")
        browser: Optional[Browser] = None
        page: Optional[Page] = None
        context: Optional[BrowserContext] = None
        try:
            async with async_playwright() as p:
                launch_args = {}
                if slow_mo:
                    launch_args["slow_mo"] = slow_mo
                launch_args.setdefault("args", ["--no-sandbox", "--disable-dev-shm-usage"])
                browser = await p.chromium.launch(headless=headless, **launch_args)

                context_kwargs = {"accept_downloads": True, "ignore_https_errors": True}
                if proxy:
                    context_kwargs["proxy"] = proxy
                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()

                page.on("console", lambda msg: print(f"[BROWSER][console] {msg.type}: {msg.text}"))
                page.on("pageerror", lambda exc: print(f"[BROWSER][pageerror] {exc}"))
                page.on("response", lambda resp: print(f"[BROWSER][resp] {resp.status} {resp.url}"))

                print(f"[PERSONERIA] Navegando a {PAGE_URL}")
                try:
                    await _goto_with_retries(page, PAGE_URL, absolute_folder, ts, attempts=4, base_delay=3.0, timeout=90000)
                except Exception as e:
                    raise Exception(f"Page.goto falló: {e}")

                print("[PERSONERIA] Página cargada")

                # llenar formulario principal
                try:
                    await page.select_option("#tipo_documento", tipo_doc_value)
                except Exception as e:
                    print(f"[PERSONERIA][WARN] select_option falló: {e}")

                try:
                    await page.fill("#documento", str(cedula))
                except Exception as e:
                    print(f"[PERSONERIA][WARN] fill documento falló: {e}")

                fecha_fmt = datetime.strptime(fecha_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
                print(f"[PERSONERIA] Seteando fecha_expedicion: {fecha_fmt}")
                try:
                    await page.evaluate("document.getElementById('fecha_expedicion').removeAttribute('readonly')")
                    await page.evaluate(f"""
                        var fp = document.getElementById('fecha_expedicion')._flatpickr;
                        if(fp) {{ fp.setDate('{fecha_fmt}', true); }}
                    """)
                except Exception as e:
                    print(f"[PERSONERIA][WARN] No se pudo setear fecha via flatpickr: {e}")

                # rellenar código de verificación si aplica
                if codigo_verificacion:
                    try:
                        if await page.locator("#codigo_v").count() > 0:
                            await page.fill("#codigo_v", codigo_verificacion)
                            print("[PERSONERIA] Código de verificación rellenado")
                    except Exception as e:
                        print(f"[PERSONERIA][WARN] No se pudo rellenar #codigo_v: {e}")

                await _dismiss_swals_or_overlays(page)

                # --- Espera robusta por descarga / popup / inyección DOM / XHR ---
                pdf_saved = False
                pdf_bytes = None

                # futures / tasks
                loop = asyncio.get_event_loop()
                download_future = loop.create_future()
                popup_future = loop.create_future()
                xhr_future = loop.create_future()

                # listeners
                def on_download(download):
                    try:
                        if not download_future.done():
                            download_future.set_result(("download", download))
                    except Exception:
                        pass

                def on_page(new_page):
                    try:
                        if not popup_future.done():
                            popup_future.set_result(("popup", new_page))
                    except Exception:
                        pass

                def on_response(r):
                    try:
                        u = r.url.lower()
                        ct = r.headers.get("content-type", "").lower()
                        if u.endswith(".pdf") or ct.startswith("application/pdf"):
                            if not xhr_future.done():
                                xhr_future.set_result(("xhr_pdf", r))
                    except Exception:
                        pass

                context.on("download", on_download)
                context.on("page", on_page)
                page.on("response", on_response)

                # task: wait for DOM anchor/button injection
                async def wait_dom_selector():
                    try:
                        sel = await page.wait_for_selector(
                            "a[href$='.pdf'], a:has-text('Descargar'), button:has-text('Descargar'), a:has-text('Certificado')",
                            timeout=25000
                        )
                        return ("dom", sel)
                    except Exception:
                        return None

                dom_task = asyncio.create_task(wait_dom_selector())

                # enviar formulario (inicia Livewire XHR)
                try:
                    await page.click("button[type='submit']")
                except Exception as e:
                    print(f"[PERSONERIA][WARN] click submit falló: {e}")

                # esperar cualquiera de los eventos
                done, pending = await asyncio.wait(
                    [download_future, popup_future, xhr_future, dom_task],
                    timeout=45,
                    return_when=asyncio.FIRST_COMPLETED
                )

                result = None
                if download_future.done():
                    result = download_future.result()
                elif popup_future.done():
                    result = popup_future.result()
                elif xhr_future.done():
                    result = xhr_future.result()
                elif dom_task.done() and dom_task.result():
                    result = dom_task.result()

                # cleanup listeners
                try:
                    context.off("download", on_download)
                    context.off("page", on_page)
                    page.off("response", on_response)
                except Exception:
                    pass

                # if nothing happened, save debug and raise
                if not result:
                    await _save_debug_html(page, absolute_folder, f"no_download_{ts}_attempt{intentos}")
                    try:
                        await _safe_screenshot(page, absolute_png)
                    except Exception:
                        pass
                    raise Exception("No se encontró enlace/descarga del PDF; HTML guardado para inspección.")

                kind, payload = result

                # handle each case
                if kind == "download":
                    download = payload
                    await download.save_as(absolute_pdf)
                    pdf_saved = True

                elif kind == "popup":
                    new_page: Page = payload
                    try:
                        await new_page.wait_for_load_state("load", timeout=20000)
                    except Exception:
                        pass
                    # try to capture download from popup
                    try:
                        async with new_page.expect_download(timeout=20000) as di:
                            # if popup triggers download automatically, this will capture it
                            await asyncio.sleep(0.1)
                        dl = await di.value
                        await dl.save_as(absolute_pdf)
                        pdf_saved = True
                    except Exception:
                        # fallback: search anchor in popup
                        try:
                            a = await new_page.query_selector("a[href$='.pdf'], a:has-text('Descargar'), a:has-text('Certificado')")
                            if a:
                                href = await a.get_attribute("href")
                                if href and href.lower().startswith("http"):
                                    content = await new_page.evaluate("""(u) => fetch(u).then(r => r.arrayBuffer()).then(b => Array.from(new Uint8Array(b)))""", href)
                                    if content:
                                        await _save_pdf_bytes(absolute_pdf, bytes(content))
                                        pdf_saved = True
                        except Exception:
                            pass

                elif kind == "xhr_pdf":
                    resp = payload
                    try:
                        ct = resp.headers.get("content-type", "").lower()
                        if ct.startswith("application/pdf"):
                            buf = await resp.body()
                            await _save_pdf_bytes(absolute_pdf, buf)
                            pdf_saved = True
                        else:
                            body = await resp.text()
                            m = re.search(r'https?://[^"\']+\.pdf', body, flags=re.IGNORECASE)
                            if m:
                                pdf_url = m.group(0)
                                content = await page.evaluate("""(u) => fetch(u).then(r => r.arrayBuffer()).then(b => Array.from(new Uint8Array(b)))""", pdf_url)
                                if content:
                                    await _save_pdf_bytes(absolute_pdf, bytes(content))
                                    pdf_saved = True
                    except Exception as e:
                        print(f"[PERSONERIA][WARN] Error manejando respuesta XHR: {e}")

                elif kind == "dom":
                    sel = payload
                    try:
                        async with page.expect_download(timeout=20000) as di:
                            await sel.click()
                        dl = await di.value
                        await dl.save_as(absolute_pdf)
                        pdf_saved = True
                    except Exception:
                        try:
                            href = await sel.get_attribute("href")
                            if href and href.lower().startswith("http"):
                                content = await page.evaluate("""(u) => fetch(u).then(r => r.arrayBuffer()).then(b => Array.from(new Uint8Array(b)))""", href)
                                if content:
                                    await _save_pdf_bytes(absolute_pdf, bytes(content))
                                    pdf_saved = True
                        except Exception as e:
                            print(f"[PERSONERIA][WARN] Fallback descarga desde href falló: {e}")

                # always take a screenshot of the page state
                try:
                    await _safe_screenshot(page, absolute_png)
                except Exception:
                    pass

                # close browser context
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass

            # after browser closed, process PDF if saved
            if not pdf_saved or not os.path.exists(absolute_pdf):
                raise Exception("No se pudo obtener el PDF tras los intentos; HTML y screenshot guardados para inspección.")

            # extract text from PDF
            pdf_text = ""
            try:
                with open(absolute_pdf, "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    for idx, ppage in enumerate(reader.pages):
                        text_page = ppage.extract_text() or ""
                        pdf_text += text_page
                        print(f"[PERSONERIA] Página {idx+1}: extraídos {len(text_page)} caracteres")
            except Exception as e:
                raise Exception(f"No se pudo leer el PDF: {e}")

            # convert first page to PNG (visual capture of PDF)
            try:
                pdf_doc = fitz.open(absolute_pdf)
                pdf_page = pdf_doc[0]
                pix = pdf_page.get_pixmap(dpi=200)
                pix.save(absolute_png)
                pdf_doc.close()
                print(f"[PERSONERIA] PNG guardado en: {absolute_png}")
            except Exception as e:
                raise Exception(f"No se pudo convertir PDF a PNG: {e}")

            # evaluate result
            pdf_text_upper = (pdf_text or "").upper()
            print(f"[PERSONERIA] Longitud texto extraído: {len(pdf_text_upper)}")
            if TEXTO_OK in pdf_text_upper:
                score = 0
                mensaje = TEXTO_OK
                print("[PERSONERIA] Resultado: NO REGISTRA sanciones (score=0)")
            else:
                score = 10
                mensaje = "Se encontraron hallazgos en la consulta"
                print("[PERSONERIA] Resultado: HALLAZGOS detectados (score=10)")

            # save result
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validado",
                mensaje=mensaje,
                archivo=relative_png
            )
            print("[PERSONERIA] Resultado guardado correctamente")
            exito = True

        except Exception as e:
            last_exception = e
            print(f"[PERSONERIA][ERROR] Excepción en intento {intentos}: {e}")
            traceback.print_exc()
            try:
                if page is not None:
                    await _save_debug_html(page, absolute_folder, f"error_{ts}_attempt{intentos}")
                    try:
                        await _safe_screenshot(page, absolute_png)
                    except Exception as ss_err:
                        print(f"[PERSONERIA][WARN] No se pudo tomar pantallazo de error: {ss_err}")
            except Exception as dbg_err:
                print(f"[PERSONERIA][WARN] Error al intentar guardar debug: {dbg_err}")
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
            await asyncio.sleep(1.0)

    if not exito:
        msg = f"Ocurrió un problema al obtener la información de la fuente: {last_exception}"
        print(f"[PERSONERIA][ERROR] {msg}")
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje=msg,
            archivo=relative_png if os.path.exists(absolute_png) else ""
        )
        print("[PERSONERIA] Registro de error guardado")
