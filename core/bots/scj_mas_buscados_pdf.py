# core/bots/scj_mas_buscados_pdf.py
import os
import re
from datetime import datetime
from urllib.parse import quote_plus

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright
from PyPDF2 import PdfReader, PdfWriter

from core.models import Resultado, Fuente

NOMBRE_SITIO = "scj_mas_buscados_pdf"

# Noticia “más buscados” (hoy está caída en 404)
ORIG_URL = (
    "https://scj.gov.co/es/noticias/estos-son-los-homicidas-m%C3%A1s-buscados-bogot%C3%A1-"
    "hay-recompensa-hasta-50-millones-pesos"
)
BASE = "https://scj.gov.co"

# Selectores
SEL_COOKIE_ACEPTAR = "button.agree-button.eu-cookie-compliance-secondary-button"
SEL_INPUT_SEARCH   = "input#query, input.form-search[name='query']"
SEL_RESULTS_HINTS  = [
    "main .view-content",
    ".search-results",
    "#block-mainpagecontent",
    "article",
    ".region-content",
    ".layout-content",
]

# Tiempos
NAV_TIMEOUT_MS   = 120_000
WAIT_RESULTS_MS  = 30_000


def _safe_rel(path: str) -> str:
    return path.replace("\\", "/") if path else ""


async def _guardar_resultado(consulta_id, fuente, estado, mensaje, archivo_rel):
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente,
        score=0,
        estado=estado,   # "Validada" / "error"
        mensaje=mensaje,
        archivo=_safe_rel(archivo_rel),
    )


async def consultar_scj_mas_buscados_pdf(consulta_id: int, nombre: str, apellido: str):
    """
    Flujo:
      1) Abre ORIG_URL (headful) y toma screenshot full-page.
      2) Si es 404 -> guardar PNG único y terminar.
      3) Si no, intenta búsqueda (input o fallback por URL), espera resultados y crea PDF
         (en un segundo navegador headless), recortado a 2 páginas.
    """
    # 0) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await _guardar_resultado(
            consulta_id, None, "error",
            f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", ""
        )
        return

    # 1) Carpetas / nombres
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    q = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()
    safe_q = re.sub(r"\s+", "_", q) or "consulta"

    # Archivos
    png_inicial_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe_q}_{ts}.png")
    png_inicial_rel = os.path.join(relative_folder, os.path.basename(png_inicial_abs))

    tmp_pdf_abs   = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe_q}_{ts}_FULL.pdf")
    final_pdf_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe_q}_{ts}.pdf")
    final_pdf_rel = os.path.join(relative_folder, os.path.basename(final_pdf_abs))

    # 2) Primer navegador (headful) para que “se vea”
    browser = None
    ctx = None
    page = None
    used_fallback = False
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,  # visible para que veas que abre
                args=["--disable-dev-shm-usage"]
            )
            ctx = await browser.new_context(viewport={"width": 1366, "height": 900}, locale="es-CO")
            page = await ctx.new_page()

            await page.goto(ORIG_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

            # Screenshot completo apenas carga (evidencia del estado real)
            await page.screenshot(path=png_inicial_abs, full_page=True)

            # Aceptar cookies si aparece
            try:
                btn = page.locator(SEL_COOKIE_ACEPTAR)
                if await btn.count():
                    await btn.first.click(timeout=1500)
            except Exception:
                pass

            # ¿Es 404?
            is_404 = False
            try:
                h1 = page.locator("h1.display-3.text-danger, h1.display-3")
                body_text = (await page.inner_text("body") or "").lower()
                if (await h1.count() and "404" in (await h1.first.inner_text() or "").lower()) \
                   or "página no encontrada" in body_text \
                   or "pagina no encontrada" in body_text \
                   or "error 404" in body_text:
                    is_404 = True
            except Exception:
                pass

            if is_404:
                # Reutilizamos el screenshot inicial (único) como evidencia
                await ctx.close()
                await browser.close()
                browser = ctx = page = None

                await _guardar_resultado(
                    consulta_id, fuente_obj, "Validada",
                    "ERROR 404 – Página no encontrada en SCJ. Se adjunta evidencia.",
                    png_inicial_rel,
                )
                return

            # Si no es 404, intentar búsqueda (no bloquea si no hay input)
            searched = False
            try:
                await page.wait_for_selector(SEL_INPUT_SEARCH, timeout=5000)
                await page.fill(SEL_INPUT_SEARCH, q)
                await page.keyboard.press("Enter")
                searched = True
            except Exception:
                used_fallback = True
                for url in (
                    f"{BASE}/es/search?search={quote_plus(q)}",
                    f"{BASE}/es/buscar?search={quote_plus(q)}",
                    f"{BASE}/search?search={quote_plus(q)}",
                ):
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                        searched = True
                        break
                    except Exception:
                        continue

            # Esperar un contenedor de resultados si lo hay (no bloqueante)
            if searched:
                for sel in SEL_RESULTS_HINTS:
                    try:
                        await page.wait_for_selector(sel, timeout=WAIT_RESULTS_MS)
                        break
                    except Exception:
                        continue

            # Guardar la URL de destino ANTES de cerrar el contexto
            destino_url = page.url if searched else ORIG_URL

            # Cerrar headful
            await ctx.close()
            await browser.close()
            browser = ctx = page = None

            # 3) Segundo navegador (headless) SOLO para PDF
            browser2 = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            ctx2 = await browser2.new_context(viewport={"width": 1366, "height": 2000}, locale="es-CO")
            page2 = await ctx2.new_page()

            await page2.goto(destino_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

            # PDF completo del estado actual
            await page2.pdf(
                path=tmp_pdf_abs,
                format="A4",
                print_background=True,
                margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"},
                prefer_css_page_size=False,
            )

            await ctx2.close()
            await browser2.close()

        # 4) Recortar a 2 páginas (si falla, se deja el FULL)
        try:
            reader = PdfReader(tmp_pdf_abs)
            writer = PdfWriter()
            for i in range(min(2, len(reader.pages))):
                writer.add_page(reader.pages[i])
            with open(final_pdf_abs, "wb") as out_f:
                writer.write(out_f)
            try:
                os.remove(tmp_pdf_abs)
            except Exception:
                pass
        except Exception:
            # dejar el full como final
            final_pdf_abs = tmp_pdf_abs
            final_pdf_rel = os.path.join(relative_folder, os.path.basename(final_pdf_abs))

        msg = f"SCJ – búsqueda para '{q}'." + (" (Usó búsqueda por URL)" if used_fallback else "")
        await _guardar_resultado(consulta_id, fuente_obj, "Validada", msg, final_pdf_rel)

    except Exception as e:
        # Evidencia si algo alcanzó a abrir
        try:
            if page:
                await page.screenshot(path=png_inicial_abs, full_page=True)
        except Exception:
            pass
        finally:
            try:
                if ctx:
                    await ctx.close()
            except Exception:
                pass
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass

        await _guardar_resultado(consulta_id, fuente_obj, "error", str(e), "")
