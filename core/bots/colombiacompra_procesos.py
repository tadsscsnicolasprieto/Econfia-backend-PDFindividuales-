# core/bots/colombiacompra_procesos.py
import os
import re
import asyncio
import traceback
from datetime import datetime, timedelta
import fitz
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from core.models import Resultado, Fuente

NOMBRE_SITIO = "colombiacompra_procesos"
URL = "https://consultaprocesos.colombiacompra.gov.co/"

# -------- Selectores robustos --------
SEL_CHECK_TERMS   = "#cp-accept-terms, input[name='accept_terms']"
SEL_BTN_TERMS_OK  = "#cp-continue-to-profile, button#cp-continue-to-profile"
SEL_CARD_PROV     = "h4:has-text('Proveedores'), .cp-radio-content:has(h4:has-text('Proveedores'))"
SEL_BTN_TO_SEARCH = "#cp-continue-to-search, button#cp-continue-to-search"
SEL_DOC_INPUT     = "#cp-numero-documento, input[name='numero_documento']"

# Fechas
SEL_FECHA_INI = "#cp-fecha-inicio, input[name='fecha_inicio'], input[placeholder*='inicio' i]"
SEL_FECHA_FIN = "#cp-fecha-fin,   input[name='fecha_fin'],    input[placeholder*='final' i], input[placeholder*='fin' i]"

# Buscar
SEL_BTN_BUSCAR = "#cp-search-submit, button#cp-search-submit, form button[type='submit']"

# Botón manual de PDF (fallback)
SEL_BTN_MANUAL_PDF = "button:has-text('Descargar Certificado PDF (Manual)'), a:has-text('Descargar Certificado PDF (Manual)')"

# Hints para centrar vista (opcional)
RESULT_HINTS = [".cp-results", ".results", "table", ".cp-resultados", ".cp-cards", ".dataTables_wrapper"]

# -------- Tiempos --------
NAV_TIMEOUT_MS        = 120_000
WAIT_AFTER_NAV_MS     = 1_500
WAIT_VISIBLE_MS       = 15_000
DOWNLOAD_TIMEOUT_MS   = 60_000  # da tiempo a que el backend arme el PDF

DEBUG_PRINTS = True


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dbg(consulta_id, msg):
    if DEBUG_PRINTS:
        print(f"[{_now()}][{NOMBRE_SITIO}][consulta:{consulta_id}] {msg}", flush=True)


# ------------------------------------
# Persistencia de resultados
async def _registrar(consulta_id, fuente, estado, mensaje, archivo, score: int = 0):
    try:
        _dbg(consulta_id, f"Registrando resultado -> estado='{estado}', score={score}, archivo='{archivo[:120]}'")
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente,
            score=score,
            estado=estado,
            mensaje=mensaje,
            archivo=archivo,
        )
        _dbg(consulta_id, "Resultado registrado OK.")
    except Exception as e:
        print(f"[{_now()}][{NOMBRE_SITIO}][consulta:{consulta_id}] ERROR registrando resultado: {e}", flush=True)
        traceback.print_exc()


# ------------------------------------
# Fechas: FIN = hoy, INICIO = hoy - 5 años (maneja 29/Feb)
def _fechas_dt():
    hoy = datetime.now()
    try:
        ini = hoy.replace(year=hoy.year - 5)
    except ValueError:
        # Si hoy es 29/Feb y el año -5 no lo tiene, cae acá
        ini = hoy - timedelta(days=365 * 5)
    return hoy, ini  # objetos datetime


# Setter robusto para inputs de fecha: respeta type="date" (YYYY-MM-DD) o texto con máscara dd/mm/yyyy
async def _set_date(page, sel: str, dt: datetime, consulta_id: int, nombre: str):
    el = page.locator(sel).first
    await el.wait_for(state="visible", timeout=WAIT_VISIBLE_MS)

    # Si viene readonly por máscara, lo removemos (no es crítico si falla)
    try:
        await el.evaluate("e => e.removeAttribute && e.removeAttribute('readonly')")
    except Exception:
        pass

    # Detecta el tipo del input
    try:
        t = (await el.get_attribute("type") or "").lower()
    except Exception:
        t = ""

    # Formato según tipo
    val = dt.strftime("%Y-%m-%d") if t == "date" else dt.strftime("%d/%m/%Y")

    # Rellena y dispara eventos (input/change)
    try:
        await el.fill("")          # limpia
        await el.fill(val)
    except Exception:
        handle = await el.element_handle()
        await page.evaluate(
            """(e, v) => {
                e.value = v;
                e.dispatchEvent(new Event('input',  {bubbles:true}));
                e.dispatchEvent(new Event('change', {bubbles:true}));
            }""",
            handle, val
        )

    # Blur para que se valide la máscara/form
    try:
        await page.keyboard.press("Tab")
    except Exception:
        pass

    # Lee lo que quedó realmente y loguea
    try:
        cur = await el.input_value()
    except Exception:
        cur = "(no readable)"
    _dbg(consulta_id, f"[{nombre}] type='{t}' set='{val}' read='{cur}'")


