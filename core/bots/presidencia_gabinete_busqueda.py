import os
import re
import asyncio
import unicodedata
from datetime import datetime
from urllib.parse import quote, urlencode

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from core.models import Resultado, Fuente

NOMBRE_SITIO = "presidencia_gabinete_busqueda"
BASE_URL = "https://www.presidencia.gov.co/prensa/gabinete"

WAIT_NAV = 15000
WAIT_POST = 2500
MAX_INTENTOS = 3


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


async def consultar_presidencia_gabinete_busqueda(
    consulta_id: int,
    nombre: str,
    apellido: str,
):
    # Lee variables de entorno
    headless_env = os.environ.get("PRESIDENCIA_GABINETE_HEADLESS", "true").lower()
    headless_flag = headless_env not in ["false", "0", "no"]
    slow_mo_env = os.environ.get("PRESIDENCIA_GABINETE_SLOW_MO", "0")
    slow_mo = int(slow_mo_env) if slow_mo_env.isdigit() else 0
    disable_screenshot = os.environ.get("DISABLE_SCREENSHOT_FULLPAGE", "false").lower() in ["true", "1", "yes"]

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

    # Carpeta
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    query = (f"{(nombre or '').strip()} {(apellido or '').strip()}").strip()
    if not query:
        query = "consulta"

    q_norm = _norm(query)
    safe_query = re.sub(r"\s+", "_", query)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    png_name = f"{NOMBRE_SITIO}_{safe_query}_{ts}.png"
    abs_png = os.path.join(absolute_folder, png_name)
    rel_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    # La URL es la página de Gabinete (sin parámetro de búsqueda)
    # La búsqueda se hará en el formulario dentro de la página
    url = BASE_URL

    print(f"[presidencia_gabinete] Inicio consulta_id={consulta_id} nombre={nombre} apellido={apellido}")
    print(f"[presidencia_gabinete] URL base: {url}")
    print(f"[presidencia_gabinete] Búsqueda: {query}")
    print(f"[presidencia_gabinete] headless={headless_flag} slow_mo={slow_mo}")

    ultimo_error = None

    for intento in range(1, MAX_INTENTOS + 1):
        browser = None
        ctx = None

        try:
            print(f"[presidencia_gabinete] Intento {intento}/{MAX_INTENTOS}")
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=headless_flag,
                    slow_mo=slow_mo,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                        "--disable-web-security",
                        "--start-maximized"
                    ]
                )
                ctx = await browser.new_context(
                    viewport={"width": 1400, "height": 900},
                    locale="es-CO",
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
                )
                page = await ctx.new_page()

                # Spoofing anti-automación
                await page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4] });
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'languages', { get: () => ['es-CO', 'es', 'en'] });
                """)

                # Headers para eludir detección
                await page.set_extra_http_headers({
                    "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://www.google.com/",
                    "Cache-Control": "max-age=0",
                })

                # Navegar
                print(f"[presidencia_gabinete] Navegando a {url}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=120000)
                except PWTimeout:
                    print(f"[presidencia_gabinete] Timeout en goto, continuando...")
                    pass

                # Verificar si hay CAPTCHA/Validación
                print(f"[presidencia_gabinete] Verificando si hay CAPTCHA o bloqueo")
                captcha_selectors = [
                    "#Validacion",
                    ".g-recaptcha",
                    "iframe[src*='recaptcha']",
                    "iframe[src*='challenge']",
                    "div:has-text('Validacion')",
                    "div:has-text('CAPTCHA')",
                ]
                
                has_captcha = False
                for sel in captcha_selectors:
                    try:
                        if await page.locator(sel).count() > 0:
                            print(f"[presidencia_gabinete] ⚠️ CAPTCHA/Validación detectado: {sel}")
                            has_captcha = True
                            break
                    except Exception:
                        pass
                
                if has_captcha:
                    print(f"[presidencia_gabinete] ⚠️ CAPTCHA detectado - intentando bypass")
                    # Si hay CAPTCHA, capturar screenshot para diagnóstico
                    diag_png = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_captcha_{ts}.png")
                    await page.screenshot(path=diag_png, full_page=True)
                    print(f"[presidencia_gabinete] Screenshot CAPTCHA guardado: {diag_png}")
                    
                    # Esperar a que el usuario resuelva manualmente (solo en modo headless=false)
                    if not headless_flag:
                        print(f"[presidencia_gabinete] Esperando resolución manual del CAPTCHA (60 segundos)...")
                        try:
                            # Esperar a que desaparezca el CAPTCHA
                            await page.wait_for_selector(
                                "#Validacion, .g-recaptcha, iframe[src*='recaptcha']",
                                state="hidden",
                                timeout=60000
                            )
                            print(f"[presidencia_gabinete] CAPTCHA resuelto")
                            await asyncio.sleep(2)
                        except PWTimeout:
                            print(f"[presidencia_gabinete] Timeout esperando resolución de CAPTCHA")
                            raise Exception("CAPTCHA no fue resuelto en tiempo")
                    else:
                        # En headless no se puede resolver, saltar
                        print(f"[presidencia_gabinete] ❌ CAPTCHA en modo headless - no se puede resolver")
                        raise Exception("CAPTCHA requerido (no se puede resolver en modo headless)")

                # Esperar AJAX
                await asyncio.sleep(2)

                # Esperar contenedores
                print(f"[presidencia_gabinete] Esperando contenedores de resultados")
                try:
                    await page.wait_for_selector(
                        "#NoResult, .ms-srch-item, .ms-srch-group-content",
                        timeout=10000
                    )
                except PWTimeout:
                    print(f"[presidencia_gabinete] Timeout esperando selectores, continuando...")
                    pass

                # BÚSQUEDA EN EL FORMULARIO
                print(f"[presidencia_gabinete] Buscando campo de búsqueda en la página")
                search_input = None
                
                # Intentar múltiples selectores para encontrar el campo de búsqueda
                search_selectors = [
                    "input[placeholder*='Búsqueda']",
                    "input[placeholder*='búsqueda']",
                    "input[type='search']",
                    "input[placeholder*='Busqueda']",
                    ".search-input",
                    "input.form-control",
                ]
                
                for sel in search_selectors:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=3000)
                        search_input = page.locator(sel).first
                        print(f"[presidencia_gabinete] ✅ Campo de búsqueda encontrado: {sel}")
                        break
                    except Exception:
                        print(f"[presidencia_gabinete] Selector no encontrado: {sel}")
                        continue
                
                if not search_input:
                    print(f"[presidencia_gabinete] ❌ No se encontró campo de búsqueda")
                    # Capturar screenshot para diagnóstico
                    diag_png = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_no_search_{ts}.png")
                    await page.screenshot(path=diag_png, full_page=True)
                    raise Exception("No se encontró campo de búsqueda en la página")
                
                # Escribir en el campo
                print(f"[presidencia_gabinete] Escribiendo búsqueda: {query}")
                await search_input.click()
                await search_input.fill(query)
                await asyncio.sleep(0.5)
                
                # Buscar el botón de búsqueda (lupa o similar)
                search_button = None
                button_selectors = [
                    "button[aria-label*='Buscar']",
                    "button[aria-label*='buscar']",
                    "button:has-text('Buscar')",
                    "input[type='submit']",
                    "button[type='submit']",
                    ".search-button",
                ]
                
                for sel in button_selectors:
                    try:
                        search_button = page.locator(sel).first
                        if await search_button.count() > 0:
                            print(f"[presidencia_gabinete] ✅ Botón de búsqueda encontrado: {sel}")
                            break
                    except Exception:
                        continue
                
                if search_button and await search_button.count() > 0:
                    print(f"[presidencia_gabinete] Clickeando botón de búsqueda")
                    await search_button.click()
                else:
                    print(f"[presidencia_gabinete] Presionando Enter para buscar")
                    await search_input.press("Enter")
                
                # Esperar resultados
                await asyncio.sleep(3)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except PWTimeout:
                    pass

                # Caso: No resultados
                print(f"[presidencia_gabinete] Verificando si hay resultados")
                try:
                    noresult_visible = await page.locator("#NoResult").is_visible(timeout=2000)
                    if noresult_visible:
                        txt = await page.locator("#NoResult").inner_text()
                        mensaje = txt.strip() if txt else "No se encontraron resultados"
                        print(f"[presidencia_gabinete] Resultado: {mensaje}")

                        # Screenshot
                        full_page = not disable_screenshot
                        await page.screenshot(path=abs_png, full_page=full_page)
                        print(f"[presidencia_gabinete] Screenshot guardado")

                        await sync_to_async(Resultado.objects.create)(
                            consulta_id=consulta_id,
                            fuente=fuente_obj,
                            score=1,
                            estado="Validado",
                            mensaje=mensaje,
                            archivo=rel_png,
                        )

                        await ctx.close()
                        await browser.close()
                        print(f"[presidencia_gabinete] ✅ Ejecución finalizada (sin resultados)")
                        return
                except Exception as e:
                    print(f"[presidencia_gabinete] No se detectó #NoResult: {e}")
                    pass

                # Obtener items
                print(f"[presidencia_gabinete] Buscando items de resultados")
                items = page.locator(".ms-srch-item")
                total_items = await items.count()
                print(f"[presidencia_gabinete] Total items encontrados: {total_items}")

                exact_matches = 0

                for i in range(total_items):
                    try:
                        node = items.nth(i)
                        title_node = node.locator(".ms-srch-item-title a").first

                        title = ""
                        if await title_node.count() > 0:
                            title = (await title_node.get_attribute("title")) or ""
                            if not title:
                                title = await title_node.inner_text()

                        if _norm(title) == q_norm:
                            exact_matches += 1
                            print(f"[presidencia_gabinete] Coincidencia exacta encontrada: {title}")
                    except Exception as e:
                        print(f"[presidencia_gabinete] Error procesando item {i}: {e}")
                        continue

                # Determinar resultado
                if total_items == 0:
                    score = 1
                    mensaje = "No se encontraron resultados"
                elif exact_matches > 0:
                    score = 5
                    mensaje = f"Se encontraron {exact_matches} coincidencia(s) exacta(s)."
                else:
                    score = 1
                    mensaje = "Resultados encontrados, pero sin coincidencias exactas."

                print(f"[presidencia_gabinete] Score: {score}, Mensaje: {mensaje}")

                # Screenshot
                full_page = not disable_screenshot
                await page.screenshot(path=abs_png, full_page=full_page)
                print(f"[presidencia_gabinete] Screenshot guardado")

                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validado",
                    mensaje=mensaje,
                    archivo=rel_png,
                )

                await ctx.close()
                await browser.close()
                print(f"[presidencia_gabinete] ✅ Ejecución finalizada")
                return

        except Exception as e:
            ultimo_error = str(e)
            print(f"[presidencia_gabinete] ❌ Error en intento {intento}: {e}")
            import traceback
            traceback.print_exc()

            try:
                if ctx:
                    await ctx.close()
                if browser:
                    await browser.close()
            except Exception:
                pass

            await asyncio.sleep(2)

    # Fallo total después de reintentos
    print(f"[presidencia_gabinete] ❌ Fallo total después de {MAX_INTENTOS} intentos")
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=0,
        estado="Sin validar",
        mensaje=f"Ocurrió un problema al obtener la información: {ultimo_error}",
        archivo=rel_png if os.path.exists(abs_png) else "",
    )
