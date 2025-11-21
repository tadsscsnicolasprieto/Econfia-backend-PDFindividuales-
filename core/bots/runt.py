import os
import re
import base64
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from core.models import Resultado, Fuente
from core.resolver.captcha_img import resolver_captcha_imagen

NOMBRE_SITIO = "runt"
URL = "https://portalpublico.runt.gov.co/#/consulta-ciudadano-documento/consulta/consulta-ciudadano-documento"
MAX_INTENTOS = 10

SEL_MAT_SELECT_TRIGGER = "mat-select, .mat-select-trigger"
SEL_MAT_OPTION_TEXT    = ".mat-option .mat-option-text"
SEL_DOC_INPUT          = 'input[formcontrolname="documento"], input#mat-input-0'
SEL_CAPTCHA_IMG        = ".divCaptcha img[src^=\'data:image/png;base64\']"
SEL_CAPTCHA_INPUT      = 'input[formcontrolname="captcha"], input#mat-input-1'
SEL_SUBMIT_BTN         = "button:has-text('Consultar'), button:has-text('Buscar'), button[type='submit']"

SEL_RESULT_HINTS = [
    ".mat-card", ".mat-table", ".resultado", ".content", "app-consulta-ciudadano-documento",
    "table", ".alert", ".mat-expansion-panel", ".resultado-consulta", ".swal2-popup"
]

DOC_TYPE_MAP = {
    "CC": "Cédula Ciudadanía",
    "CE": "Cédula de Extranjería",
    "TI": "Tarjeta de Identidad",
    "PA": "Pasaporte",
    "CD": "Carnet Diplomático",
    "RC": "Registro Civil",
    "PPT": "Permiso por Protección Temporal",
}

async def _guardar_resultado(consulta_id, fuente_obj, estado, mensaje, rel_path):
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=0,
        estado=estado,
        mensaje=mensaje,
        archivo=rel_path,
    )

async def consultar_runt(consulta_id: int, tipo_doc: str, numero: str):
    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_num = re.sub(r"\s+", "_", (numero or "").strip()) or "doc"
    png_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"
    abs_png = os.path.join(absolute_folder, png_name)
    rel_png = os.path.join(relative_folder, png_name)

    intentos = 0
    while intentos < MAX_INTENTOS:
        intentos += 1
        browser = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
                ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="es-CO")
                page = await ctx.new_page()
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                await page.wait_for_timeout(2500)

                # Seleccionar tipo de documento
                sel = page.locator(SEL_MAT_SELECT_TRIGGER).first
                await sel.click()
                await page.wait_for_timeout(800)
                opcion_texto = DOC_TYPE_MAP.get((tipo_doc or "").strip().upper(), "Cédula Ciudadanía")
                await page.locator(f"{SEL_MAT_OPTION_TEXT}:text-is('{opcion_texto}')").first.click(timeout=10000)

                # Número documento
                await page.locator(SEL_DOC_INPUT).first.type(numero, delay=20)

                # Captcha
                img_el = page.locator(SEL_CAPTCHA_IMG).first
                b64src = await img_el.get_attribute("src")
                b64_clean = b64src.split(",", 1)[1] if "," in b64src else b64src
                captcha_file = os.path.join(absolute_folder, f"captcha_{safe_num}_{ts}.png")
                with open(captcha_file, "wb") as f:
                    f.write(base64.b64decode(b64_clean))
                texto_captcha = await resolver_captcha_imagen(captcha_file)
                if not texto_captcha:
                    raise RuntimeError("No fue posible resolver el captcha")
                await page.locator(SEL_CAPTCHA_INPUT).first.type(texto_captcha.strip(), delay=20)

                # Enviar formulario
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    await page.locator(SEL_SUBMIT_BTN).first.click()

                await asyncio.sleep(3)  # esperar renderizado

                # Screenshot
                await page.screenshot(path=abs_png, full_page=False)

                # Verificar mensaje de persona no activa
                swal2_text = page.locator(".swal2-popup .swal2-html-container").all_text_contents()
                mensaje = "La persona está activa"
                if any("No se ha encontrado la persona en estado ACTIVA o SIN REGISTRO" in t for t in swal2_text):
                    mensaje = "La persona no se encuentra activa"

                await _guardar_resultado(consulta_id, fuente_obj, "Validado", mensaje, rel_png)

                await ctx.close()
                await browser.close()
                return

        except Exception as e:
            if browser:
                try:
                    await page.screenshot(path=abs_png)
                    await browser.close()
                except:
                    pass
            if intentos >= MAX_INTENTOS:
                await _guardar_resultado(consulta_id, fuente_obj, "Sin validar", "Ocurrió un problema al obtener la información de la fuente", rel_png)
