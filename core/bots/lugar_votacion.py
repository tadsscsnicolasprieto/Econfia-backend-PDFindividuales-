# bots/lugar_votacion.py
import os
import asyncio
from datetime import datetime
from typing import Optional

from playwright.async_api import async_playwright, Page, Response
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente
from core.resolver.captcha_v2 import resolver_captcha_v2  # versión async que devuelve token

URL = "https://wsp.registraduria.gov.co/censo/consultar/"
SITE_KEY = "6LcthjAgAAAAAFIQLxy52074zanHv47cIvmIHglH"
NOMBRE_SITIO = "lugar_votacion"

GOTO_TIMEOUT_MS = 30_000


async def _safe_goto(page: Page, url: str, folder: str, prefix: str, timeout: int = GOTO_TIMEOUT_MS, attempts: int = 3) -> Optional[Response]:
    """Navega con reintentos y guarda HTML si falla la carga."""
    last_exc = None
    delay = 1.0
    for i in range(1, attempts + 1):
        try:
            resp = await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            status = resp.status if resp else None
            # guardar HTML si status no es 200
            if status and status >= 400:
                try:
                    body = await resp.text()
                    with open(os.path.join(folder, f"{prefix}_http_{status}_{i}.html"), "w", encoding="utf-8") as fh:
                        fh.write(body)
                except Exception:
                    pass
                last_exc = Exception(f"HTTP {status} en intento {i}")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return resp
        except Exception as e:
            last_exc = e
            await asyncio.sleep(delay)
            delay *= 2
    raise last_exc


