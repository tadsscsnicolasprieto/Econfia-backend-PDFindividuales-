# solo este archivo: consultar_registraduria.py

import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from core.resolver.captcha_img import resolver_captcha_imagen

url = "https://consultasrc.registraduria.gov.co:28080/ProyectoSCCRC/"
nombre_sitio = "registro_civil"

async def consultar_registraduria(cedula):
    navegador = None
    captcha_path = None
    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(
                headless=False,
                channel="chrome",
                slow_mo=150,
                args=["--start-maximized"]
            )
            pagina = await navegador.new_page()
            await pagina.goto(url, timeout=60000)
            await pagina.wait_for_load_state("domcontentloaded")

            await pagina.click('input[id="controlador:consultasId"]')
            await pagina.wait_for_timeout(500)

            await pagina.select_option(
                'select[id="searchForm:tiposBusqueda"]',
                label='DOCUMENTO (NUIP/NIP/Tarjeta de Identidad)'
            )

            cedula_str = str(cedula).strip()

            MAX_REINTENTOS_FLUJO = 3
            exito_flujo = False

            for intento_flujo in range(1, MAX_REINTENTOS_FLUJO + 1):
                INTENTOS = 3
                for intento in range(1, INTENTOS + 1):
                    await pagina.wait_for_selector('input[id="searchForm:documento"]', timeout=10000)
                    await pagina.fill('input[id="searchForm:documento"]', "")
                    await pagina.fill('input[id="searchForm:documento"]', cedula_str)

                    captcha = pagina.locator("img[src*='kaptcha.jpg']")
                    await captcha.wait_for(state="visible", timeout=15000)

                    captcha_src = await captcha.get_attribute("src")
                    captcha_url = f"https://consultasrc.registraduria.gov.co:28080{captcha_src}"
                    response = await pagina.request.get(captcha_url)
                    image_bytes = await response.body()

                    captcha_path = f"captcha_{nombre_sitio}_{cedula_str}.png"
                    with open(captcha_path, "wb") as f:
                        f.write(image_bytes)

                    captcha_resultado = await resolver_captcha_imagen(captcha_path)
                    await pagina.fill('input[id="searchForm:inCaptcha"]', str(captcha_resultado))

                    await pagina.click('input[id="searchForm:busquedaRCX"]')

                    try:
                        await pagina.wait_for_selector(
                            '[id$=":documento"], ul li:has-text("imagen de verificación")',
                            timeout=10000
                        )
                    except Exception:
                        try:
                            await captcha.click()
                        except Exception:
                            await pagina.reload()
                            await pagina.click('input[id="controlador:consultasId"]')
                            await pagina.select_option(
                                'select[id="searchForm:tiposBusqueda"]',
                                label='DOCUMENTO (NUIP/NIP/Tarjeta de Identidad)'
                            )
                        continue

                    if await pagina.locator("ul li:has-text('imagen de verificación')").count() > 0:
                        try:
                            await captcha.click()
                        except Exception:
                            await pagina.reload()
                            await pagina.click('input[id="controlador:consultasId"]')
                            await pagina.select_option(
                                'select[id="searchForm:tiposBusqueda"]',
                                label='DOCUMENTO (NUIP/NIP/Tarjeta de Identidad)'
                            )
                        continue

                    exito_flujo = True
                    break

                if exito_flujo:
                    break

                await pagina.reload()
                await pagina.wait_for_load_state("domcontentloaded")
                await pagina.click('input[id="controlador:consultasId"]')
                await pagina.wait_for_timeout(300)
                await pagina.select_option(
                    'select[id="searchForm:tiposBusqueda"]',
                    label='DOCUMENTO (NUIP/NIP/Tarjeta de Identidad)'
                )

            if not exito_flujo:
                raise RuntimeError("No se pudo superar el captcha tras varios intentos.")

            # ==== EXTRACCIÓN ROBUSTA ====
            async def get_txt(sel):
                try:
                    loc = pagina.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.wait_for(state="visible", timeout=8000)
                        return (await loc.first.inner_text() or "").strip()
                except Exception:
                    pass
                return ""

            documento       = await get_txt('[id$=":documento"]')
            primer_apellido = await get_txt('[id$=":primerApellido"]')
            segundo_apellido= await get_txt('[id$=":segundoApellido"]')
            primer_nombre   = await get_txt('[id$=":primerNombre"]')
            segundo_nombre  = await get_txt('[id$=":segundoNombre"]')
            sexo            = await get_txt('[id$=":sexo"]')

            # Fallback tabla
            if not any([primer_nombre, segundo_nombre, primer_apellido, segundo_apellido, sexo]):
                filas = pagina.locator("table tr")
                try:
                    n = await filas.count()
                    pares = {}
                    for i in range(n):
                        tds = filas.nth(i).locator("td")
                        if await tds.count() >= 2:
                            k = ((await tds.nth(0).inner_text()) or "").strip().lower()
                            v = ((await tds.nth(1).inner_text()) or "").strip()
                            pares[k] = v
                    primer_nombre    = pares.get("primer nombre", primer_nombre)
                    segundo_nombre   = pares.get("segundo nombre", segundo_nombre)
                    primer_apellido  = pares.get("primer apellido", primer_apellido)
                    segundo_apellido = pares.get("segundo apellido", segundo_apellido)
                    sexo             = pares.get("sexo", sexo)
                    documento        = pares.get("documento", documento)
                except Exception:
                    pass

            # Screenshot debug
            try:
                ss = os.path.join(settings.MEDIA_ROOT, f"debug_reg_{cedula_str}.png")
                await pagina.screenshot(path=ss, full_page=True)
            except Exception:
                pass

            await navegador.close()
            navegador = None

            # ---- Resultado base de Registraduría ----
            datos = {
                'cedula': cedula_str,
                'tarjeta_identidad': documento,
                'tipo_doc': 'CC',
                'nombre': f"{(primer_nombre or '').strip()} {(segundo_nombre or '').strip()}".strip(),
                'apellido': f"{(primer_apellido or '').strip()} {(segundo_apellido or '').strip()}".strip(),
                'fecha_nacimiento': '2005-05-22',
                'fecha_expedicion': '2023-05-25',
                'tipo_persona': 'natural',
                'sexo': (sexo or '').strip(),
            }

        return datos
    finally:
        # Cierra navegador si quedó abierto
        try:
            if navegador is not None:
                await navegador.close()
        except Exception:
            pass
        # Limpia el captcha temporal si quedó
        try:
            if captcha_path and os.path.exists(captcha_path):
                os.remove(captcha_path)
        except Exception:
            pass


