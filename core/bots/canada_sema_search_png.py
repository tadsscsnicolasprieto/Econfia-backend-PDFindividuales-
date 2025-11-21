# bots/canada_sema_pdf_hits.py  (versión async, adaptada a BD)
import os
import re
import zipfile
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

# Ajusta a tu app real
from core.models import Resultado, Fuente

URL = "https://www.international.gc.ca/world-monde/assets/pdfs/international_relations-relations_internationales/sanctions/sema-lmes.pdf"
NOMBRE_SITIO = "canada_sema_search_png"

def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"


async def consultar_canada_sema_search_png(consulta_id: int, nombre: str, max_hits: int = 5):
    """
    Abre el PDF SEMA/LMES, busca `nombre` (Ctrl/Cmd+F), avanza con Enter y toma
    screenshots recortados de la franja central para cada coincidencia (hasta max_hits).
    Guarda todas las imágenes en un ZIP y registra el resultado en la BD.
    """
    navegador = None
    fuente_obj = None

    # Buscar fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo="",
        )
        return

    try:
        # Carpeta de salida: resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        safe = _safe_name(nombre)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archivos_abs = []

        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await navegador.new_context(
                viewport={"width": 1600, "height": 1000},
                locale="en-CA"
            )
            page = await context.new_page()

            # 1) Abrir PDF
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            # Espera larga para renderizar el visor
            await page.wait_for_timeout(35000)

            # 2) Abrir búsqueda
            try:
                await page.keyboard.press("Control+f")
            except Exception:
                try:
                    await page.keyboard.press("Meta+f")
                except Exception:
                    pass

            # 3) Escribir término y saltar al primer resultado
            try:
                await page.keyboard.press("Control+a")
            except Exception:
                pass
            await page.keyboard.type(nombre or "")
            await page.wait_for_timeout(600)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(800)

            # 3.1 Zoom para legibilidad
            for _ in range(3):  # ~150–200%
                try:
                    await page.keyboard.press("Control+Plus")
                except Exception:
                    try:
                        await page.keyboard.press("Meta+Plus")
                    except Exception:
                        break
                await page.wait_for_timeout(200)

            # 4) Captura franja centrada (resaltado suele quedar visible ahí)
            vp = page.viewport_size or {"width": 1600, "height": 1000}
            vw, vh = vp["width"], vp["height"]
            clip_height = 240
            clip_width = int(vw * 0.92)
            clip_x = int((vw - clip_width) / 2)
            clip_y = int((vh - clip_height) / 2)

            hits_tomados = 0
            while hits_tomados < max_hits:
                await page.wait_for_timeout(700)
                try:
                    await page.evaluate("window.scrollBy(0, -60)")
                except Exception:
                    pass

                out_png_name = f"{NOMBRE_SITIO}_{safe}_{ts}_{hits_tomados+1}.png"
                out_png_abs = os.path.join(absolute_folder, out_png_name)

                await page.screenshot(
                    path=out_png_abs,
                    full_page=False,
                    clip={"x": clip_x, "y": clip_y, "width": clip_width, "height": clip_height},
                )
                archivos_abs.append(out_png_abs)
                hits_tomados += 1

                # Siguiente coincidencia
                await page.keyboard.press("Enter")

            await navegador.close()
            navegador = None

        # Empaquetar en ZIP (o registrar OK sin archivo si no hubo hits)
        if archivos_abs:
            zip_name = f"{NOMBRE_SITIO}_{safe}_{ts}.zip"
            abs_zip_path = os.path.join(absolute_folder, zip_name)
            rel_zip_path = os.path.join(relative_folder, zip_name)

            with zipfile.ZipFile(abs_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for fpath in archivos_abs:
                    zf.write(fpath, arcname=os.path.basename(fpath))

            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Validada",
                mensaje=f"{len(archivos_abs)} capturas (recortadas) para '{nombre}'.",
                archivo=rel_zip_path,
            )
        else:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Validada",
                mensaje=f"Sin coincidencias visibles para '{nombre}'.",
                archivo="",
            )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin validar",
                mensaje=str(e),
                archivo="",
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
