# core/bots/porvenir_cert_afiliacion.py
import os
import re
import asyncio
import random
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

# URLs oficiales
LANDING_URL = "https://www.porvenir.com.co/certificados-y-extractos"
URL = "https://www.porvenir.com.co/web/certificados-y-extractos/certificado-de-afiliacion"
NOMBRE_SITIO = "porvenir_cert_afiliacion"

# Mapa de tipos de documento
TIPO_DOC_MAP = {"CC": "CC", "CE": "CE", "TI": "TI"}


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


async def _human_delay(min_ms: int = 500, max_ms: int = 1500):
    """Espera variable para simular comportamiento humano"""
    delay = random.uniform(min_ms, max_ms) / 1000
    await asyncio.sleep(delay)


async def consultar_porvenir_cert_afiliacion(consulta_id: int, cedula: str, tipo_doc: str):
    """
    Versión ESTABLE:
      - No intenta resolver CAPTCHA.
      - No intenta descargar el PDF.
      - Solo envía el formulario y captura el mensaje que muestre Porvenir
        (éxito: enviado al correo, no afiliado, error técnico, etc.).
    """

    # Buscar la fuente configurada en BD
    fuente_obj = await sync_to_async(
        lambda: Fuente.objects.filter(nombre=NOMBRE_SITIO).first()
    )()
    if not fuente_obj:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            estado="Sin Validar",
            mensaje=f"No existe la fuente '{NOMBRE_SITIO}'",
            archivo="",
            score=0,
        )
        return

    # Carpetas de salida
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"porvenir_{cedula}_{ts}"

    abs_png = os.path.join(absolute_folder, f"{base}.png")
    rel_png = os.path.join(relative_folder, f"{base}.png").replace("\\", "/")

    browser = context = page = None

    try:
        tipo_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())
        if not tipo_val:
            raise ValueError(f"Tipo de documento no soportado: {tipo_doc!r}")

        print(f"[PORVENIR] Iniciando flujo estable para cedula={cedula}, tipo={tipo_doc}")

        async with async_playwright() as p:
            # Modo "offscreen": ventana real pero fuera de la pantalla
            print("[PORVENIR] Lanzando navegador (offscreen, anti-detección avanzada)...")
            browser = await p.chromium.launch(
                headless=False,  # importante: no headless para evitar bloqueos
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                    "--no-sandbox",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-component-update",
                    "--disable-sync",
                    "--disable-extensions",
                    "--disable-default-apps",
                    "--disable-preconnect",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-size=1400,900",
                    "--window-position=-2000,0",  # mueve la ventana fuera de la pantalla
                ],
            )

            context = await browser.new_context(
                viewport={"width": 1400, "height": 900},
                locale="es-CO",
                accept_downloads=False,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36 "
                    "Edg/122.0.0.0"  # Edge para variar
                ),
                extra_http_headers={
                    "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
            )

            # Script de anti-detección AVANZADO
            await context.add_init_script(
                """
                // 1. Remover indicadores de automatización
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
                
                // 2. Chrome runtime (anti-headless)
                window.chrome = {
                    runtime: {
                        id: 'aabbccdd',
                        onInstalled: { addListener: () => {} },
                        onMessage: { addListener: () => {} },
                    }
                };
                
                // 3. Plugins (simulate real plugins)
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [
                        { name: 'Chrome PDF Plugin', description: 'Portable Document Format' },
                        { name: 'Chrome PDF Viewer' },
                        { name: 'Native Client Executable', description: '' },
                    ],
                });
                
                // 4. Languages y lenguaje predeterminado
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['es-CO', 'es', 'en-US', 'en'],
                });
                
                Object.defineProperty(navigator, 'language', {
                    get: () => 'es-CO',
                });
                
                // 5. Permissions query (anti-detection)
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : originalQuery(parameters)
                );
                
                // 6. Platform, hardwareConcurrency, deviceMemory
                Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
                Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
                Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
                
                // 7. Canvas fingerprinting (simular normalidad)
                const canvas = document.createElement('canvas');
                const ctx = canvas.getContext('2d');
                ctx.textBaseline = 'top';
                ctx.font = '14px Arial';
                ctx.fillStyle = '#f60';
                ctx.fillRect(125, 1, 62, 20);
                ctx.fillStyle = '#069';
                ctx.fillText('Browser Fingerprint', 2, 15);
                
                // 8. Quitar __proto__ y propiedades sospechosas
                Object.defineProperty(navigator, '__proto__', {
                    get: function() { return navigator; }
                });
                
                // 9. Screen resolution (simular monitor real)
                Object.defineProperty(screen, 'width', { get: () => 1920 });
                Object.defineProperty(screen, 'height', { get: () => 1080 });
                Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
                Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
                
                // 10. Timezone (Colombia)
                Object.defineProperty(Intl.DateTimeFormat.prototype, 'resolvedOptions', {
                    value: function() { return { timeZone: 'America/Bogota' }; }
                });
                
                // 11. Quitar console.log y funciones de debug
                if (typeof __nightmare !== 'undefined' || typeof _phantom !== 'undefined') {
                    throw new Error('PhantomJS detected!');
                }
                
                // 12. Random seed para canvas
                Math.random = (function() {
                    const x = Math.sin(12345) * 10000;
                    return function() {
                        return x - Math.floor(x);
                    };
                })();
                """
            )

            page = await context.new_page()
            
            # Agregar delay aleatorio antes de navegar (simular usuario pensando)
            await _human_delay(1000, 3000)
            
            print("[PORVENIR] Navegador iniciado (anti-detección aplicada)")

            # PASO 1 – Landing
            print("[PORVENIR] PASO 1: Navegando al landing...")
            await page.goto(LANDING_URL, wait_until="domcontentloaded", timeout=90000)
            await _human_delay(1500, 2500)  # Esperar como usuario real

            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            # Cerrar cookies si aparecen
            try:
                cookie = page.locator(
                    "button:has-text('Aceptar'), button:has-text('Acepto'), [aria-label*='acept']"
                ).first
                if await cookie.count() > 0:
                    await _human_delay(800, 1200)  # Delay antes de cerrar
                    await cookie.click(timeout=3000)
                    await _human_delay(500, 1000)
                    print("[PORVENIR] Banner de cookies cerrado")
            except Exception:
                pass

            # PASO 2 – Ir al formulario de certificado
            print("[PORVENIR] PASO 2: Navegando al formulario de certificado...")
            try:
                link = page.locator("a:has-text('Descárgalo aquí')").first
                if await link.count() == 0:
                    link = page.locator("a[href*='certificado-de-afiliacion']").first

                await _human_delay(800, 1500)  # Simular lectura
                async with page.expect_navigation(timeout=15000):
                    await link.click()
                await _human_delay(1500, 2500)  # Esperar carga
                print("[PORVENIR] Navegó al formulario mediante link")
            except Exception as e:
                print(f"[PORVENIR] No encontró link, navegando directo: {e}")
                await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
                await _human_delay(1500, 2500)

            # Asegurar URL de destino
            try:
                await page.wait_for_url(
                    lambda u: "certificado-de-afiliacion" in u, timeout=8000
                )
            except Exception:
                await page.goto(URL, wait_until="domcontentloaded", timeout=90000)

            # PASO 3 – Llenar formulario
            print("[PORVENIR] PASO 3: Llenando formulario...")
            await page.wait_for_selector('select[id$="_documento"]', timeout=20000)
            
            # Delay antes de interactuar
            await _human_delay(800, 1200)
            
            # Seleccionar tipo de documento
            await page.select_option('select[id$="_documento"]', tipo_val)
            await _human_delay(600, 1000)
            
            # Llenar cédula carácter por carácter
            input_field = page.locator('input[id$="_numeroIdentificacion"]:not([type="hidden"])')
            await input_field.click()
            await _human_delay(300, 600)
            
            for char in str(cedula):
                await input_field.type(char, delay=random.randint(50, 150))
            
            await _human_delay(800, 1200)
            print("[PORVENIR] Formulario completado")

            # PASO 4 – Enviar formulario (SIN captcha y SIN descarga)
            print("[PORVENIR] PASO 4: Enviando formulario...")
            await _human_delay(1000, 1500)
            
            try:
                await page.click("#submitDescargarCertificado", timeout=5000)
            except Exception:
                print("[PORVENIR] Click normal falló, usando JS...")
                await page.evaluate(
                    "document.querySelector('#submitDescargarCertificado')?.click();"
                )

            await _human_delay(2000, 3500)

            await page.wait_for_timeout(2500)

            # PASO 5 – Analizar estado de la pantalla
            print("[PORVENIR] PASO 5: Detectando mensaje en pantalla...")

            estado = None
            mensaje = None

            # 5.1 – Mensaje de éxito / enviado
            try:
                exito = page.locator(
                    "p:has-text('descargado con éxito'), "
                    "p:has-text('se ha descargado con éxito'), "
                    "p:has-text('enviado'), "
                    "p:has-text('se ha enviado'), "
                    "h2:has-text('Tu certificado se ha descargado con éxito')"
                ).first
                await exito.wait_for(state="visible", timeout=8000)
                mensaje = _normalize_ws(await exito.inner_text())
                estado = "Validada"
                print(f"[PORVENIR] Mensaje de ÉXITO detectado: {mensaje}")
            except Exception:
                pass

            # 5.2 – Mensaje NO afiliado
            if not estado:
                try:
                    na = page.locator("p.p-status").first
                    await na.wait_for(state="visible", timeout=4000)
                    mensaje = _normalize_ws(await na.inner_text())
                    estado = "Validada"
                    print(f"[PORVENIR] Mensaje de NO AFILIADO: {mensaje}")
                except Exception:
                    pass

            # 5.3 – Mensajes de error técnico / mantenimiento
            if not estado:
                try:
                    err = page.locator(
                        "p:has-text('problema técnico'), "
                        "p:has-text('Por favor ingresa más tarde'), "
                        'p:has-text("Nuestro servicio está experimentando un problema técnico")'
                    ).first
                    await err.wait_for(state="visible", timeout=6000)
                    mensaje = _normalize_ws(await err.inner_text())
                    estado = "Sin Validar"
                    print(f"[PORVENIR] Mensaje de ERROR/MANTENIMIENTO: {mensaje}")
                except Exception:
                    pass

            # 5.4 – Fallback si no se detectó nada
            if not estado:
                estado = "Sin Validar"
                mensaje = "No se pudo determinar el estado. Revise la evidencia."
                print("[PORVENIR] No se detectó ningún mensaje claro.")

            # PASO 6 – Captura final
            print("[PORVENIR] Capturando pantalla final...")
            await page.screenshot(path=abs_png, full_page=True)

            # Guardar en BD
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                estado=estado,
                mensaje=mensaje,
                archivo=rel_png,
                score=1 if estado == "Validada" else 0,
            )

    except Exception as e:
        import traceback

        print("[PORVENIR] EXCEPTION:", str(e))
        print(traceback.format_exc())

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            estado="Sin Validar",
            mensaje=str(e),
            archivo="",
            score=0,
        )

    finally:
        try:
            if context:
                await context.close()
        except Exception:
            pass

        try:
            if browser:
                await browser.close()
        except Exception:
            pass

        print("[PORVENIR] Bot finalizado (flujo estable sin captcha/descarga).")
