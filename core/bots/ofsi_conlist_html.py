import os
import re
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.html"
NOMBRE_SITIO = "ofsi_consolidated_html"

_ONE_BY_ONE_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0cIDATx\x9cc```\x00"
    b"\x00\x00\x05\x00\x01\x0d\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

def _crear_png_sin_coincidencias_pillow(out_path: str, texto: str) -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (1280, 800), color=(245, 245, 245))
        d = ImageDraw.Draw(img)
        try:
            fnt = ImageFont.truetype("arial.ttf", 28)
        except Exception:
            fnt = ImageFont.load_default()
        try:
            w = d.textlength(texto, font=fnt)
        except Exception:
            w = d.textbbox((0, 0), texto, font=fnt)[2]
        d.text(((1280 - w) / 2, 380), texto, fill=(80, 80, 80), font=fnt)
        img.save(out_path, "PNG")
        return True
    except Exception:
        return False

async def _screenshot_html_mensaje(context, out_path: str, texto: str) -> bool:
    try:
        tmp = await context.new_page()
        await tmp.set_viewport_size({"width": 1280, "height": 800})
        html = f"""
        <html><head><meta charset="utf-8"><style>
        body{{margin:0;background:#f5f5f5;display:flex;align-items:center;justify-content:center;height:100vh;font-family:Arial,Helvetica,sans-serif;color:#444}}
        .box{{font-size:28px}}
        </style></head>
        <body><div class="box">{texto}</div></body></html>
        """
        await tmp.set_content(html, wait_until="load")
        await tmp.screenshot(path=out_path)
        await tmp.close()
        return True
    except Exception:
        return False

def _write_min_png(out_path: str) -> None:
    with open(out_path, "wb") as f:
        f.write(_ONE_BY_ONE_PNG)

def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

async def consultar_ofsi_conlist_html(consulta_id: int, cedula: str, nombre: str):
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = _safe_name(nombre)
    archivos = []

    intentos = 0
    max_intentos = 3
    error_final = None

    while intentos < max_intentos:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                page = await context.new_page()

                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                query = (nombre or "").strip()
                if not query:
                    out_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}.png")
                    await page.screenshot(path=out_abs, full_page=True)
                    archivos.append(os.path.join(relative_folder, os.path.basename(out_abs)))
                    await browser.close()

                    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=0,
                        estado="Validado",
                        mensaje="Sin criterio de búsqueda; captura general.",
                        archivo=archivos[0]
                    )
                    return

                await page.wait_for_timeout(800)
                loc = page.get_by_text(query, exact=False)
                try:
                    total = await loc.count()
                except Exception:
                    total = 0

                try:
                    found_js = await page.evaluate(
                        "q => (document.body?.innerText || '').toLowerCase().includes(q.toLowerCase())",
                        query
                    )
                except Exception:
                    found_js = False

                if total == 0 and not found_js:
                    out_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_no_hits.png")
                    texto = f"Sin coincidencias: {query}"

                    ok = False
                    try:
                        await page.screenshot(path=out_abs, full_page=True)
                        ok = True
                    except Exception:
                        ok = False

                    if not ok:
                        ok = _crear_png_sin_coincidencias_pillow(out_abs, texto)
                    if not ok:
                        ok = await _screenshot_html_mensaje(context, out_abs, texto)
                    if not ok:
                        _write_min_png(out_abs)

                    archivos.append(os.path.join(relative_folder, os.path.basename(out_abs)))
                else:
                    cap_count = max(total, 1)
                    for i in range(cap_count):
                        target = loc.nth(i) if total else page.locator("body").first
                        try:
                            await target.scroll_into_view_if_needed(timeout=4000)
                        except Exception:
                            pass

                        out_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{cedula}_hit{i+1}.png")
                        try:
                            await page.screenshot(path=out_abs, full_page=False)
                            archivos.append(os.path.join(relative_folder, os.path.basename(out_abs)))
                        except Exception:
                            pass

                    # Captura general
                    full_out_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_overview.png")
                    try:
                        await page.screenshot(path=full_out_abs, full_page=True)
                        archivos.append(os.path.join(relative_folder, os.path.basename(full_out_abs)))
                    except Exception:
                        pass

                await browser.close()

            # Guardar resultado si salió bien
            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0 if total == 0 else 10,
                estado="Validado",
                mensaje="Sin coincidencias" if total == 0 else "Se encontraron coincidencias",
                archivo=archivos[0] if archivos else ""
            )
            return

        except Exception as e:
            intentos += 1
            error_final = e
            # Guardar pantallazo de error en cada intento
            out_err = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_error_intento{intentos}.png")
            try:
                if 'page' in locals():
                    await page.screenshot(path=out_err, full_page=True)
                else:
                    _write_min_png(out_err)
                archivos.append(os.path.join(relative_folder, os.path.basename(out_err)))
            except Exception:
                _write_min_png(out_err)

            if intentos < max_intentos:
                continue  # Reintentar

    # Si llegó aquí es porque falló en los 3 intentos → Guardar en BD como error
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje="Ocurrió un problema al obtener la información de la fuente",
            archivo=archivos[-1] if archivos else ""
        )
    except Exception:
        pass
