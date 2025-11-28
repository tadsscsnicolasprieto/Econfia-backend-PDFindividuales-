# core/bots/afiliados_eps.py
import os
import traceback
from datetime import datetime
from typing import Optional

from playwright.async_api import async_playwright, Page, Dialog, TimeoutError as PlaywrightTimeoutError
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Consulta, Resultado, Fuente

# Intento de "captura antes de aceptar alert" mediante:
# 1) capturar el texto del dialog cuando aparece,
# 2) aceptar el dialog para desbloquear la página,
# 3) tomar screenshot de la página ya sin el dialog,
# 4) generar una imagen compuesta que superpone visualmente el texto de la alerta
#    sobre la captura (simulando cómo se vería la alerta antes de aceptarla).
# Nota: las alertas nativas no forman parte del DOM y bloquean la ejecución JS,
# por eso no es posible tomar una captura real del cuadro nativo mientras está abierto.
# Esta técnica crea una imagen diagnóstica que incluye el texto de la alerta encima
# de la captura de la página inmediatamente posterior a su aceptación.
URL = "https://www.minsalud.gov.co/paginas/consulta-afiliados.aspx"
NOMBRE_SITIO = "afiliados_eps"

TIPO_DOC_MAP = {
    "CC": "1",
    "TI": "2",
    "CE": "4",
    "PAS": "7",
    "PEP": "5",
}


def _normrel(p: str) -> str:
    return (p or "").replace("\\", "/")


