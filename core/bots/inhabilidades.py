# consulta/inhabilidades_async.py
import os
from datetime import datetime, date
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout  # ★
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente
from core.resolver.captcha_v2 import resolver_captcha_v2  # async

PAGE_URL = "https://inhabilidades.policia.gov.co:8080/"
SITE_KEY = "6LflZLwUAAAAAP6-I_SuqVa1YDSTqfMyk43peb_M"
NOMBRE_SITIO = "inhabilidades"

def normalizar_fecha(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime("%d-%m-%Y")
    s = str(value).strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%d-%m-%Y")
        except ValueError:
            pass
    return s

async def consultar_inhabilidades(
    consulta_id: int,
    cedula: str,
    tipo_doc: str,
    fecha_exp,          # puede venir como str o date
    empresa: str,
    nit: str,
):
    browser = None
    fuente_obj = None

    # Buscar fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo="",
        )
        return

    try:
        # Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        png_name = f"{NOMBRE_SITIO}_{cedula}_{ts}.png"
        absolute_path = os.path.join(absolute_folder, png_name)
        relative_path = os.path.join(relative_folder, png_name)

        fecha_formateada = normalizar_fecha(fecha_exp)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, slow_mo=250)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                device_scale_factor=1.5,
                locale="es-CO",
            )
            page = await context.new_page()

            # 1) Abrir
            await page.goto(PAGE_URL, timeout=90000, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)  # ↑ un poco más
            except Exception:
                pass

            # ========== GUARD RÁPIDO: si no aparece el form, marcar caída ==========  ★
            try:
                await page.wait_for_selector("#tipo", state="visible", timeout=30000)
            except PlaywrightTimeout as te:
                # Screenshot y registrar como caída/lento
                try:
                    await page.screenshot(path=absolute_path, full_page=True)
                except Exception:
                    pass
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin Validar",
                    mensaje="La página de inhabilidades no respondió (sitio caído o muy lento). Detalle: "
                            f"{str(te).splitlines()[0]}",
                    archivo=relative_path if os.path.exists(absolute_path) else "",
                )
                try:
                    await browser.close()
                except Exception:
                    pass
                return
            # ======================================================================

            # 2) Form
            await page.select_option("#tipo", (tipo_doc or "").strip())
            await page.fill("#nuip", str(cedula))
            await page.fill("#fechaExpNuip", fecha_formateada)
            try:
                await page.keyboard.press("Tab")
                await page.evaluate("document.querySelector('#fechaExpNuip')?.blur()")
            except Exception:
                pass

            # 3) Empresa
            await page.fill("#nombreEmpresa", empresa or "")
            await page.fill("#nitEmpresa", str(nit or ""))

            # 4) Aceptar condiciones
            try:
                cb = page.locator("#cbCondiciones")
                await cb.wait_for(state="visible", timeout=5000)
                try:
                    await cb.check(timeout=1000, force=True)
                except Exception:
                    box = await cb.bounding_box()
                    if box:
                        await page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
            except Exception:
                pass

            # 5) reCAPTCHA v2
            token = await resolver_captcha_v2(PAGE_URL, SITE_KEY)

            # 6) Inyectar token
            await page.evaluate(
                """
                (token) => {
                  let el = document.getElementById("g-recaptcha-response");
                  if (!el) {
                    el = document.createElement("textarea");
                    el.id = "g-recaptcha-response";
                    el.name = "g-recaptcha-response";
                    el.style = "display:none;";
                    document.forms[0]?.appendChild(el);
                  }
                  el.value = token;
                  el.dispatchEvent(new Event('input', {bubbles: true}));
                  el.dispatchEvent(new Event('change', {bubbles: true}));
                }
                """,
                token,
            )

            # 7) Enviar
            await page.click("#btnConsultar")

            # 8) Espera “algo visible” tras enviar: alerta o contenedor de resultados
            objetivos = [
                "div.alert.alert-danger.pb_font-16.py-1",
                "p.text-uppercase.font-weight-bold.py-3",
                "div.resultados, div#resultados",
                "main",
                "div.container",
            ]
            for sel in objetivos:
                try:
                    await page.wait_for_selector(sel, timeout=15000, state="visible")
                    break
                except Exception:
                    continue
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            await page.evaluate("window.scrollTo(0,0)")
            await page.wait_for_timeout(200)

            # ===== Detectar mensaje/score =====
            score_final = 0
            mensaje_final = "Consulta completada."

            # 8.1 Prioridad: alerta "Los datos no coinciden"
            try:
                alerta = page.locator("div.alert.alert-danger.pb_font-16.py-1").first
                if await alerta.count() > 0 and await alerta.is_visible():
                    strong = alerta.locator("strong").first
                    txt = ""
                    if await strong.count() > 0:
                        txt = (await strong.inner_text() or "").strip()
                    if not txt:
                        txt = (await alerta.inner_text() or "").strip()
                    mensaje_final = txt or "Los datos no coinciden"
                    score_final = 0
                else:
                    # 8.2 Estado normal
                    status_el = page.locator("p.text-uppercase.font-weight-bold.py-3").first
                    status_text = None
                    if await status_el.count() > 0 and await status_el.is_visible():
                        status_text = (await status_el.inner_text()).strip()

                    if not status_text:
                        html = (await page.content()) or ""
                        up = html.upper()
                        if "NO REGISTRA INHABILIDAD" in up:
                            status_text = "NO REGISTRA INHABILIDAD"
                        elif "REGISTRA INHABILIDAD" in up:
                            status_text = "REGISTRA INHABILIDAD"

                    if status_text:
                        up = status_text.upper()
                        if "NO REGISTRA INHABILIDAD" in up:
                            mensaje_final = "NO REGISTRA INHABILIDAD"
                            score_final = 0
                        elif "REGISTRA INHABILIDAD" in up:
                            mensaje_final = "REGISTRA INHABILIDAD"
                            score_final = 10
                        else:
                            mensaje_final = status_text
                            score_final = 10
            except Exception:
                pass

            # 9) Screenshot SIEMPRE
            await page.screenshot(path=absolute_path, full_page=True)
            await page.wait_for_timeout(200)

            await browser.close()
            browser = None

        # Registrar OK
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=relative_path,
        )

    except Exception as e:
        # Si algo truena fuera del guard, intenta capturar pantalla y decir que está caído
        try:
            if browser is not None:
                # intentar una pantalla del último page/context (si existe)
                # si no existe, se ignora sin romper
                pass
        except Exception:
            pass
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje=f"La página de inhabilidades no respondió correctamente. Detalle: {e}",
            archivo="",
        )
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass