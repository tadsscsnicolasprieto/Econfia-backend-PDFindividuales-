import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

# Ajusta estos imports a tu app real
from core.models import Resultado, Fuente
from core.resolver.captcha_v2 import resolver_captcha_v2  # Versión async

url = "https://wsp.registraduria.gov.co/censo/consultar/"
site_key = "6LcthjAgAAAAAFIQLxy52074zanHv47cIvmIHglH"
nombre_sitio = "lugar_votacion"


async def consultar_lugar_votacion(consulta_id: int, cedula: str):
    navegador = None
    fuente_obj = None

    # Buscar la fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{nombre_sitio}': {e}",
            archivo=""
        )
        return

    try:
        # Carpeta resultados/<consulta_id>
        relative_folder = os.path.join('resultados', str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
        absolute_path = os.path.join(absolute_folder, screenshot_name)
        relative_path = os.path.join(relative_folder, screenshot_name)

        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()
            await pagina.goto(url)

            await pagina.fill('#nuip', str(cedula))

            # Resolver captcha async
            token = await resolver_captcha_v2(url, site_key)

            await pagina.evaluate(
                """
                (token) => {
                    let el = document.getElementById('g-recaptcha-response');
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
                token
            )

            await pagina.click("input[type='submit']")
            await pagina.wait_for_selector("#consulta, #success", timeout=8000)
            await pagina.wait_for_timeout(1000)

            # ===================== score + mensaje =====================
            score_final = 0
            mensaje_final = ""

            try:
                tabla = pagina.locator("#consulta")
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
                    success = pagina.locator("#success")
                    if await success.count() > 0:
                        raw = (await success.inner_text() or "").strip()
                        mensaje_final = " ".join(raw.split())
                        score_final = 10  # no encontrado => score 10
                    else:
                        # fallback de no encontrado
                        mensaje_final = "El documento consultado no se encuentra en el censo para esta elección."
                        score_final = 10
            except Exception:
                # Si algo falla, considerar como no encontrado
                mensaje_final = "No fue posible determinar la información del lugar de votación."
                score_final = 10
            # ===========================================================

            # Screenshot del contenedor principal
            elemento = pagina.locator("#form")
            if await elemento.count() > 0:
                await elemento.screenshot(path=absolute_path)
            else:
                await pagina.screenshot(path=absolute_path, full_page=True)

            await navegador.close()
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