# ------------------------------------
# Descargas: helpers para escoger SIEMPRE el PDF (ignorar XLSX)
async def _is_pdf(download) -> bool:
    """Valida por extensión o cabecera mágica %PDF- (archivo real PDF)."""
    try:
        name = (download.suggested_filename or "").lower()
        url = (download.url or "").lower()
        if name.endswith(".pdf") or ".pdf" in url:
            return True
        tmp_path = await download.path()
        if not tmp_path:
            return False
        with open(tmp_path, "rb") as f:
            head = f.read(5)
        return head.startswith(b"%PDF-")
    except Exception:
        return False


async def _wait_pdf_download(page, consulta_id: int, overall_timeout_ms: int = 60_000):
    """Espera múltiples 'download' hasta que llegue un PDF; ignora otras descargas (XLSX)."""
    deadline = asyncio.get_event_loop().time() + (overall_timeout_ms / 1000)
    intentos = 0
    while asyncio.get_event_loop().time() < deadline:
        intentos += 1
        restante_ms = int((deadline - asyncio.get_event_loop().time()) * 1000)
        if restante_ms <= 0:
            break
        try:
            d = await page.wait_for_event("download", timeout=restante_ms)
        except Exception:
            break

        name = (d.suggested_filename or "").lower()
        url  = (d.url or "").lower()
        es_pdf = await _is_pdf(d)
        _dbg(consulta_id, f"Descarga recibida (intento {intentos}): name='{name}' url='{url}' -> es_pdf={es_pdf}")

        if es_pdf:
            return d

        # --- Opcional: si deseas archivar el XLSX para auditoría, descomenta:
        # try:
        #     if name.endswith(".xlsx"):
        #         tmp = await d.path()
        #         if tmp:
        #             # Necesitas abs_folder y safe_num; pásalos por cierre o gestiona externamente
        #             pass
        # except Exception as _:
        #     pass

    raise PWTimeout("No llegó ningún PDF dentro del tiempo dado.")


