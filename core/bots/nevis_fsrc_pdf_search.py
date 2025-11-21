# core/bots/nevis_fsrc_pdf_search.py
import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = (
    "https://www.nevisfsrc.com/themencode-pdf-viewer-sc/"
    "?tnc_pvfw=ZmlsZT1odHRwczovL3d3dy5uZXZpc2ZzcmMuY29tL3dwLWNvbnRlbnQvdXBsb2Fkcy8yMDIyLzA3L0VVLUNvbnNvbGlkYXRlZC1GaW5hbmNpYWwtU2FuY3Rpb25zLUxpc3QtMTMtSnVseS0yMDIyLnBkZiZzZXR0aW5ncz0xMTExMTExMTExMTExMSZsYW5nPWVuLVVT#page=&zoom=auto&pagemode="
)

NOMBRE_SITIO = "nevis_fsrc"

async def consultar_nevis_fsrc_pdf_search(consulta_id: int, cedula, nombre: str, max_hits: int = 7):
    """
    Busca 'nombre' dentro del PDF público del FSRC (Nevis).
    Genera capturas de pantalla por cada match y guarda cada una en la base de datos como Resultado.
    """
    try:
        # Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"{NOMBRE_SITIO}_{cedula}_{ts}"
        archivos = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1600, "height": 950})
            page = await context.new_page()

            # 1) Abrir visor PDF
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # 2) Ctrl+F y escribir término
            try:
                await page.click("body", timeout=3000)
            except Exception:
                pass

            await page.keyboard.press("Control+f")

            try:
                # Usar input del visor PDF.js
                box = page.locator("#findInput, .findInput").first
                await box.wait_for(state="visible", timeout=3000)
                await box.fill("")
                await box.type(nombre or "", delay=30)
                await box.press("Enter")
            except Exception:
                # fallback: escribir directo
                if nombre:
                    await page.keyboard.type(nombre, delay=30)
                    await page.keyboard.press("Enter")

            # 3) Leer contador de resultados si está disponible
            await page.wait_for_timeout(800)
            total = None
            try:
                cnt = page.locator("#findResultsCount, .findResultsCount").first
                if await cnt.count():
                    raw = (await cnt.inner_text() or "").strip()
                    if "of" in raw:
                        try:
                            total = int(raw.split("of")[-1].strip())
                        except Exception:
                            pass
                    else:
                        try:
                            total = int(raw)
                        except Exception:
                            pass
            except Exception:
                pass

            if not total:
                total = 1

            total = min(total, max_hits)

            # 4) Capturar cada hit
            for i in range(1, total + 1):
                await page.wait_for_timeout(600)
                fname = f"{base}_hit_{i}.png"
                abs_path = os.path.join(absolute_folder, fname)
                rel_path = os.path.join(relative_folder, fname)
                await page.screenshot(path=abs_path, full_page=True)
                archivos.append(rel_path)

                # Crear un Resultado por cada captura
                fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Validada",
                    mensaje=f"Match {i} de {total}",
                    archivo=rel_path,
                )

                if i < total:
                    await page.keyboard.press("Enter")

            await browser.close()

    except Exception as e:
        # Guardar error único
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje="Ha ocurrido un problema en la fuente",
            archivo="rel_path",
        )
