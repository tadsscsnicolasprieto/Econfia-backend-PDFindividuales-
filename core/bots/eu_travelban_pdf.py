# consulta/eu_travelban_pdf.py (versión async con preview PNG)
import os
import re
from urllib.parse import urlparse
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente
from core.utils.pdf_preview import pdf_first_page_to_png  # <- IMPORTANTE

URL = "https://www.sanctionsmap.eu/#/main/travel/ban"
NOMBRE_SITIO = "eu_travelbans_pdf"


def _content_disposition_filename(cd: str) -> str | None:
    if not cd:
        return None
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^\";]+)"?', cd, flags=re.IGNORECASE)
    return m.group(1) if m else None


async def consultar_eu_travelban_pdf(consulta_id: int, nombre_completo: str):
    """
    Abre la página de Travel Bans, obtiene el enlace 'PDF' y descarga
    el archivo en MEDIA_ROOT/resultados/<consulta_id>/YYYYmmdd_HHMMSS_nombre.pdf.
    Luego genera un PNG de la primera página y lo guarda como evidencia en Resultado.archivo.
    
    Registros:
      - Éxito: score=0, estado="Validada", mensaje="PDF: <ruta_rel_pdf>", archivo=<ruta_rel_png>
      - Falla: score=10, estado="Sin Validar", mensaje="el pdf no pudo ser descargado", archivo=""
    """
    navegador = None
    context = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=10,
            estado="Sin Validar", mensaje="el pdf no pudo ser descargado", archivo=""
        )
        return

    # 2) Carpetas salida
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    # Variables para registrar al final
    pdf_abs = ""
    pdf_rel = ""

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            context = await navegador.new_context()
            page = await context.new_page()

            await page.goto(URL, wait_until="networkidle")

            # Cookies (best-effort)
            for sel in [
                "button:has-text('Accept all cookies')",
                "button:has-text('I accept')",
                "button:has-text('Accept')",
                "button#accept-all-cookies",
            ]:
                try:
                    await page.locator(sel).first.click(timeout=1200)
                    break
                except Exception:
                    pass

            # Link 'PDF'
            link = page.locator("a[target='_blank']:has-text('PDF')").first
            await link.wait_for(state="visible", timeout=10000)
            href = await link.get_attribute("href")
            if not href:
                await navegador.close(); navegador = None
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj, score=10,
                    estado="Sin Validar", mensaje="el pdf no pudo ser descargado", archivo=""
                )
                return

            # Descarga directa heredando cookies
            resp = await context.request.get(href, headers={"Accept": "application/pdf"})
            if not resp.ok:
                await navegador.close(); navegador = None
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj, score=10,
                    estado="Sin Validar", mensaje="el pdf no pudo ser descargado", archivo=""
                )
                return

            # Chequeo simple de tipo (no obligatorio, pero ayuda)
            ctype = (resp.headers.get("content-type") or resp.headers.get("Content-Type") or "").lower()
            if "pdf" not in ctype:
                # Puede seguir siendo un PDF aunque no lo diga, pero si quieres ser estricto:
                pass

            # Nombre de archivo (header o URL)
            headers = resp.headers
            cd = headers.get("content-disposition") or headers.get("Content-Disposition")
            filename = _content_disposition_filename(cd)

            if not filename:
                parsed = urlparse(href)
                base = os.path.basename(parsed.path) or "TravelBans.pdf"
                if not base.lower().endswith(".pdf"):
                    base += ".pdf"
                filename = base

            # Sanitizar y garantizar .pdf
            filename = re.sub(r"[^\w\-. ]+", "_", filename)
            if not filename.lower().endswith(".pdf"):
                filename = re.sub(r"\.[a-z0-9]+$", "", filename, flags=re.IGNORECASE) + ".pdf"

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename_ts = f"{ts}_{filename}"

            pdf_abs = os.path.join(absolute_folder, filename_ts)
            pdf_rel = os.path.join(relative_folder, filename_ts)

            body_bytes = await resp.body()
            with open(pdf_abs, "wb") as f:
                f.write(body_bytes)

            await navegador.close()
            navegador = None

        # 3) Verificar y generar preview PNG
        if os.path.exists(pdf_abs) and os.path.getsize(pdf_abs) > 0:
            try:
                # Genera preview en la misma carpeta con sufijo _preview.png
                png_abs, png_rel = pdf_first_page_to_png(pdf_abs, max_width=1400)

                # Registrar OK: archivo = PNG (para consolidado), mensaje = ruta del PDF
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj, score=0,
                    estado="Validada", mensaje=f"PDF: {pdf_rel}", archivo=png_rel
                )
            except Exception as e:
                # Si falla el preview pero el PDF existe, registramos el PDF como evidencia (fallback)
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj, score=0,
                    estado="Validada",
                    mensaje=f"PDF sin preview ({e}). PDF: {pdf_rel}",
                    archivo=pdf_rel
                )
        else:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj, score=10,
                estado="Sin Validar", mensaje="el pdf no pudo ser descargado", archivo=""
            )

    except Exception as e:
        try:
            if navegador is not None:
                await navegador.close()
        except Exception:
            pass
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=10,
            estado="Sin Validar", mensaje="el pdf no pudo ser descargado", archivo=""
        )
