# core/bots/eris.py
import os
import asyncio
from pathlib import Path
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from playwright.async_api import async_playwright
from asgiref.sync import sync_to_async

from core.resolver.captcha_v2 import resolver_captcha_v2
from core.models import Consulta, Resultado, Fuente

url = "https://eris.contaduria.gov.co/BDME/"
nombre_sitio = "eris"

STORAGE_DIR = Path(getattr(settings, "BASE_DIR", ".")) / "storage_state"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_FILE = STORAGE_DIR / "estado_sesion_eris.json"

TIPO_DOC_MAP = {
    'CC': '28979',
    'NIT': '28980',
    'TI': '28981',
    'CE': '28982',
    'PAS': '28983',
    'SE': '28984',
    'RC': '69865',
    'PEP': '263418'
}

async def consultar_eris(consulta_id: int, cedula: str, tipo_doc: str):
    async def _get_fuente():
        return await sync_to_async(lambda: Fuente.objects.filter(nombre=nombre_sitio).first())()

    async def _crear_resultado_ok(relative_path: str, mensaje: str, score: int):
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=await _get_fuente(),
            estado="Validada",
            mensaje=mensaje,
            archivo=relative_path,
            score=score,
        )

    async def _crear_resultado_error(mensaje: str):
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=await _get_fuente(),
            estado="Sin Validar",
            mensaje=mensaje,
            archivo="",
            score=None
        )

    try:
        # Verificamos que la consulta exista
        await sync_to_async(Consulta.objects.get)(id=consulta_id)
        tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())

        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)

            context_kwargs = {}
            if STORAGE_FILE.exists():
                context_kwargs["storage_state"] = str(STORAGE_FILE)
            context = await navegador.new_context(**context_kwargs)

            pagina = await context.new_page()
            await pagina.goto(url)

            # 🔄 Verificar enlace con reintentos
            max_retries = 10
            for intento in range(max_retries):
                try:
                    await pagina.wait_for_selector(
                        "//a[contains(text(), 'Consultas al Boletín de Deudores Morosos del Estado')]",
                        timeout=3000
                    )
                    break
                except Exception:
                    print(f"⚠️ Intento {intento+1}: enlace no encontrado, recargando...")
                    await pagina.reload()
                    await asyncio.sleep(2)
            else:
                raise Exception("No se encontró el enlace después de varios intentos.")

            # Clic en el enlace
            await pagina.locator(
                "//a[contains(text(), 'Consultas al Boletín de Deudores Morosos del Estado')]"
            ).click()

            # Seleccionar tipo doc y llenar número
            await pagina.wait_for_selector("select.gwt-ListBox")
            if tipo_doc_val:
                await pagina.locator("select.gwt-ListBox").nth(0).select_option(tipo_doc_val)
            await pagina.locator("input.gwt-TextBox").nth(0).fill(str(cedula))

            # Resolver captcha
            iframe = await pagina.wait_for_selector("iframe[title='reCAPTCHA']")
            src = await iframe.get_attribute("src")
            query = parse_qs(urlparse(src or "").query)
            sitekey = (query.get("k") or [None])[0]
            token = await resolver_captcha_v2(url, sitekey)

            # Inyectar token
            await pagina.evaluate(
                """(token) => {
                    let ta = document.getElementById('g-recaptcha-response');
                    if (!ta) {
                        ta = document.createElement('textarea');
                        ta.id = 'g-recaptcha-response';
                        ta.name = 'g-recaptcha-response';
                        ta.style.display = 'none';
                        document.body.appendChild(ta);
                    }
                    ta.value = token;
                }""",
                token
            )

            # Enviar formulario
            await pagina.keyboard.press("Tab")
            await pagina.locator("button.gwt-Button").click()

            # Carpeta resultados
            relative_folder = os.path.join('resultados', str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
            absolute_path = os.path.join(absolute_folder, screenshot_name)
            relative_path = os.path.join(relative_folder, screenshot_name).replace("\\", "/")

            div = await pagina.wait_for_selector('div[id="panelPrincipal"]')
            await asyncio.sleep(1)
            await div.screenshot(path=absolute_path)

            # 👇 Extraer mensaje del certificado
            certificado = await pagina.wait_for_selector("div.col-md-12.certificado-content", timeout=5000)
            mensaje = await certificado.inner_text()

            if "NO está incluido en el BDME" in mensaje:
                score = 0
                mensaje="El documento de identificación CEDULA DE CIUDADANÍA  número 1000381826 NO está incluido en el BDME No. 43 al 31/05/2025 que publica la CONTADURÍA GENERAL DE LA NACIÓN, de acuerdo con lo establecido en el artículo 2° de la Ley 901 del 2004."
            elif "está incluido en el BDME" in mensaje:
                score = 10
                mensaje = await certificado.inner_text()
            else:
                score = None

            # Guardar estado de sesión
            try:
                await context.storage_state(path=str(STORAGE_FILE))
            except Exception:
                pass

            await navegador.close()

        # ✅ Guardamos como validada solo si todo salió bien
        await _crear_resultado_ok(relative_path, mensaje, score)

    except Exception as e:
        # ❌ Cualquier excepción lo marca como Sin Validar
        await _crear_resultado_error(str(e))