async def consultar_afiliados_eps(consulta_id: int, cedula: str, tipo_doc: str):
    """
    Bot para consultar afiliados EPS con captura "simulada" de la alerta antes de aceptarla.
    """
    async def _get_fuente(nombre: str) -> Optional[Fuente]:
        return await sync_to_async(lambda: Fuente.objects.filter(nombre=nombre).first())()

    async def _crear_resultado(estado: str, archivo: str, mensaje: str, fuente, score: float = 0):
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente,
            estado=estado,
            archivo=_normrel(archivo),
            mensaje=mensaje,
            score=score,
        )

    NO_ENCONTRADO_MSG = (
        "No se encontraron registros con los datos ingresados, "
        "por favor verifique la información e intente de nuevo"
    )
    ENCONTRADO_MSG = "Se encontraron registros con los datos ingresados"

    try:
        # validar existencia de la consulta y obtener fuente
        await sync_to_async(Consulta.objects.get)(id=consulta_id)
        fuente = await _get_fuente(NOMBRE_SITIO)

        # validar tipo de documento
        tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "CC").upper())
        if not tipo_doc_val:
            await _crear_resultado("Sin Validar", "", f"Tipo de documento no soportado: {tipo_doc!r}", fuente, score=0)
            return

        # preparar rutas
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shot_name = f"{NOMBRE_SITIO}_{cedula}_{ts}.png"
        abs_png = os.path.join(absolute_folder, shot_name)
        rel_png = os.path.join(relative_folder, shot_name)
        # archivo adicional que contendrá la "simulación" de la alerta antes de aceptar
        shot_alert_before_name = f"{NOMBRE_SITIO}_{cedula}_{ts}_alert_before.png"
        abs_png_alert_before = os.path.join(absolute_folder, shot_alert_before_name)
        rel_png_alert_before = os.path.join(relative_folder, shot_alert_before_name)
        html_debug = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{cedula}_{ts}.html")

        # estado para capturar texto de alert()
        alerta_texto = {"value": None}

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page: Page = await browser.new_page(viewport={"width": 1440, "height": 900})

            # handler para dialog() (alert/confirm/prompt)
            # Guardamos el texto en alerta_texto y aceptamos el dialog inmediatamente.
            async def on_dialog(dialog: Dialog):
                try:
                    alerta_texto["value"] = dialog.message
                    # aceptar para desbloquear la página y permitir capturas
                    await dialog.accept()
                except Exception:
                    try:
                        await dialog.dismiss()
                    except Exception:
                        pass

            page.on("dialog", on_dialog)

            # navegar a la página
            await page.goto(URL, wait_until="domcontentloaded", timeout=120_000)

            # --- Rellenar formulario ---
            # seleccionar tipo de documento
            try:
                await page.select_option('select[name="ddltipoidentificacion"]', tipo_doc_val)
            except Exception:
                # fallback por JS si select_option falla
                try:
                    await page.evaluate(
                        """(v) => {
                            const s = document.querySelector('select[name="ddltipoidentificacion"], select#ddltipoidentificacion');
                            if (s) { s.value = v; s.dispatchEvent(new Event('change', { bubbles: true })); }
                        }""",
                        tipo_doc_val
                    )
                except Exception:
                    pass

            # rellenar número de identificación
            try:
                await page.fill('input[id="txtnumeroidentificacion"]', str(cedula))
            except Exception:
                try:
                    await page.fill('input[name="txtnumeroidentificacion"]', str(cedula))
                except Exception:
                    # fallback: set via JS
                    await page.evaluate(
                        """(v) => {
                            const el = document.getElementById('txtnumeroidentificacion') || document.querySelector('input[name="txtnumeroidentificacion"]');
                            if (el) { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }
                        }""",
                        str(cedula)
                    )

            # forzar focus/blur y disparar eventos para que la página habilite el botón si aplica
            try:
                await page.focus('input[id="txtnumeroidentificacion"]')
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(200)
                await page.evaluate("""
                    () => {
                        const el = document.getElementById('txtnumeroidentificacion') || document.querySelector('input[name="txtnumeroidentificacion"]');
                        if (el) { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }
                    }
                """)
                await page.wait_for_timeout(200)
            except Exception:
                pass

            # eliminar overlays/modales que puedan bloquear el click
            try:
                await page.evaluate("""
                    () => {
                        const sel = ['.swal2-container', '.swal2-backdrop', '.modal-backdrop', '.overlay', '.cookie-banner', '#cookie-consent'];
                        sel.forEach(s => document.querySelectorAll(s).forEach(e => { try { e.remove(); } catch(e){} }));
                        document.querySelectorAll('[style*="pointer-events: none"]').forEach(e => { try { e.style.pointerEvents = 'auto'; } catch(e){} });
                    }
                """)
                await page.wait_for_timeout(150)
            except Exception:
                pass

            # guardar longitud inicial del HTML para detectar cambios posteriores
            try:
                initial_html = await page.content()
                initial_len = len(initial_html or "")
            except Exception:
                initial_len = 0

            # localizar botón "Consultar" con selector robusto
            btn_locator = page.locator('button:has-text("Consultar"), input[type="submit"][value*="Consultar"], a:has-text("Consultar")').first

            # esperar que el botón sea visible y no esté deshabilitado
            try:
                await btn_locator.wait_for(state="visible", timeout=5000)
            except Exception:
                pass

            # intentar click con reintentos y fallbacks
            clicked = False
            for attempt in range(3):
                try:
                    try:
                        await btn_locator.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    await btn_locator.click(timeout=4000)
                    clicked = True
                    break
                except Exception:
                    # fallback: click via JS sobre el primer elemento que coincida
                    try:
                        handle = await btn_locator.element_handle()
                        if handle:
                            await page.evaluate("(el) => el.click()", handle)
                            clicked = True
                            break
                    except Exception:
                        pass

                    # fallback: intentar enviar el formulario por JS (submit)
                    try:
                        await page.evaluate("""
                            () => {
                                const f = document.querySelector('form');
                                if (f) { try { f.submit(); } catch(e) { /* ignore */ } }
                            }
                        """)
                        clicked = True
                        break
                    except Exception:
                        pass

                    # fallback: intentar __doPostBack (ASP.NET) con nombres comunes
                    try:
                        await page.evaluate("""
                            () => {
                                const candidates = [
                                    'ctl00$ContentPlaceHolder1$btnConsultar',
                                    'ctl00$MainContent$btnConsultar',
                                    'btnConsultar',
                                    'ctl00$ContentPlaceHolder1$btnBuscar'
                                ];
                                for (const c of candidates) {
                                    try { if (typeof __doPostBack === 'function') { __doPostBack(c, ''); return; } } catch(e) {}
                                }
                            }
                        """)
                        clicked = True
                        break
                    except Exception:
                        pass

                    # fallback: teclado (Tab + Enter)
                    try:
                        await page.keyboard.press("Tab")
                        await page.wait_for_timeout(150)
                        await page.keyboard.press("Enter")
                        await page.wait_for_timeout(300)
                        clicked = True
                        break
                    except Exception:
                        pass

                    await page.wait_for_timeout(500)

            # si no se logró click ni fallback, guardar evidencia y retornar error
            if not clicked:
                try:
                    html = await page.content()
                    with open(html_debug, "w", encoding="utf-8") as fh:
                        fh.write(html)
                except Exception:
                    pass
                try:
                    await page.screenshot(path=abs_png, full_page=True)
                except Exception:
                    pass
                await _crear_resultado("Sin Validar", rel_png, "No se pudo pulsar el botón Consultar (evidencia guardada).", fuente, score=0)
                try:
                    await browser.close()
                except Exception:
                    pass
                return

            # --- Después de pulsar: confirmar que hubo respuesta ---
            # Esperamos un corto periodo para que aparezca dialog (alert) y lo capture el handler on_dialog.
            # El handler aceptará el dialog automáticamente; guardamos el texto en alerta_texto.
            # Inmediatamente después tomamos screenshot de la página (sin la alerta nativa).
            try:
                # esperar un poco para que el dialog aparezca y sea manejado por on_dialog
                await page.wait_for_timeout(1200)
            except Exception:
                pass

            # esperar networkidle breve para que XHR termine
            try:
                await page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass

            # tomar screenshot de la página (después de aceptar la alerta nativa)
            try:
                await page.screenshot(path=abs_png, full_page=True)
            except Exception:
                try:
                    await page.screenshot(path=abs_png)
                except Exception:
                    pass

            # guardar HTML de diagnóstico
            try:
                html = await page.content()
                with open(html_debug, "w", encoding="utf-8") as fh:
                    fh.write(html)
            except Exception:
                pass

            # --- Crear imagen compuesta que simula la alerta antes de aceptar ---
            # Si capturamos texto de alerta, generamos una imagen adicional que superpone
            # un recuadro con el texto de la alerta sobre la captura tomada (simulación).
            if alerta_texto.get("value"):
                try:
                    from PIL import Image, ImageDraw, ImageFont
                    try:
                        img = Image.open(abs_png).convert("RGBA")
                    except Exception:
                        img = None

                    if img is not None:
                        draw = ImageDraw.Draw(img)
                        W, H = img.size
                        # caja de alerta simulada: ancho 80% y centrada
                        box_w = int(W * 0.8)
                        box_h = int(H * 0.18)
                        box_x = int((W - box_w) / 2)
                        box_y = int(H * 0.12)
                        # fondo semitransparente
                        overlay = Image.new("RGBA", img.size, (255, 255, 255, 0))
                        od = ImageDraw.Draw(overlay)
                        rect_color = (255, 255, 255, 230)  # casi opaco
                        border_color = (0, 0, 0, 200)
                        od.rectangle([box_x, box_y, box_x + box_w, box_y + box_h], fill=rect_color, outline=border_color)
                        # texto de alerta
                        text = alerta_texto["value"]
                        # elegir fuente por defecto
                        try:
                            font = ImageFont.truetype("arial.ttf", 16)
                        except Exception:
                            font = ImageFont.load_default()
                        # ajustar texto en líneas
                        max_w = box_w - 24
                        words = text.split()
                        lines = []
                        cur = ""
                        for w in words:
                            test = (cur + " " + w).strip()
                            tw, th = od.textsize(test, font=font)
                            if tw <= max_w:
                                cur = test
                            else:
                                if cur:
                                    lines.append(cur)
                                cur = w
                        if cur:
                            lines.append(cur)
                        # escribir título (origen) y líneas
                        title = "www.minsalud.gov.co dice"
                        od.text((box_x + 12, box_y + 8), title, fill=(0, 0, 0), font=font)
                        y = box_y + 8 + od.textsize(title, font=font)[1] + 6
                        for line in lines:
                            od.text((box_x + 12, y), line, fill=(0, 0, 0), font=font)
                            y += od.textsize(line, font=font)[1] + 4
                        # botón simulado "Aceptar"
                        btn_w = 100
                        btn_h = 30
                        btn_x = box_x + box_w - btn_w - 16
                        btn_y = box_y + box_h - btn_h - 12
                        od.rectangle([btn_x, btn_y, btn_x + btn_w, btn_y + btn_h], fill=(0, 102, 204), outline=(0, 0, 0))
                        btn_text = "Aceptar"
                        tw, th = od.textsize(btn_text, font=font)
                        od.text((btn_x + (btn_w - tw) / 2, btn_y + (btn_h - th) / 2), btn_text, fill=(255, 255, 255), font=font)
                        # combinar overlay con la imagen original
                        combined = Image.alpha_composite(img, overlay)
                        # guardar la imagen simulada
                        combined.convert("RGB").save(abs_png_alert_before, "PNG")
                    else:
                        # si no se pudo abrir la imagen original, no hacemos la composición
                        abs_png_alert_before = abs_png
                        rel_png_alert_before = rel_png
                except Exception:
                    # si falla PIL o la composición, caemos a usar la captura normal
                    abs_png_alert_before = abs_png
                    rel_png_alert_before = rel_png
            else:
                # no hubo alerta: la "simulación" es la misma captura
                abs_png_alert_before = abs_png
                rel_png_alert_before = rel_png

            # --- Determinar resultado final usando alert o detección DOM/HTML ---
            score = 0
            mensaje = NO_ENCONTRADO_MSG

            if alerta_texto["value"]:
                txt = alerta_texto["value"].lower()
                if "no se encontraron" in txt or "no se encontraron registros" in txt:
                    score = 0
                    mensaje = NO_ENCONTRADO_MSG
                else:
                    score = 1
                    mensaje = alerta_texto["value"]
            else:
                # buscar indicios en HTML
                try:
                    html_low = (await page.content()).lower()
                    if "no se encontraron registros" in html_low or "no se encontraron" in html_low:
                        score = 0
                        mensaje = NO_ENCONTRADO_MSG
                    elif any(k in html_low for k in ("eps", "asignado", "entidad", "eps asignada", "eps receptora")):
                        score = 10
                        mensaje = ENCONTRADO_MSG
                    else:
                        score = 1
                        mensaje = "No se detectó alerta ni contenido claro de resultado. Revisar captura."
                except Exception:
                    score = 1
                    mensaje = "No se pudo analizar el HTML resultante."

            # Guardar en BD: preferimos guardar la imagen que simula la alerta antes de aceptar
            archivo_a_guardar = rel_png_alert_before if os.path.exists(abs_png_alert_before) else rel_png
            await _crear_resultado("Validada", archivo_a_guardar, mensaje, fuente, score=score)

            try:
                await browser.close()
            except Exception:
                pass

    except Exception as e:
        # en caso de excepción, intentar guardar resultado de error
        try:
            fuente = await _get_fuente(NOMBRE_SITIO)
        except Exception:
            fuente = None
        try:
            await _crear_resultado("Sin Validar", "", f"Excepción: {e}", fuente, score=0)
        except Exception:
            pass
        # opcional: re-raise para logging externo
        # raise
