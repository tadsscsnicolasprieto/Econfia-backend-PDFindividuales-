# core/bots/embajada_alemania_funcionarios.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from core.models import Resultado, Fuente

NOMBRE_SITIO = "embajada_alemania_funcionarios"
URL = "https://alemania.embajada.gov.co/acerca/funcionarios"

# Cloudflare detection
CF_CHALLENGE_SELECTORS = [
    "cf-challenge",
    "cf_clearance",
    "iframe[src*='challenges.cloudflare.com']",
    "body:has(> #challenge-form)",
    "div:has-text('Just a moment')",
]

# Selectores (el sitio es Drupal y a veces duplica el mismo id)
SEL_INPUT_VISIBLE   = "#edit-keys:visible"
SEL_INPUT_FALLBACK  = "main input#edit-keys.form-search:visible, main input[name='keys'].form-search:visible"

RESULT_HINTS = [
    ".view-content", ".region-content", "main", "article", ".block-system-main-block",
]

WAIT_NAV_MS  = 15000
WAIT_POST_MS = 2500

async def consultar_embajada_alemania_funcionarios(
    consulta_id: int,
    nombre: str,
    apellido: str,
):
    browser = None
    score_final = 0
    mensaje_final = "Ejecución incompleta"

    # Lee env vars
    headless_env = os.environ.get("EMBAJADA_ALEMANIA_HEADLESS", "true").lower()
    headless_flag = headless_env not in ["false", "0", "no"]
    slow_mo_env = os.environ.get("EMBAJADA_ALEMANIA_SLOW_MO", "0")
    slow_mo = int(slow_mo_env) if slow_mo_env.isdigit() else 0

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo="",
        )
        return

    try:
        # 2) Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        query = (f"{(nombre or '').strip()} {(apellido or '').strip()}").strip() or "consulta"
        safe_query = re.sub(r"\s+", "_", query)
        png_name = f"{NOMBRE_SITIO}_{safe_query}_{ts}.png"
        abs_png  = os.path.join(absolute_folder, png_name)
        rel_png  = os.path.join(relative_folder, png_name).replace("\\", "/")

        print(f"[embajada_alemania] Inicio consulta_id={consulta_id} nombre={nombre} apellido={apellido}")
        print(f"[embajada_alemania] headless={headless_flag} slow_mo={slow_mo}")

        async with async_playwright() as p:
            # Carpeta de perfil persistente (para Cloudflare)
            user_data = os.path.join(settings.MEDIA_ROOT, "browser_profiles", "alemania_funcionarios")
            os.makedirs(user_data, exist_ok=True)

            print(f"[embajada_alemania] Lanzando navegador (perfil persistente)")
            
            # OPCIÓN 1: Launch_persistent_context (mejor para Cloudflare con headless=False en desktop)
            # OPCIÓN 2: launch + new_context (mejor para headless=True en servidor)
            
            if headless_flag:
                # Modo headless: launch normal sin perfil persistente
                print(f"[embajada_alemania] Modo headless (sin UI)")
                browser = await p.chromium.launch(
                    headless=True,
                    slow_mo=slow_mo,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ]
                )
                context = await browser.new_context(
                    locale="es-CO",
                    viewport={"width": 1400, "height": 900},
                )
                page = await context.new_page()
            else:
                # Modo visual: persistent_context para mejor interacción
                print(f"[embajada_alemania] Modo visual (con UI)")
                browser = await p.chromium.launch_persistent_context(
                    user_data,
                    headless=False,
                    locale="es-CO",
                    slow_mo=slow_mo,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                        "--disable-web-security",
                        "--start-maximized",
                    ],
                )
                page = browser.pages[0] if browser.pages else await browser.new_page()

            # Spoofing anti-automación
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4] });
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', { get: () => ['es-CO', 'es', 'en'] });
            """)

            # User-Agent y headers reales
            await page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            })

            # NAVEGAR a la URL protegida por Cloudflare
            print(f"[embajada_alemania] Navegando a {URL}")
            try:
                await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
            except PWTimeout:
                print(f"[embajada_alemania] Timeout en goto, continuando...")
                pass

            # Detectar Cloudflare Challenge
            print(f"[embajada_alemania] Detectando desafío de Cloudflare")
            cf_detected = False
            for sel in CF_CHALLENGE_SELECTORS:
                try:
                    if await page.locator(sel).count() > 0:
                        print(f"[embajada_alemania] ⚠️ Cloudflare Challenge detectado: {sel}")
                        cf_detected = True
                        break
                except Exception:
                    pass

            # Esperar a que Cloudflare se resuelva
            if cf_detected:
                print(f"[embajada_alemania] Esperando resolución de Cloudflare (hasta 45 segundos)...")
                try:
                    await page.wait_for_load_state("networkidle", timeout=45000)
                except PWTimeout:
                    print(f"[embajada_alemania] Timeout esperando networkidle, continuando...")
                    pass
            else:
                print(f"[embajada_alemania] Sin Cloudflare detectado, esperando carga normal")
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except PWTimeout:
                    pass

            # Extra: Espera por si Cloudflare aún está procesando
            await asyncio.sleep(3)

            # 3) Cerrar cookies/popups
            print(f"[embajada_alemania] Cerrando popups de cookies")
            try:
                for sel in [
                    "button:has-text('Aceptar')",
                    "button:has-text('Accept')",
                    ".eu-cookie-compliance-default-button",
                    "button.close, .close[aria-label='Close']",
                ]:
                    btns = page.locator(sel)
                    for i in range(min(await btns.count(), 3)):
                        b = btns.nth(i)
                        try:
                            if await b.is_visible(timeout=1000):
                                await b.click(timeout=1000)
                        except Exception:
                            pass
            except Exception:
                pass

            # 4) Scroll al contenido principal
            print(f"[embajada_alemania] Preparando página para búsqueda")
            try:
                main = page.locator("main").first
                if await main.count() > 0:
                    el = await main.element_handle()
                    if el:
                        await page.evaluate("(el)=>el.scrollIntoView({behavior:'instant', block:'start'})", el)
                        await asyncio.sleep(0.2)
            except Exception:
                pass

            # 5) Localizar input de búsqueda
            print(f"[embajada_alemania] Buscando campo de búsqueda")
            input_loc = None
            try:
                await page.wait_for_selector("#edit-keys:visible", state="visible", timeout=5000)
                input_loc = page.locator("#edit-keys:visible").first
                print(f"[embajada_alemania] Campo encontrado: #edit-keys:visible")
            except Exception:
                try:
                    await page.wait_for_selector("main input#edit-keys.form-search:visible", state="visible", timeout=5000)
                    input_loc = page.locator("main input#edit-keys.form-search:visible").first
                    print(f"[embajada_alemania] Campo encontrado: main input#edit-keys.form-search:visible")
                except Exception as e:
                    print(f"[embajada_alemania] ERROR: No se encontró campo de búsqueda: {e}")
                    raise ValueError("No se encontró el campo de búsqueda #edit-keys")

            # 6) Realizar búsqueda
            print(f"[embajada_alemania] Ejecutando búsqueda: '{query}'")
            await input_loc.click()
            try:
                await input_loc.fill("")
            except Exception:
                pass
            await input_loc.type(query, delay=20)
            await input_loc.press("Enter")

            # 7) Esperar resultados
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                pass
            await asyncio.sleep(2)

            # 8) Análisis de resultados
            print(f"[embajada_alemania] Analizando resultados")
            nores_sel = "div.content h3:has-text('Su búsqueda no produjo resultados'), h3:has-text('Su búsqueda no produjo resultados')"
            try:
                nores = page.locator(nores_sel).first
                if (await nores.count()) > 0 and (await nores.is_visible()):
                    score_final = 0
                    mensaje_final = "Su búsqueda no produjo resultados"
                    print(f"[embajada_alemania] ❌ Resultado: NO se encontraron coincidencias")
                else:
                    score_final = 10
                    mensaje_final = "Se encontraron hallazgos"
                    print(f"[embajada_alemania] ✅ Resultado: Se encontraron hallazgos")
            except Exception as e:
                print(f"[embajada_alemania] No se pudo detectar mensaje de resultados: {e}")
                # Por defecto asumir hallazgos si la búsqueda se ejecutó
                score_final = 10
                mensaje_final = "Se encontraron hallazgos"

            # 9) Screenshot de página COMPLETA
            print(f"[embajada_alemania] Capturando screenshot")
            try:
                await page.screenshot(path=abs_png, full_page=True)
                print(f"[embajada_alemania] Screenshot guardado: {abs_png}")
            except Exception as e:
                print(f"[embajada_alemania] Error al capturar screenshot: {e}")

            # Cerrar contexto/navegador
            if headless_flag:
                await context.close()
            await browser.close()
            browser = None

        # 10) Registrar resultado
        print(f"[embajada_alemania] Guardando resultado")
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=rel_png,
        )
        print(f"[embajada_alemania] ✅ Ejecución finalizada")

    except Exception as e:
        print(f"[embajada_alemania] ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        # Registrar error
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=str(e)[:500],
                archivo="",
            )
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
