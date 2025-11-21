import os
import re
import asyncio
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright
from PIL import Image
from core.models import Resultado, Fuente

NOMBRE_SITIO = "secop_consulta_aacs"
URL = "https://www.contratos.gov.co/consultas/consultarArchivosAACS.do"
MAX_INTENTOS = 3

TIPO_DOC_MAP = {
    "CC": "4",   # Cédula de Ciudadanía
    "CD": "8",   # Carné Diplomático
    "CE": "5",   # Cédula de Extranjería
    "NE": "3",   # Nit de Extranjería
    "NIT": "1",  # Nit de Persona Jurídica
    "NITN": "2", # Nit de Persona Natural
    "NUIP": "9", # Nuip
    "FID": "11", # Número de Fideicomiso
    "PA": "7",   # Pasaporte
    "SE": "10",  # Sociedades Extranjeras
    "TI": "6",   # Tarjeta de Identidad
}

async def consultar_secop_consulta_aacs(consulta_id: int, nombre: str, tipo_doc: str, numero: str):
    nombre = (nombre or "").strip()
    browser = None
    valor_select = TIPO_DOC_MAP.get(tipo_doc, "")

    # Carpeta resultados
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_q = re.sub(r"\s+", "_", nombre)

    screenshot_before = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe_q}_{timestamp}_before.png")
    screenshot_after = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe_q}_{timestamp}_after.png")
    screenshot_final = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe_q}_{timestamp}.png")
    screenshot_rel = os.path.join(relative_folder, f"{NOMBRE_SITIO}_{safe_q}_{timestamp}.png")

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo="",
        )
        return

    intentos = 0
    while intentos < MAX_INTENTOS:
        intentos += 1
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                ctx = await browser.new_context(viewport={"width":1440,"height":1000}, locale="es-CO")
                page = await ctx.new_page()

                # Navegar
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)

                # Completar formulario
                await page.fill("#nomCont", nombre)
                await page.fill("#text_numDocContratista", numero)
                await page.select_option("#sel_tipo_doc_contratista", value=valor_select)

                # Captura antes de enviar (solo viewport)
                await page.screenshot(path=screenshot_before)

                # Clic en buscar
                await page.locator("#ctl00_ContentPlaceHolder1_imgBuscar").click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(3)

                # Captura después de enviar (solo viewport)
                await page.screenshot(path=screenshot_after)


                # Combinar imágenes (lado a lado)
                img1 = Image.open(screenshot_before)
                img2 = Image.open(screenshot_after)
                total_width = img1.width + img2.width
                max_height = max(img1.height, img2.height)

                new_img = Image.new("RGB", (total_width, max_height), (255, 255, 255))
                new_img.paste(img1, (0, 0))
                new_img.paste(img2, (img1.width, 0))
                new_img.save(screenshot_final)

                # Buscar texto
                div_locator = page.locator(".fixedHeaderTable").first
                texto_div = await div_locator.inner_text()
                if nombre.lower() in texto_div.lower():
                    score = 10
                    mensaje = "Se encontraron hallazgos"
                else:
                    score = 0
                    mensaje = "No se encontraron hallazgos, Importante observar la segunda foto, la primera son datos Default de la pagina"

                await ctx.close()
                await browser.close()
                browser = None

                # Guardar resultado en BD
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validado",
                    mensaje=mensaje,
                    archivo=screenshot_rel,
                )
                return

        except Exception as e:
            if browser:
                try:
                    await page.screenshot(path=screenshot_after, full_page=True)
                    await browser.close()
                except:
                    pass
            if intentos >= MAX_INTENTOS:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin validar",
                    mensaje=e,
                    archivo=screenshot_rel if os.path.exists(screenshot_final) else "",
                )