# ------------------------------------
# Bot principal
async def consultar_colombiacompra_procesos(
    consulta_id: int,
    numero: str,
):
    browser = None
    _dbg(consulta_id, f"Inicio bot. Número documento: '{numero}' | URL: {URL}")

    # Fuente
    try:
        _dbg(consulta_id, "Buscando Fuente en BD…")
        fuente = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
        _dbg(consulta_id, f"Fuente encontrada: id={fuente.id}")
    except Exception as e:
        err = f"Fuente '{NOMBRE_SITIO}' no encontrada: {e}"
        print(f"[{_now()}][{NOMBRE_SITIO}][consulta:{consulta_id}] {err}", flush=True)
        traceback.print_exc()
        await _registrar(consulta_id, None, "error", err, "")
        return

    try:
        # Carpeta resultados
        _dbg(consulta_id, "Preparando carpetas de resultados…")
        rel_folder = os.path.join("resultados", str(consulta_id))
        abs_folder = os.path.join(settings.MEDIA_ROOT, rel_folder)
        os.makedirs(abs_folder, exist_ok=True)
        _dbg(consulta_id, f"Carpeta OK: {abs_folder}")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_num = re.sub(r"\s+", "_", (numero or "").strip()) or "doc"

        # Nombres de archivos
        pdf_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.pdf"
        abs_pdf  = os.path.join(abs_folder, pdf_name)

        img_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"
        abs_img  = os.path.join(abs_folder, img_name)
        rel_img  = os.path.join(rel_folder, img_name)

        _dbg(consulta_id, f"Paths -> PDF: {abs_pdf} | IMG: {abs_img}")

        # Fechas (FIN = hoy, INICIO = hoy - 5 años)
        fin_dt, ini_dt = _fechas_dt()
        _dbg(consulta_id, f"Rango fechas (dt) -> fin: {fin_dt.date()} | inicio: {ini_dt.date()}")

        async with async_playwright() as p:
            _dbg(consulta_id, "Lanzando Chromium headless…")
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            _dbg(consulta_id, "Creando contexto…")
            context = await browser.new_context(
                accept_downloads=True,
                ignore_https_errors=True,
                locale="es-CO",
                viewport={"width": 1400, "height": 2200},
            )
            context.set_default_timeout(30_000)

            page = await context.new_page()
            _dbg(consulta_id, "Navegando a la URL…")
            await page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=6_000)
                _dbg(consulta_id, "Estado 'networkidle' alcanzado.")
            except Exception:
                _dbg(consulta_id, "No se alcanzó 'networkidle' (no crítico).")
            await page.wait_for_timeout(WAIT_AFTER_NAV_MS)

            # Aceptar términos
            _dbg(consulta_id, "Intentando aceptar términos…")
            try:
                await page.wait_for_selector(SEL_CHECK_TERMS, timeout=WAIT_VISIBLE_MS)
                cb = page.locator(SEL_CHECK_TERMS).first
                await cb.scroll_into_view_if_needed()
                try:
                    await cb.check()
                    _dbg(consulta_id, "Checkbox términos marcado con .check().")
                except Exception:
                    await page.click(SEL_CHECK_TERMS, force=True)
                    _dbg(consulta_id, "Checkbox términos marcado con click(force=True).")
            except Exception as e:
                _dbg(consulta_id, f"Términos no visibles/omitidos: {e}")

            try:
                btn_terms = page.locator(SEL_BTN_TERMS_OK).first
                await btn_terms.click()
                _dbg(consulta_id, "Botón 'continuar' de términos clickeado.")
            except Exception as e:
                _dbg(consulta_id, f"No se pudo clicar continuar términos (posible skip): {e}")

            # Seleccionar Proveedores
            _dbg(consulta_id, "Seleccionando opción 'Proveedores'…")
            try:
                await page.wait_for_selector(SEL_CARD_PROV, timeout=WAIT_VISIBLE_MS)
                await page.locator(SEL_CARD_PROV).first.click(force=True)
                _dbg(consulta_id, "Card 'Proveedores' clickeado (selector preferente).")
            except Exception:
                try:
                    await page.get_by_text("Proveedores", exact=True).click()
                    _dbg(consulta_id, "Texto 'Proveedores' clickeado (fallback por texto).")
                except Exception as e:
                    _dbg(consulta_id, f"No se pudo seleccionar 'Proveedores': {e}")

            # Continuar búsqueda
            _dbg(consulta_id, "Continuando hacia la búsqueda…")
            try:
                await page.locator(SEL_BTN_TO_SEARCH).first.click()
                _dbg(consulta_id, "Botón continuar a búsqueda clickeado.")
            except Exception as e:
                _dbg(consulta_id, f"No se pudo clicar 'continuar a búsqueda' (posible skip): {e}")

            # Documento
            _dbg(consulta_id, "Rellenando número de documento…")
            await page.wait_for_selector(SEL_DOC_INPUT, timeout=WAIT_VISIBLE_MS)
            await page.fill(SEL_DOC_INPUT, "")
            await page.type(SEL_DOC_INPUT, str(numero), delay=20)
            _dbg(consulta_id, f"Documento tipeado: '{numero}'")

            # Fechas (FIN luego INICIO) con setter robusto
            _dbg(consulta_id, "Rellenando fechas (FIN luego INICIO)…")
            await _set_date(page, SEL_FECHA_FIN, fin_dt, consulta_id, "fecha_fin")
            await _set_date(page, SEL_FECHA_INI, ini_dt, consulta_id, "fecha_ini")

            # Helper local para pantallazos con label y timestamp (definido ANTES de usarlo)
            async def _snap(label: str) -> str:
                ts_shot = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                shot_path = os.path.join(
                    abs_folder,
                    f"{NOMBRE_SITIO}_{safe_num}_{ts_shot}_{label}.png"
                )
                try:
                    await page.screenshot(path=shot_path, full_page=True)
                    _dbg(consulta_id, f"Pantallazo [{label}] -> {shot_path}")
                except Exception as ee:
                    _dbg(consulta_id, f"No se pudo tomar pantallazo [{label}]: {ee}")
                return shot_path

            # Lanzar la búsqueda y esperar específicamente el PDF (ignorando XLSX si llega primero)
            _dbg(consulta_id, "Lanzando búsqueda y esperando PDF (ignorando XLSX si llega primero)…")
            try:
                try:
                    await page.locator(SEL_BTN_BUSCAR).first.click()
                    _dbg(consulta_id, "Clic en botón 'Buscar'.")
                except Exception:
                    _dbg(consulta_id, "No se pudo clicar 'Buscar'. Intentando Enter…")
                    try:
                        await page.keyboard.press("Enter")
                        _dbg(consulta_id, "Enter enviado.")
                    except Exception as e:
                        _dbg(consulta_id, f"No se pudo enviar Enter: {e}")

                await _snap("esperando_descarga_auto")
                pdf_download = await _wait_pdf_download(page, consulta_id, overall_timeout_ms=DOWNLOAD_TIMEOUT_MS)
                _dbg(consulta_id, "PDF capturado por descarga automática.")
                await _snap("descarga_auto_pdf_ok")

            except PWTimeout:
                # Fallback: botón manual de PDF
                _dbg(consulta_id, "Timeout esperando PDF automático. Probando botón manual…")
                await _snap("timeout_descarga_auto")
                try:
                    await page.wait_for_selector(SEL_BTN_MANUAL_PDF, timeout=30_000)
                    await _snap("manual_pdf_visible")
                    await page.click(SEL_BTN_MANUAL_PDF)
                    _dbg(consulta_id, "Click en botón manual PDF, esperando PDF…")
                    await _snap("manual_pdf_click")
                    pdf_download = await _wait_pdf_download(page, consulta_id, overall_timeout_ms=DOWNLOAD_TIMEOUT_MS)
                    _dbg(consulta_id, "PDF capturado por descarga manual.")
                    await _snap("descarga_manual_pdf_ok")
                except Exception as e:
                    err = f"No se obtuvo PDF (auto ni manual): {e}"
                    _dbg(consulta_id, err)
                    await _snap("error_sin_pdf")
                    raise RuntimeError(err)

            # Guardar el PDF descargado
            _dbg(consulta_id, f"Guardando PDF en: {abs_pdf}")
            await pdf_download.save_as(abs_pdf)
            _dbg(consulta_id, "PDF guardado OK.")

            _dbg(consulta_id, "Cerrando contexto/navegador…")
            await context.close()
            await browser.close()
            browser = None
            _dbg(consulta_id, "Playwright cerrado.")

        _dbg(consulta_id, f"Abrriendo PDF con PyMuPDF para analizar texto: {abs_pdf}")
        with fitz.open(abs_pdf) as doc:
            text = ""
            try:
                for i, p in enumerate(doc):
                    page_text = p.get_text("text")
                    _dbg(consulta_id, f"Página {i+1}/{len(doc)} extraída. Len={len(page_text)}")
                    text += page_text
            except Exception as e:
                _dbg(consulta_id, f"Error extrayendo texto PDF: {e}")

            # Normalizar texto
            normalized = " ".join(text.lower().split())
            _dbg(consulta_id, f"Texto normalizado (primeros 200 chars): {normalized[:200]}")

            if "no se encontraron resultados de procesos de contratación" in normalized:
                mensaje_final = "No se encontraron resultados de procesos de contratación para los criterios de búsqueda"
                score_final = 0
                _dbg(consulta_id, "Detección: SIN resultados.")
            else:
                mensaje_final = "Se encontraron resultados de procesos de contratación para los criterios de búsqueda"
                score_final = 6
                _dbg(consulta_id, "Detección: CON resultados.")

            # Captura SIEMPRE de la primera página
            _dbg(consulta_id, f"Exportando imagen de la primera página a: {abs_img}")
            page0 = doc[0]
            pix = page0.get_pixmap(matrix=fitz.Matrix(2, 2))  # zoom 2x
            pix.save(abs_img)
            _dbg(consulta_id, "Imagen exportada OK.")

        # Registrar
        _dbg(consulta_id, "Registrando resultado final en BD…")
        await _registrar(
            consulta_id, fuente, "Validada",
            mensaje_final,
            rel_img,
            score=score_final
        )

        # Eliminar el PDF (solo queda la imagen)
        if os.path.exists(abs_pdf):
            try:
                os.remove(abs_pdf)
                _dbg(consulta_id, f"PDF eliminado: {abs_pdf}")
            except Exception as e:
                _dbg(consulta_id, f"No se pudo borrar el PDF (no crítico): {e}")

        _dbg(consulta_id, "Flujo completado sin excepciones.")

    except Exception as e:
        err = f"Excepción en flujo principal: {e}"
        print(f"[{_now()}][{NOMBRE_SITIO}][consulta:{consulta_id}] {err}", flush=True)
        traceback.print_exc()
        try:
            await _registrar(
                consulta_id, fuente, "Sin Validar",
                str(e), "", score=0
            )
        finally:
            if browser:
                try:
                    await browser.close()
                    _dbg(consulta_id, "Browser cerrado en cleanup.")
                except Exception:
                    pass
