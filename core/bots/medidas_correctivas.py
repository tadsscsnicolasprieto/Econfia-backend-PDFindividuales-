import os
import asyncio
import random
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente
from core.resolver.captcha_v2 import resolver_captcha_v2

PAGE_URL = "https://cfiscal.contraloria.gov.co/certificados/certificadopersonanatural.aspx"
PAGE_URL_JUR = "https://cfiscal.contraloria.gov.co/certificados/certificadopersonajuridica.aspx"
NOMBRE_SITIO = "contraloria"

UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

async def _goto_resiliente(page, url, intentos=4, base_sleep=1.5):
    ultimo_err = None
    for i in range(1, intentos + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except Exception as e:
            ultimo_err = e
            await asyncio.sleep(base_sleep * (2 ** (i - 1)) + random.random())
    raise ultimo_err

async def consultar_medidas_correctivas(consulta_id: int, cedula: str, tipo_doc: str, tipo_persona: str):
    browser = None
    context = None
    page = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    try:
        # Rutas
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pdf_name = f"{NOMBRE_SITIO}_{cedula}_{timestamp}.pdf"
        absolute_path = os.path.join(absolute_folder, pdf_name)
        relative_path = os.path.join(relative_folder, pdf_name)

        async with async_playwright() as p:
            # Intenta Chrome estable si existe; si no, Chromium
            try:
                browser = await p.chromium.launch(
                    channel="chrome",
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--lang=es-CO,es",
                        "--disable-http2",
                    ],
                    ignore_default_args=["--enable-automation"],
                )
            except Exception:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--lang=es-CO,es",
                        "--disable-http2",
                    ],
                    ignore_default_args=["--enable-automation"],
                )

            context = await browser.new_context(
                accept_downloads=True,
                user_agent=UA_DESKTOP,
                locale="es-CO",
                timezone_id="America/Bogota",
                ignore_https_errors=True,
                viewport={"width": 1366, "height": 768},
            )

            # Reducir fingerprint
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
                Object.defineProperty(navigator, 'languages', { get: () => ['es-CO','es'] });
            """)

            target = PAGE_URL if (tipo_persona or "").lower() == "natural" else PAGE_URL_JUR

            page = await context.new_page()
            await page.route("**/*", lambda route: (
                route.abort() if route.request.resource_type in {"image","media","font"} else route.continue_()
            ))
            await _goto_resiliente(page, target)
            await page.unroute("**/*")
            await asyncio.sleep(1.0)

            if (tipo_persona or "").lower() == "natural":
                await page.select_option('#ddlTipoDocumento', (tipo_doc or "").strip())

            await page.fill('#txtNumeroDocumento', str(cedula))

            # Captcha
            token = await resolver_captcha_v2(PAGE_URL, "6LcfnjwUAAAAAIyl8ehhox7ZYqLQSVl_w1dmYIle")
            await page.evaluate(
                """token => {
                    let el = document.getElementById('g-recaptcha-response');
                    if (!el) {
                        el = document.createElement('textarea');
                        el.id = 'g-recaptcha-response';
                        el.name = 'g-recaptcha-response';
                        el.style.display = 'none';
                        document.body.appendChild(el);
                    }
                    el.value = token;
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                }""",
                token
            )
            await page.wait_for_timeout(600)

            # Descargar PDF
            async with page.expect_download(timeout=90000) as download_info:
                await page.click('#btnBuscar')
            download = await download_info.value
            await download.save_as(absolute_path)

            await browser.close()
            browser = None

        # ⬇️ AQUÍ EL CAMBIO DE MENSAJE Y SCORE
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Validada",
            mensaje="PDF descargado exitosamente revisar informacion, porque es pdf",
            archivo=relative_path
        )

    except (PWTimeoutError, Exception) as e:
        err = f"Fallo navegación/descarga: {e}"
        absolute_path = locals().get('absolute_path', 'error_screenshot.png')
        relative_path = locals().get('relative_path', '')
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj, score=0,
                estado="Sin Validar", mensaje=err, archivo=relative_path
            )
        finally:
            try:
                if browser is not None:
                    await browser.close()
            except Exception:
                pass