async def consultar_lugar_votacion(consulta_id: int, cedula: str):
    navegador = None
    fuente_obj = None

    # Buscar la fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    # Preparar carpeta de resultados
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_name = f"{NOMBRE_SITIO}_{cedula}_{ts}.png"
    absolute_path = os.path.join(absolute_folder, screenshot_name)
    relative_path = os.path.join(relative_folder, screenshot_name)
    html_debug = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{cedula}_{ts}.html")

    try:
        async with async_playwright() as p:
            # Lanzar navegador con cabeceras y contexto más "humano"
            navegador = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            context = await navegador.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent=ua,
                locale="es-CO",
                timezone_id="America/Bogota",
                ignore_https_errors=True,
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "es-CO,es;q=0.9",
                    "Referer": "https://www.google.com/",
                },
            )

            # pequeño stealth patch
            try:
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'languages', { get: () => ['es-CO','es'] });
                    window.navigator.chrome = { runtime: {} };
                """)
            except Exception:
                pass

            page = await context.new_page()
            # aumentar timeouts por defecto si es necesario
            page.set_default_navigation_timeout(GOTO_TIMEOUT_MS)
            page.set_default_timeout(20_000)

            # 1) Navegar con reintentos
            try:
                await _safe_goto(page, URL, absolute_folder, f"landing_{ts}")
            except Exception as e:
                # guardar HTML si hay algo
                try:
                    html = await page.content()
                    with open(html_debug, "w", encoding="utf-8") as fh:
                        fh.write(html)
                except Exception:
                    pass
                raise Exception(f"No fue posible acceder a la página inicial: {e}")

            # 2) Esperar el formulario / campo de cédula
            try:
                # el selector puede variar; esperar por varios candidatos
                await page.wait_for_selector("#nuip, input[name='nuip'], input[id*='nuip']", timeout=8000)
            except Exception:
                # guardar HTML para diagnóstico
                try:
                    html = await page.content()
                    with open(html_debug, "w", encoding="utf-8") as fh:
                        fh.write(html)
                except Exception:
                    pass
                raise Exception("No se encontró el campo de número de identificación en la página (selector '#nuip').")

            # 3) Rellenar cédula
            try:
                await page.fill("#nuip", str(cedula))
            except Exception:
                # fallback por name/id alternativos
                try:
                    await page.fill("input[name='nuip']", str(cedula))
                except Exception:
                    await page.evaluate(
                        "(v) => { const el = document.querySelector('#nuip') || document.querySelector('input[name=\"nuip\"]'); if (el) { el.value = v; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); } }",
                        str(cedula)
                    )

            # 4) Resolver captcha (token) usando tu resolver async
            token = None
            try:
                token = await resolver_captcha_v2(URL, SITE_KEY)
            except Exception as e:
                # guardar evidencia y continuar (no podemos avanzar sin token)
                try:
                    html = await page.content()
                    with open(html_debug, "w", encoding="utf-8") as fh:
                        fh.write(html)
                except Exception:
                    pass
                raise Exception(f"No se pudo resolver el captcha: {e}")

            if not token:
                raise Exception("El resolver de captcha devolvió token vacío.")

            # 5) Inyectar token en g-recaptcha-response y disparar eventos
            try:
                await page.evaluate(
                    """
                    (token) => {
                        let el = document.getElementById('g-recaptcha-response');
                        if (!el) {
                            el = document.createElement('textarea');
                            el.id = 'g-recaptcha-response';
                            el.name = 'g-recaptcha-response';
                            el.style = 'display:none;';
                            const f = document.querySelector('form') || document.body;
                            f.appendChild(el);
                        }
                        el.value = token;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        // si existe un callback de recaptcha, intentar invocarlo
                        try {
                            if (typeof grecaptcha !== 'undefined' && grecaptcha && grecaptcha.getResponse) {
                                // no podemos forzar grecaptcha internamente, pero dejamos el token en el textarea
                            }
                        } catch(e) {}
                    }
                    """,
                    token,
                )
            except Exception:
                # no crítico, continuamos intentando submit
                pass

            # 6) Intentar seleccionar tipo de elección si es requerido (algunas páginas requieren elegir)
            try:
                # si existe un select para elección, seleccionar la primera opción válida
                if await page.locator("select#eleccion, select[name='eleccion']").count() > 0:
                    await page.select_option("select#eleccion, select[name='eleccion']", index=0)
            except Exception:
                pass

            # 7) Enviar formulario: intentar click en submit y varios fallbacks
            clicked = False
            try:
                # esperar botón submit
                await page.wait_for_selector("input[type='submit'], button[type='submit'], button:has-text('Consultar'), input[value*='Consultar']", timeout=5000)
            except Exception:
                pass

            for attempt in range(3):
                try:
                    # intentar click directo
                    try:
                        await page.click("input[type='submit']", timeout=4000)
                    except Exception:
                        try:
                            await page.click("button[type='submit']", timeout=4000)
                        except Exception:
                            # fallback JS click en el primer submit encontrado
                            await page.evaluate("""
                                () => {
                                    const el = document.querySelector('input[type=submit], button[type=submit], input[value*=\"Consultar\"], button:has-text(\"Consultar\")');
                                    if (el) { el.click(); }
                                }
                            """)
                    clicked = True
                    break
                except Exception:
                    await asyncio.sleep(0.5)

            if not clicked:
                # guardar evidencia y abortar
                try:
                    html = await page.content()
                    with open(html_debug, "w", encoding="utf-8") as fh:
                        fh.write(html)
                except Exception:
                    pass
                await page.screenshot(path=absolute_path, full_page=True)
                raise Exception("No se pudo pulsar el botón de envío en la página.")

            # 8) Esperar resultado: selector #consulta o #success o cambio en DOM
            try:
                await page.wait_for_selector("#consulta, #success, .resultado, table", timeout=10_000)
            except Exception:
                # no apareció en el tiempo, guardar HTML y screenshot para diagnóstico
                try:
                    html = await page.content()
                    with open(html_debug, "w", encoding="utf-8") as fh:
                        fh.write(html)
                except Exception:
                    pass
                await page.screenshot(path=absolute_path, full_page=True)
                raise Exception("No se detectó el contenedor de resultado tras enviar la consulta.")

            # 9) Analizar resultado (igual que tu lógica original)
            score_final = 10
            mensaje_final = ""
            try:
                tabla = page.locator("#consulta")
                filas = tabla.locator("tbody tr")
                if await filas.count() > 0:
                    # Hay resultados -> score 0 y mensaje en texto plano
                    celdas = filas.first.locator("td")
                    pares = []
                    total_celdas = await celdas.count()
                    for i in range(total_celdas):
                        td = celdas.nth(i)
                        etiqueta = (await td.get_attribute("data-th")) or ""
                        valor = (await td.inner_text() or "").strip()
                        etiqueta = " ".join(etiqueta.split())
                        valor = " ".join(valor.split())
                        if etiqueta and valor:
                            pares.append((etiqueta.upper(), valor))

                    orden = ["NUIP", "DEPARTAMENTO", "MUNICIPIO", "PUESTO", "DIRECCIÓN", "MESA"]
                    lineas = []
                    for key in orden:
                        for etq, val in pares:
                            if etq == key:
                                lineas.append(f"{key}: {val}")
                                break
                    # Añadir pares no incluidos
                    ya = set(lineas)
                    for etq, val in pares:
                        linea = f"{etq}: {val}"
                        if linea not in ya:
                            lineas.append(linea)

                    mensaje_final = "\n".join(lineas).strip()
                    score_final = 0  # encontrado => score 0
                else:
                    # Ver si hay mensaje "no se encuentra en el censo"
                    success = page.locator("#success")
                    if await success.count() > 0:
                        raw = (await success.inner_text() or "").strip()
                        mensaje_final = " ".join(raw.split())
                        score_final = 10  # no encontrado => score 10
                    else:
                        mensaje_final = "El documento consultado no se encuentra en el censo para esta elección."
                        score_final = 10
            except Exception:
                mensaje_final = "No fue posible determinar la información del lugar de votación."
                score_final = 10

            # 10) Captura: preferir screenshot del contenedor principal si existe
            try:
                elemento = page.locator("#form, #consulta, form").first
                if await elemento.count() > 0:
                    await elemento.screenshot(path=absolute_path)
                else:
                    await page.screenshot(path=absolute_path, full_page=True)
            except Exception:
                try:
                    await page.screenshot(path=absolute_path, full_page=True)
                except Exception:
                    pass

            # Cerrar navegador/contexto
            try:
                await context.close()
            except Exception:
                pass
            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # Registrar OK (usando el score y mensaje calculados)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=relative_path
        )

    except Exception as e:
        # Guardar HTML/screenshot si no existen para diagnóstico
        try:
            if not os.path.exists(absolute_path):
                # intentar tomar captura rápida con un navegador nuevo si es crítico (opcional)
                pass
        except Exception:
            pass

        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=str(e),
                archivo=""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
