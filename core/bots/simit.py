# core/bots/simit.py
import os
import re
from datetime import datetime
from pathlib import Path

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

SIMIT_URL = "https://www.fcm.org.co/simit/#/home-public"
NOMBRE_SITIO = "simit"

# Opcional: ruta a Poppler para pdf2image (si lo tienes instalado)
POPPLER_PATH = getattr(settings, "POPPLER_PATH", os.getenv("POPPLER_PATH"))

# ---------------------------
# Helpers PDF / Imagen
# ---------------------------
def _render_pdf_first_page_pymupdf(pdf_path: str, png_path: str, zoom: float = 2.0) -> bool:
    try:
        import fitz  # PyMuPDF
        with fitz.open(pdf_path) as doc:
            if doc.page_count == 0:
                return False
            page = doc[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pix.save(png_path)
        return os.path.exists(png_path) and os.path.getsize(png_path) > 0
    except Exception:
        return False

def _render_pdf_first_page_pdf2image(pdf_path: str, png_path: str, dpi: int = 300) -> bool:
    try:
        from pdf2image import convert_from_path
        kwargs = {"dpi": dpi, "first_page": 1, "last_page": 1}
        if POPPLER_PATH:
            kwargs["poppler_path"] = POPPLER_PATH
        imgs = convert_from_path(pdf_path, **kwargs)
        if imgs:
            imgs[0].save(png_path, "PNG")
            return True
        return False
    except Exception:
        return False

async def _screenshot_pdf_embed(context, abs_pdf: str, abs_png: str) -> None:
    viewer = await context.new_page()
    file_url = Path(abs_pdf).resolve().as_uri()
    await viewer.goto(file_url, wait_until="load")
    embed = viewer.locator(
        "embed#pdf-embed, embed[type='application/x-google-chrome-pdf'], embed[type*='pdf']"
    ).first
    await embed.wait_for(state="visible", timeout=10000)
    await embed.screenshot(path=abs_png)
    await viewer.close()

def _merge_pngs_vertical(png_top: str, png_bottom: str, out_path: str, padding: int = 16) -> bool:
    """
    Une dos PNG verticalmente sin deformar:
    - No reescala: crea un lienzo ancho = max(widths) y centra cada imagen.
    - Devuelve True si genera out_path correctamente.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        if not (os.path.exists(png_top) and os.path.getsize(png_top) > 0):
            return False
        if not (os.path.exists(png_bottom) and os.path.getsize(png_bottom) > 0):
            return False

        top = Image.open(png_top).convert("RGB")
        bot = Image.open(png_bottom).convert("RGB")

        max_w = max(top.width, bot.width)
        total_h = top.height + bot.height + padding

        canvas = Image.new("RGB", (max_w, total_h), (255, 255, 255))

        # Centrar horizontalmente
        x_top = (max_w - top.width) // 2
        x_bot = (max_w - bot.width) // 2

        canvas.paste(top, (x_top, 0))
        canvas.paste(bot, (x_bot, top.height + padding))

        # Etiquetas sutiles (opcional)
        try:
            draw = ImageDraw.Draw(canvas)
            font = ImageFont.load_default()
            draw.text((10, 10), "Portal SIMIT (vista general)", fill=(0, 0, 0), font=font)
            draw.text((10, top.height + padding + 10), "Estado de cuenta / PDF", fill=(0, 0, 0), font=font)
        except Exception:
            pass

        canvas.save(out_path, "PNG")
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception:
        return False

# ---------------------------
# Mapeo flexible de tipo de doc
# ---------------------------
DOC_VALUE_BY_CODE = {
    "CC": "1",
    "TI": "2",
    "CE": "3",
    "NIT": "4",
    "SD": "5",
    "PASAPORTE": "6",
    "CD": "7",  # Carnet Diplomático
    "RC": "8",
    "VENEZOLANA": "9",
    "ECUATORIANA": "10",
    "PPT": "11",
}
DOC_LABEL_BY_CODE = {
    "CC": "Cédula",
    "TI": "Tarjeta Identidad",
    "CE": "Cédula Extranjeria",
    "NIT": "Nit",
    "SD": "Sin documento",
    "PASAPORTE": "Pasaporte",
    "CD": "Carnet Diplomático",
    "RC": "Registro Civil",
    "VENEZOLANA": "Cédula Venezolana",
    "ECUATORIANA": "Cédula Ecuatoriana",
    "PPT": "Permiso Protección Temporal",
}

# ---------------------------
# Bot principal
# ---------------------------
async def consultar_simit(cedula: str, consulta_id: int, tipo_doc: str = "CC"):
    """
    - Consulta SIMIT.
    - Si NO hay pendientes: descarga 'Paz y salvo' y genera PNG de la 1ra página.
    - Si HAY pendientes: screenshot de la página, descarga/viste PDF y genera PNG,
      luego UNE AMBAS evidencias en un solo PNG.
    - Guarda un Resultado con el PNG final (o el mejor disponible).
    """
    # Carpetas / nombres base
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Fuente (resolver antes del loop, para usarla en errores finales)
    fuente_obj = await sync_to_async(lambda: Fuente.objects.filter(nombre=NOMBRE_SITIO).first())()
    if not fuente_obj:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin validar", mensaje=f"No se encontró la fuente '{NOMBRE_SITIO}'", archivo=""
        )
        return

    # Normalizar tipo_doc
    td = (tipo_doc or "").strip().upper()
    td_value = DOC_VALUE_BY_CODE.get(td, td if td.isdigit() else None)
    td_label = DOC_LABEL_BY_CODE.get(td, None)

    max_intentos = 3
    last_error = None

    for intento in range(1, max_intentos + 1):
        browser = None
        page = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=False)
                context = await browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 960})
                page = await context.new_page()

                # 1) Abrir SIMIT
                await page.goto(SIMIT_URL, timeout=120000, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass

                # 2) Cerrar modal informativo si aparece
                try:
                    await page.locator("button.modal-info-close").first.click(timeout=2000)
                except Exception:
                    pass

                # 3) Ingresar cédula y consultar
                await page.wait_for_selector("#txtBusqueda", timeout=20000)
                await page.fill("#txtBusqueda", str(cedula))
                await page.click("#consultar")
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass

                # 4) Detectar resultado
                cont_sin = page.locator("text=/no\\s+(posee|tienes?).*pendientes\\s+de\\s+pago/i").first
                tiene_paz = await cont_sin.count() > 0

                # --- rutas base de evidencia ---
                if tiene_paz:
                    base = f"simit_pazysalvo_{cedula}_{ts}"
                else:
                    base = f"simit_estado_{cedula}_{ts}"

                abs_pdf = os.path.join(absolute_folder, f"{base}.pdf")
                rel_pdf = os.path.join(relative_folder, f"{base}.pdf").replace("\\", "/")
                abs_png = os.path.join(absolute_folder, f"{base}.png")          # PNG principal (PDF o visor)
                rel_png = os.path.join(relative_folder, f"{base}.png").replace("\\", "/")

                # Para el caso con pendientes:
                abs_png_page = os.path.join(absolute_folder, f"{base}_page.png")
                abs_png_merged = os.path.join(absolute_folder, f"{base}_merged.png")
                rel_png_merged = os.path.join(relative_folder, f"{base}_merged.png").replace("\\", "/")

                if tiene_paz:
                    # -----------------------
                    # 5A) PAZ Y SALVO
                    # -----------------------
                    try:
                        mensaje = (await cont_sin.inner_text()).strip()
                    except Exception:
                        mensaje = "No posee pendientes de pago ante SIMIT."

                    # Botón 'Descargar paz y salvo'
                    btn_paz = page.locator("a:has-text('Descargar paz y salvo')").first
                    await btn_paz.click(timeout=8000)

                    # Modal: seleccionar tipoDoc y descargar
                    try:
                        await page.wait_for_selector("#tipoDoc", timeout=10000)
                        if td_value:
                            try:
                                await page.select_option("#tipoDoc", value=str(td_value))
                            except Exception:
                                if td_label:
                                    await page.select_option("#tipoDoc", label=td_label)
                        elif td_label:
                            await page.select_option("#tipoDoc", label=td_label)
                    except Exception:
                        pass

                    # Botón 'Descargar' dentro del modal
                    try:
                        async with page.expect_download(timeout=120000) as dl:
                            await page.locator("button.btn.btn-primary.btn-sm.btn-block:has-text('Descargar')").first.click()
                        d = await dl.value
                        await d.save_as(abs_pdf)
                    except Exception:
                        # si falla la descarga, seguimos con visor/screenshot abajo
                        pass

                    # Evidencia PNG (página 1 del PDF, o visor, fallback)
                    rendered = False
                    if os.path.exists(abs_pdf) and os.path.getsize(abs_pdf) > 0:
                        rendered = _render_pdf_first_page_pymupdf(abs_pdf, abs_png, zoom=2.0)
                        if not rendered:
                            rendered = _render_pdf_first_page_pdf2image(abs_pdf, abs_png, dpi=300)
                    if not rendered:
                        try:
                            await _screenshot_pdf_embed(context, abs_pdf, abs_png)
                            rendered = os.path.exists(abs_png) and os.path.getsize(abs_png) > 0
                        except Exception:
                            # último recurso: screenshot de la página actual
                            try:
                                await page.screenshot(path=abs_png, full_page=True)
                                rendered = True
                            except Exception:
                                rendered = False

                    score = 1
                    final_rel_png = rel_png  # paz y salvo no necesita merge

                else:
                    # -----------------------
                    # 5B) PENDIENTES
                    # -----------------------
                    # (1) Screenshot de la página ANTES de abrir el modal
                    try:
                        await page.screenshot(path=abs_png_page, full_page=True)
                    except Exception:
                        pass  # no interrumpe el flujo

                    # (2) Total
                    try:
                        total_node = page.locator("div:has(> label:has-text('Total')) span strong").first
                        total_txt = (await total_node.inner_text()).strip()
                        total_txt = re.sub(r"\s+", " ", total_txt)
                        mensaje = f"Total: {total_txt}"
                    except Exception:
                        mensaje = "Se encontraron pagos pendientes ante el SIMIT."

                    # (3) Abrir modal 'Guardar estado'
                    btn_guardar = page.locator("a:has-text('Guardar estado')").first
                    await btn_guardar.click(timeout=15000)

                    # (4) Intentar descarga directa del PDF o captura del visor
                    rendered = False
                    try:
                        async with page.expect_download(timeout=180000) as dl:
                            await page.locator("a.btn.btn-outline-primary.btn-block.btn-sm:has-text('Descargar PDF')").first.click()
                        d = await dl.value
                        await d.save_as(abs_pdf)

                        # Render PNG del PDF
                        rendered = _render_pdf_first_page_pymupdf(abs_pdf, abs_png, zoom=2.0)
                        if not rendered:
                            rendered = _render_pdf_first_page_pdf2image(abs_pdf, abs_png, dpi=300)
                        if not rendered:
                            await _screenshot_pdf_embed(context, abs_pdf, abs_png)
                            rendered = os.path.exists(abs_png) and os.path.getsize(abs_png) > 0

                    except Exception:
                        # (5) Fallback: visor en nueva pestaña
                        try:
                            async with context.expect_event("page", timeout=10000) as newp:
                                await page.locator("a.btn.btn-outline-primary.btn-block.btn-sm:has-text('Descargar PDF')").first.click()
                            pdf_page = await newp.value
                            await pdf_page.wait_for_load_state("load")
                            try:
                                emb = pdf_page.locator("embed[type*='pdf']").first
                                await emb.wait_for(state="visible", timeout=8000)
                                await emb.screenshot(path=abs_png)
                            except Exception:
                                await pdf_page.screenshot(path=abs_png, full_page=True)
                            rendered = os.path.exists(abs_png) and os.path.getsize(abs_png) > 0
                            await pdf_page.close()
                        except Exception:
                            rendered = False

                    score = 5

                    # (6) Merge: si existen la captura de página y la del PDF, genera _merged.png
                    final_rel_png = None
                    merged_ok = False
                    try:
                        if os.path.exists(abs_png_page) and os.path.getsize(abs_png_page) > 0 and \
                           os.path.exists(abs_png) and os.path.getsize(abs_png) > 0:
                            merged_ok = _merge_pngs_vertical(abs_png_page, abs_png, abs_png_merged, padding=16)
                            if merged_ok:
                                final_rel_png = rel_png_merged
                    except Exception:
                        merged_ok = False

                    # Si no se pudo mergear, usa la mejor evidencia disponible
                    if not final_rel_png:
                        if os.path.exists(abs_png) and os.path.getsize(abs_png) > 0:
                            final_rel_png = rel_png
                        elif os.path.exists(abs_png_page) and os.path.getsize(abs_png_page) > 0:
                            final_rel_png = os.path.join(relative_folder, f"{base}_page.png").replace("\\", "/")
                        else:
                            final_rel_png = ""  # sin evidencia gráfica

                # 6) Guardar resultado (PNG final como evidencia; el PDF queda en carpeta)
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validada",
                    mensaje=mensaje,
                    archivo=final_rel_png
                )

                await browser.close()
                return

        except Exception as e:
            last_error = e
            try:
                if page is not None:
                    err_png = os.path.join(absolute_folder, f"simit_error_{cedula}_{ts}.png")
                    await page.screenshot(path=err_png, full_page=True)
            except Exception:
                pass
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass

    # 7) Error definitivo
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=0,
        estado="Sin validar",
        mensaje=str(last_error) if last_error else "Fallo al consultar SIMIT",
        archivo=""
    )