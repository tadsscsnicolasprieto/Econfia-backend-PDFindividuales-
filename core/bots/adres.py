import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Consulta, Resultado, Fuente  # <- ajusta si aplica
from core.resolver.captcha_img import resolver_captcha_imagen

url = "https://www.adres.gov.co/consulte-su-eps"
nombre_sitio = "adres"

TIPO_DOC_MAP = {
    'CC': 'CC', 'TI': 'TI', 'CE': 'CE', 'PA': 'PA', 'RC': 'RC', 'NU': 'NU',
    'AS': 'AS', 'MS': 'MS', 'CD': 'CD', 'CN': 'CN', 'SC': 'SC', 'PE': 'PE', 'PT': 'PT'
}

# -------- NUEVOS HELPERS (no tocan captcha) --------

async def _extraer_mensaje_y_score(pagina):
    """
    (mensaje, score)
    - Si aparece #PanelNoAfiliado #lblError => mensaje literal, score 6 (alto)
    - Si aparece #GridViewBasica => mensaje plano con pares 'COLUMNA: DATO', score 0 (bajo)
    - Si no se identifica => mensaje genérico, score 2 (medio)
    """
    # Caso: NO afiliado
    try:
        if await pagina.locator("#PanelNoAfiliado #lblError").is_visible():
            txt = (await pagina.locator("#PanelNoAfiliado #lblError").inner_text()).strip()
            if txt:
                return txt, 6
    except Exception:
        pass

    # Caso: AFILIADO (tabla)
    try:
        if await pagina.locator("#GridViewBasica").is_visible():
            filas = pagina.locator("#GridViewBasica tr")
            n = await filas.count()
            pares = []
            for i in range(1, n):  # saltar header
                celdas = filas.nth(i).locator("td")
                if await celdas.count() >= 2:
                    col = (await celdas.nth(0).inner_text()).strip()
                    val = (await celdas.nth(1).inner_text()).strip()
                    if col and val:
                        pares.append(f"{col}: {val}")
            if pares:
                mensaje = "Información Básica del Afiliado:\n" + "\n".join(pares)
                return mensaje, 0
    except Exception:
        pass

    return "Resultado obtenido (revisar captura).", 2


async def _get_fuente_by_nombre(nombre: str):
    return await sync_to_async(lambda: Fuente.objects.filter(nombre=nombre).first())()

async def _crear_resultado_ok_con_score(consulta_id: int, fuente, relative_path: str, mensaje: str, score: int):
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente,
        estado="Validada",
        mensaje=mensaje,
        archivo=relative_path,
        score=score
    )

async def _crear_resultado_error(consulta_id: int, fuente, mensaje: str):
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente,
        estado="Sin validar",
        mensaje=mensaje,
        archivo=""
    )


# ------------------- FUNCIÓN PRINCIPAL -------------------

async def consultar_adres(consulta_id: int, cedula: str, tipo_doc: str):
    max_intentos = 10
    try:
        # Validación de consulta existente
        await sync_to_async(Consulta.objects.get)(id=consulta_id)
        fuente = await _get_fuente_by_nombre(nombre_sitio)

        async with async_playwright() as p:
            tipo_doc_val = TIPO_DOC_MAP.get(tipo_doc.upper())

            navegador = await p.chromium.launch(headless=False)
            pagina = await navegador.new_page()
            await pagina.goto(url)

            await pagina.select_option('select[id="tipoDoc"]', tipo_doc_val)
            await pagina.fill('input[id="txtNumDoc"]', cedula)
            await pagina.wait_for_timeout(1000)

            # >>> guardar por ID de consulta (no por cédula)
            relative_folder = os.path.join('resultados', str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            pagina_resultado = pagina

            # ========= BUCLE CAPTCHA (NO TOCAR) =========
            for intento in range(1, max_intentos + 1):
                captcha_path = os.path.join(absolute_folder, f"captcha_{nombre_sitio}.png")
                await pagina.locator('img#Capcha_CaptchaImageUP').screenshot(path=captcha_path)

                captcha_texto = await resolver_captcha_imagen(captcha_path)
                print(f"[Intento {intento}] Captcha resuelto:", captcha_texto)
                try:
                    os.remove(captcha_path)
                except Exception:
                    pass

                # Esperar si abre popup
                try:
                    async with pagina.expect_popup() as popup_info:
                        await pagina.fill('input[id="Capcha_CaptchaTextBox"]', captcha_texto)
                        await pagina.click("input[type='submit']")
                    nueva_pagina = await popup_info.value
                    await nueva_pagina.wait_for_load_state("networkidle")
                    pagina_resultado = nueva_pagina
                except:
                    # Si no hay popup, usamos la misma pestaña
                    await pagina.wait_for_load_state("networkidle")
                    pagina_resultado = pagina

                # Verificar si el captcha fue incorrecto
                if await pagina_resultado.locator('span#Capcha_ctl00').is_visible():
                    texto_error = (await pagina_resultado.locator('span#Capcha_ctl00').inner_text()).strip()
                    if "no es valido" in texto_error.lower():
                        print(f"[Intento {intento}] Captcha incorrecto, reintentando...")
                        # si quedó en popup, ciérralo para reintentar limpio
                        try:
                            if pagina_resultado is not pagina:
                                await pagina_resultado.close()
                        except Exception:
                            pass
                        continue

                break  # Si todo fue bien, salimos del bucle
            # ========= FIN BUCLE CAPTCHA =========

            # Paths de screenshot
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
            absolute_path = os.path.join(absolute_folder, screenshot_name)
            relative_path = os.path.join(relative_folder, screenshot_name)

            # Captura de pantalla
            await pagina_resultado.screenshot(path=absolute_path)
            print("Captura guardada:", relative_path)

            # ===== NUEVO: extraer mensaje y score del DOM (SIN tocar captcha) =====
            mensaje_final, score_final = await _extraer_mensaje_y_score(pagina_resultado)

            await navegador.close()

        # Persistir en BD con mensaje y score
        await _crear_resultado_ok_con_score(consulta_id, fuente, relative_path, mensaje_final, score_final)

    except Exception as e:
        try:
            fuente = await _get_fuente_by_nombre(nombre_sitio)
        except Exception:
            fuente = None
        await _crear_resultado_error(consulta_id, fuente, str(e))
