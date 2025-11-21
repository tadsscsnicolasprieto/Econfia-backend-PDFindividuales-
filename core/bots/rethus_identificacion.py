# bots/rethus_identificacion.py
import os
import re
import asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente
from core.resolver.captcha_img2 import resolver_captcha_imagen

NOMBRE_SITIO = "rethus_identificacion"

# Selectores del formulario
SEL_TIPO_DOC   = "#ctl00_cntContenido_ddlTipoIdentificacion"
SEL_NUMERO     = "#ctl00_cntContenido_txtNumeroIdentificacion"
SEL_NOMBRE     = "#ctl00_cntContenido_txtPrimerNombre"
SEL_APELLIDO   = "#ctl00_cntContenido_txtPrimerApellido"

# Captcha
SEL_CAPTCHA_IMG      = "#imgCaptcha"
SEL_CAPTCHA_INPUT    = "#ctl00_cntContenido_txtCatpchaConfirmation"
SEL_CAPTCHA_REFRESH  = "a:has-text('Cambiar'), img[title*='Cambiar'], img[alt*='Cambiar']"

# Botón submit
SEL_BTN_VERIFICAR = "#ctl00_cntContenido_btnVerificarIdentificacion"

# Señales de resultado
SEL_RESULT_HINTS = [
    "#ctl00_cntContenido_gvTH",  # grid de resultados
    "text=ReTHUS", "table", "div.card", "div.panel"
]
SEL_ERROR_CAPTCHA = "text=captcha, text=Captcha, text=CAPTCHA"

# Tiempos
WAIT_AFTER_NAV     = 15000
WAIT_AFTER_CLICK   = 2500
EXTRA_RESULT_SLEEP = 1500


def _norm_tipo(tipo_doc: str) -> str:
    v = (str(tipo_doc) or "").strip().upper()
    return v if v in {"CC", "CE", "PT", "TI"} else "CC"


async def _resolver_y_llenar_captcha(page, carpeta_tmp: str) -> bool:
    """Descarga y resuelve el captcha."""
    try:
        await page.wait_for_selector(SEL_CAPTCHA_IMG, timeout=15000)
        src = await page.locator(SEL_CAPTCHA_IMG).get_attribute("src")
        if not src:
            return False

        resp = await page.request.get(src)
        content = await resp.body()

        os.makedirs(carpeta_tmp, exist_ok=True)
        captcha_path = os.path.join(carpeta_tmp, "captcha_rethus.png")
        with open(captcha_path, "wb") as f:
            f.write(content)

        codigo = await resolver_captcha_imagen(captcha_path)
        if not codigo:
            return False

        await page.wait_for_selector(SEL_CAPTCHA_INPUT, timeout=10000)
        inp = page.locator(SEL_CAPTCHA_INPUT)
        try:
            await inp.fill("")
        except Exception:
            pass
        await inp.type(str(codigo), delay=40)
        return True
    except Exception:
        return False


async def consultar_rethus_identificacion(
    consulta_id: int,
    tipo_doc: str,
    numero: str,
    primer_nombre: str = "",
    primer_apellido: str = ""
):
    """
    Consulta pública ReTHUS:
    - Completa formulario y captcha
    - Busca resultados
    - Screenshot viewport centrado en el div de resultados
    - Guarda con estado Validado o Sin validar
    """
    browser = None
    abs_png, rel_png = "", ""

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    try:
        # Carpeta resultados/<consulta_id>
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        tmp_folder = os.path.join(absolute_folder, "tmp")
        os.makedirs(tmp_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_num = re.sub(r"\s+", "_", (numero or "").strip()) or "consulta"
        png_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"
        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = await browser.new_context(
                viewport={"width": 1440, "height": 1200},
                locale="es-CO"
            )
            page = await ctx.new_page()

            # Navegar
            await page.goto(
                "https://web.sispro.gov.co/THS/Cliente/ConsultasPublicas/ConsultaPublicaDeTHxIdentificacion.aspx",
                wait_until="domcontentloaded",
                timeout=120000
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_NAV)
            except Exception:
                pass

            # Llenar formulario
            await page.select_option(SEL_TIPO_DOC, value=_norm_tipo(tipo_doc))
            await page.fill(SEL_NUMERO, str(numero or ""))
            if primer_nombre:
                try:
                    await page.fill(SEL_NOMBRE, primer_nombre[:12])
                except Exception:
                    pass
            if primer_apellido:
                try:
                    await page.fill(SEL_APELLIDO, primer_apellido[:12])
                except Exception:
                    pass

            # Resolver captcha (hasta 3 intentos)
            for intento in range(3):
                ok = await _resolver_y_llenar_captcha(page, tmp_folder)
                if not ok:
                    try:
                        old_src = await page.locator(SEL_CAPTCHA_IMG).get_attribute("src")
                        await page.locator(SEL_CAPTCHA_REFRESH).first.click()
                        await page.wait_for_function(
                            """(oldSrc) => {
                                const img = document.querySelector('#imgCaptcha');
                                return img && img.src && img.src !== oldSrc;
                            }""",
                            arg=old_src, timeout=10000
                        )
                    except Exception:
                        pass
                    continue

                # Verificar
                await page.locator(SEL_BTN_VERIFICAR).click()
                try:
                    await page.wait_for_load_state("networkidle", timeout=WAIT_AFTER_CLICK)
                except Exception:
                    pass
                await asyncio.sleep(EXTRA_RESULT_SLEEP / 1000)

                # ¿falló captcha?
                if await page.locator(SEL_ERROR_CAPTCHA).count() > 0:
                    try:
                        old_src = await page.locator(SEL_CAPTCHA_IMG).get_attribute("src")
                        await page.locator(SEL_CAPTCHA_REFRESH).first.click()
                        await page.wait_for_function(
                            """(oldSrc) => {
                                const img = document.querySelector('#imgCaptcha');
                                return img && img.src && img.src !== oldSrc;
                            }""",
                            arg=old_src, timeout=10000
                        )
                    except Exception:
                        pass
                    continue
                else:
                    break

            # Señales de resultado
            found = False
            sel_found = None
            for sel in SEL_RESULT_HINTS:
                try:
                    locator = page.locator(sel).first
                    await locator.wait_for(state="visible", timeout=3000)
                    found = True
                    sel_found = locator
                    break
                except Exception:
                    continue

            # Reescribir campos para que salgan en el screenshot
            try:
                await page.fill(SEL_NUMERO, str(numero or ""))
                if primer_nombre:
                    await page.fill(SEL_NOMBRE, primer_nombre[:12])
                if primer_apellido:
                    await page.fill(SEL_APELLIDO, primer_apellido[:12])
            except Exception:
                pass

            # Centrar en el div encontrado
            if sel_found:
                try:
                    await sel_found.scroll_into_view_if_needed(timeout=2000)
                    await asyncio.sleep(1)
                except Exception:
                    pass

            # Screenshot SOLO de lo visible en la ventana (viewport entero, no toda la página)
            await page.screenshot(path=abs_png, full_page=False, clip=None)

            await ctx.close()
            await browser.close()
            browser = None

        # Guardar resultado
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Validado" if found else "Sin validar",
            mensaje="" if found else "No se detectaron resultados (se guardó screenshot).",
            archivo=rel_png,
        )

    except Exception as e:
        try:
            # Guardar error como Sin validar
            archivo = rel_png if abs_png and os.path.exists(abs_png) else ""
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin validar",
                mensaje="No se pudo realizar la consulta en el momento.",
                archivo=archivo,
            )
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
