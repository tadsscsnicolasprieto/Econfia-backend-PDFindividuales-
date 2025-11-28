# bots/runt_consulta_retry.py
import os
import re
import base64
import asyncio
import random
import logging
from datetime import datetime
from typing import Optional

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from core.models import Resultado, Fuente
from core.resolver.captcha_img import resolver_captcha_imagen  # tu resolver (async o sync adaptado)

logger = logging.getLogger(__name__)

NOMBRE_SITIO = "runt"
URL = "https://portalpublico.runt.gov.co/#/consulta-ciudadano-documento/consulta/consulta-ciudadano-documento"
MAX_INTENTOS = 8                # intentos completos de todo el flujo
CAPTCHA_REINTENTOS = 4          # reintentos para resolver/actualizar captcha dentro de un intento
HUMAN_DELAY_MIN = 0.8
HUMAN_DELAY_MAX = 2.0

SEL_MAT_SELECT_TRIGGER = "mat-select, .mat-select-trigger"
SEL_MAT_OPTION_TEXT = ".mat-option .mat-option-text"
SEL_DOC_INPUT = 'input[formcontrolname="documento"], input#mat-input-0'
SEL_CAPTCHA_IMG = ".divCaptcha img[src^='data:image/png;base64']"
SEL_CAPTCHA_INPUT = 'input[formcontrolname="captcha"], input#mat-input-1'
SEL_SUBMIT_BTN = "button:has-text('Consultar'), button:has-text('Buscar'), button[type='submit']"
SEL_SWAL2_HTML = ".swal2-popup .swal2-html-container"

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


async def _refresh_captcha_image(page, selector):
    """
    Intenta forzar la recarga del captcha:
    - click en botones comunes de refresh
    - o reescribir el src de la imagen con un timestamp
    """
    try:
        # 1) intentar click en botones de refresco comunes
        refresh_selectors = [
            ".refresh-captcha", ".recaptcha-refresh", "button[title*='Actualizar']",
            "button[aria-label*='Actualizar']", ".btn-refresh-captcha"
        ]
        for s in refresh_selectors:
            try:
                if await page.locator(s).count() > 0:
                    await page.locator(s).first.click()
                    await page.wait_for_timeout(600)
                    return True
            except Exception:
                continue

        # 2) forzar recarga cambiando el src de la imagen (si existe)
        try:
            await page.evaluate(
                """(sel) => {
                    const img = document.querySelector(sel);
                    if (img) {
                        const src = img.getAttribute('src') || '';
                        const base = src.split('?')[0];
                        img.setAttribute('src', base + '?_=' + Date.now());
                    }
                }""",
                selector
            )
            await page.wait_for_timeout(700)
            return True
        except Exception:
            return False
    except Exception:
        return False


async def consultar_runt(consulta_id: int, tipo_doc: str, numero: str):
    """
    Flujo robusto para consultar RUNT con manejo de captchas que fallan en el primer intento.
    """
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        # Si no existe la fuente, registrar y salir
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_num = re.sub(r"\s+", "_", (numero or "").strip()) or "doc"
    png_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"
    abs_png = os.path.join(absolute_folder, png_name)
    rel_png = os.path.join(relative_folder, png_name)
    html_debug = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe_num}_{ts}.html")
    captcha_file = os.path.join(absolute_folder, f"captcha_{safe_num}_{ts}.png")

    intentos = 0
    while intentos < MAX_INTENTOS:
        intentos += 1
        browser = None
        page = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                )
                ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="es-CO")
                # stealth init
                try:
                    await ctx.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                        Object.defineProperty(navigator, 'languages', { get: () => ['es-CO','es'] });
                        window.navigator.chrome = { runtime: {} };
                    """)
                except Exception:
                    pass

                page = await ctx.new_page()
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                await page.wait_for_timeout(1200)

                # seleccionar tipo de documento si aplica
                try:
                    sel_trigger = page.locator(SEL_MAT_SELECT_TRIGGER).first
                    if await sel_trigger.count() > 0:
                        await sel_trigger.click()
                        await page.wait_for_timeout(600)
                        opcion_texto = DOC_TYPE_MAP.get((tipo_doc or "").strip().upper(), "Cédula Ciudadanía")
                        opt = page.locator(f"{SEL_MAT_OPTION_TEXT}:text-is('{opcion_texto}')").first
                        if await opt.count() > 0:
                            await opt.click()
                            await page.wait_for_timeout(300)
                except Exception:
                    pass

                # rellenar número de documento
                try:
                    doc_input = page.locator(SEL_DOC_INPUT).first
                    await doc_input.wait_for(state="visible", timeout=5000)
                    await doc_input.fill("")
                    await doc_input.type(numero, delay=20)
                except Exception:
                    await page.evaluate(
                        "(v) => { const el = document.querySelector('input[formcontrolname=\"documento\"]') || document.querySelector('#mat-input-0'); if (el) { el.value = v; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); } }",
                        numero
                    )

                # esperar captcha
                try:
                    await page.wait_for_selector(SEL_CAPTCHA_IMG, timeout=8000)
                except Exception:
                    html = await page.content()
                    with open(html_debug, "w", encoding="utf-8") as fh:
                        fh.write(html)
                    raise RuntimeError("No se encontró la imagen del captcha en la página.")

                # dentro de este intento, reintentar resolver/actualizar captcha si da inválido
                captcha_ok = False
                texto_captcha = None
                for c_try in range(1, CAPTCHA_REINTENTOS + 1):
                    # obtener imagen y guardarla
                    try:
                        img_el = page.locator(SEL_CAPTCHA_IMG).first
                        b64src = await img_el.get_attribute("src")
                        if not b64src:
                            raise RuntimeError("El atributo src del captcha está vacío.")
                        b64_clean = b64src.split(",", 1)[1] if "," in b64src else b64src
                        with open(captcha_file, "wb") as f:
                            f.write(base64.b64decode(b64_clean))
                    except Exception as e:
                        logger.warning("No se pudo extraer captcha (intento %d): %s", c_try, e)
                        # intentar refrescar la imagen y continuar
                        await _refresh_captcha_image(page, SEL_CAPTCHA_IMG)
                        await page.wait_for_timeout(700)
                        continue

                    # resolver captcha (tu función)
                    try:
                        texto_captcha = await resolver_captcha_imagen(captcha_file)
                        if texto_captcha:
                            texto_captcha = texto_captcha.strip()
                        else:
                            texto_captcha = None
                    except Exception as e:
                        logger.warning("Resolver captcha falló (intento %d): %s", c_try, e)
                        texto_captcha = None

                    if not texto_captcha:
                        # refrescar imagen y reintentar
                        await _refresh_captcha_image(page, SEL_CAPTCHA_IMG)
                        await page.wait_for_timeout(700 + random.random() * 0.6)
                        continue

                    # escribir captcha en input
                    try:
                        captcha_input = page.locator(SEL_CAPTCHA_INPUT).first
                        await captcha_input.wait_for(state="visible", timeout=3000)
                        await captcha_input.fill("")
                        await captcha_input.type(texto_captcha, delay=20)
                    except Exception:
                        await page.evaluate(
                            "(v) => { const el = document.querySelector('input[formcontrolname=\"captcha\"]') || document.querySelector('#mat-input-1'); if (el) { el.value = v; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); } }",
                            texto_captcha
                        )

                    # espera humana aleatoria antes de enviar (importante)
                    await asyncio.sleep(random.uniform(HUMAN_DELAY_MIN, HUMAN_DELAY_MAX))

                    # enviar formulario (click robusto)
                    submitted = False
                    try:
                        await page.wait_for_selector(SEL_SUBMIT_BTN, timeout=5000)
                        btn = page.locator(SEL_SUBMIT_BTN).first
                        await btn.scroll_into_view_if_needed()
                        await btn.click(timeout=5000)
                        submitted = True
                    except Exception:
                        try:
                            await page.keyboard.press("Enter")
                            submitted = True
                        except Exception:
                            submitted = False

                    if not submitted:
                        # no se pudo enviar, refrescar captcha y reintentar
                        await _refresh_captcha_image(page, SEL_CAPTCHA_IMG)
                        await page.wait_for_timeout(700)
                        continue

                    # esperar respuesta corta y comprobar si captcha fue rechazado
                    await page.wait_for_timeout(1200)
                    # comprobar swal2 u otros mensajes que indiquen captcha inválido
                    try:
                        if await page.locator(SEL_SWAL2_HTML).count() > 0:
                            swal_texts = await page.locator(SEL_SWAL2_HTML).all_text_contents()
                            joined = " ".join(t.lower() for t in swal_texts)
                            if "captcha" in joined or "no es válido" in joined or "no válido" in joined or "captcha inválido" in joined:
                                # captcha rechazado: refrescar y reintentar dentro del mismo intento
                                logger.info("Captcha inválido detectado (intento interno %d). Reintentando captcha.", c_try)
                                # cerrar popup si existe (click en aceptar)
                                try:
                                    if await page.locator(".swal2-confirm").count() > 0:
                                        await page.locator(".swal2-confirm").first.click()
                                except Exception:
                                    pass
                                await _refresh_captcha_image(page, SEL_CAPTCHA_IMG)
                                await page.wait_for_timeout(800 + random.random() * 0.6)
                                continue
                    except Exception:
                        # si falla la comprobación, no asumimos éxito; dejamos que el flujo continúe
                        pass

                    # si llegamos aquí, asumimos que el captcha fue aceptado y que hay respuesta
                    captcha_ok = True
                    break  # salir del loop de reintentos de captcha

                # si no logramos captcha_ok dentro de CAPTCHA_REINTENTOS, fallar este intento completo
                if not captcha_ok:
                    raise RuntimeError("No fue posible resolver el captcha tras varios reintentos dentro del intento.")

                # esperar contenedor de resultado (swal2, #consulta, table, etc.)
                try:
                    await page.wait_for_selector(", ".join([".swal2-popup", "#consulta", "#resultado", "table"]), timeout=10000)
                except Exception:
                    # guardar diagnóstico y fallar este intento
                    html = await page.content()
                    with open(html_debug, "w", encoding="utf-8") as fh:
                        fh.write(html)
                    await page.screenshot(path=abs_png, full_page=True)
                    raise RuntimeError("No se detectó contenedor de resultado tras enviar la consulta.")

                # tomar screenshot del área principal
                try:
                    if await page.locator("#consulta").count() > 0:
                        await page.locator("#consulta").first.screenshot(path=abs_png)
                    elif await page.locator("app-consulta-ciudadano-documento").count() > 0:
                        await page.locator("app-consulta-ciudadano-documento").first.screenshot(path=abs_png)
                    else:
                        await page.screenshot(path=abs_png, full_page=True)
                except Exception:
                    try:
                        await page.screenshot(path=abs_png, full_page=True)
                    except Exception:
                        pass

                # analizar resultado
                mensaje = "No fue posible determinar el resultado."
                score = 1
                try:
                    if await page.locator(SEL_SWAL2_HTML).count() > 0:
                        swal2_texts = await page.locator(SEL_SWAL2_HTML).all_text_contents()
                        joined = " ".join(sw.lower() for sw in swal2_texts)
                        if "no se ha encontrado la persona" in joined or "sin registro" in joined or "no se encuentra" in joined:
                            mensaje = "La persona no se encuentra activa o no está registrada."
                            score = 10
                        elif "captcha" in joined or "no es válido" in joined or "no válido" in joined:
                            # si aparece captcha inválido aquí, forzamos reintento completo
                            raise RuntimeError("Captcha inválido detectado en mensaje swal2 tras submit.")
                        else:
                            mensaje = joined.strip()
                            score = 1
                    else:
                        if await page.locator("#consulta tbody tr").count() > 0:
                            filas = page.locator("#consulta tbody tr")
                            primera = filas.first
                            celdas = primera.locator("td")
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
                            ya = set(lineas)
                            for etq, val in pares:
                                linea = f"{etq}: {val}"
                                if linea not in ya:
                                    lineas.append(linea)
                            mensaje = "\n".join(lineas).strip() or "Resultado encontrado."
                            score = 0
                        else:
                            if await page.locator("#success").count() > 0:
                                raw = (await page.locator("#success").first.inner_text() or "").strip()
                                mensaje = " ".join(raw.split())
                                score = 10
                            else:
                                html_low = (await page.content()).lower()
                                if "no se ha encontrado" in html_low or "sin registro" in html_low or "no se encuentra" in html_low:
                                    mensaje = "La persona no se encuentra activa o no está registrada."
                                    score = 10
                                elif any(k in html_low for k in ("nuip", "departamento", "municipio", "puesto", "mesa")):
                                    mensaje = "Resultado encontrado (posible coincidencia)."
                                    score = 0
                                else:
                                    mensaje = "No se detectó resultado claro. Revisar captura."
                                    score = 1
                except Exception as e:
                    # si detectamos captcha inválido en esta fase, forzamos reintento completo
                    if "Captcha inválido" in str(e) or "captcha inválido" in str(e).lower():
                        raise
                    logger.exception("Error analizando resultado: %s", e)
                    mensaje = "Error al analizar el resultado."
                    score = 1

                # guardar resultado y salir
                await _guardar_resultado(consulta_id, fuente_obj, "Validado", mensaje, rel_png)

                # cerrar contexto y navegador
                try:
                    await ctx.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass
                return

        except Exception as e:
            logger.warning("Intento %d falló: %s", intentos, e)
            # guardar diagnóstico
            try:
                if page:
                    html = await page.content()
                    with open(html_debug, "w", encoding="utf-8") as fh:
                        fh.write(html)
                    await page.screenshot(path=abs_png, full_page=True)
            except Exception:
                pass
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass

            # si alcanzamos max intentos, registrar fallo definitivo
            if intentos >= 3:
                await _guardar_resultado(consulta_id, fuente_obj, "Sin validar", "Ocurrió un problema al obtener la información de la fuente", rel_png)
                return

            # esperar un poco antes del siguiente intento (jitter)
            await asyncio.sleep(2.0 + random.random() * 3.2)

    # si salimos del loop sin retorno, registrar error genérico
    await _guardar_resultado(consulta_id, fuente_obj, "Sin validar", "No fue posible completar la consulta tras varios intentos.", rel_png)
