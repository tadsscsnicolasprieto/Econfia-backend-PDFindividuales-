# core/fallbacks/adres_bio.py
import os, re, tempfile
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright
from core.resolver.captcha_img2 import resolver_captcha_imagen

URL = "https://aplicaciones.adres.gov.co/bdua_internet/Pages/ConsultarAfiliadoWeb.aspx"
TIPO_DOC_MAP = {
    'CC': 'CC', 'TI': 'TI', 'CE': 'CE', 'PA': 'PA', 'RC': 'RC', 'NU': 'NU',
    'AS': 'AS', 'MS': 'MS', 'CD': 'CD', 'CN': 'CN', 'SC': 'SC', 'PE': 'PE', 'PT': 'PT'
}

async def consultar_adres_bio(cedula: str, tipo_doc):
    """Lee ADRES y retorna info biográfica en dict. NO guarda en BD."""
    tipo_val = TIPO_DOC_MAP.get((tipo_doc or "CC").upper(), "CC")
    tmpdir = tempfile.gettempdir()
    max_intentos = 10

    async with async_playwright() as p:
        # Ojo: headless=False para imitar el flujo que ya te funciona con captcha
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto(URL)

        await page.select_option('select[id="tipoDoc"]', tipo_val)
        await page.fill('input[id="txtNumDoc"]', str(cedula))
        await page.wait_for_timeout(800)

        pagina_resultado = page

        # === bucle captcha idéntico al patrón que te funciona ===
        for intento in range(1, max_intentos + 1):
            cap_path = os.path.join(tmpdir, f"captcha_adres_{cedula}.png")
            await page.locator('img#Capcha_CaptchaImageUP').screenshot(path=cap_path)
            captcha_texto = await resolver_captcha_imagen(cap_path)
            try:
                os.remove(cap_path)
            except Exception:
                pass

            try:
                async with page.expect_popup() as popup_info:
                    await page.fill('input[id="Capcha_CaptchaTextBox"]', captcha_texto)
                    await page.click("input[type='submit']")
                nueva = await popup_info.value
                await nueva.wait_for_load_state("networkidle")
                pagina_resultado = nueva
            except Exception:
                await page.wait_for_load_state("networkidle")
                pagina_resultado = page

            # captcha incorrecto
            if await pagina_resultado.locator('span#Capcha_ctl00').is_visible():
                txt = (await pagina_resultado.locator('span#Capcha_ctl00').inner_text()).strip().lower()
                if "no es valido" in txt:
                    try:
                        if pagina_resultado is not page:
                            await pagina_resultado.close()
                    except Exception:
                        pass
                    continue
            break  # salió bien

        # === ¿No afiliado? ===
        no_af = pagina_resultado.locator("div#PanelNoAfiliado span#lblError")
        if await no_af.count() > 0 and await no_af.first.is_visible():
            # Sin datos útiles -> retornamos vacío para que el caller decida
            await browser.close()
            return {}

        # === Parse de la tabla básica ===
        filas = pagina_resultado.locator('table#GridViewBasica tr')
        n = await filas.count()
        pares = {}
        for i in range(1, n):  # saltar header
            celdas = filas.nth(i).locator("td")
            if await celdas.count() >= 2:
                k = (await celdas.nth(0).inner_text() or "").strip().upper()
                v = (await celdas.nth(1).inner_text() or "").strip()
                pares[k] = v

        await browser.close()

        if not pares:
            return {}

        # Mapeo a tus campos
        tipo_doc_res = pares.get("TIPO DE IDENTIFICACIÓN", tipo_val)
        numero_res   = pares.get("NÚMERO DE IDENTIFICACION", str(cedula))
        nombres      = pares.get("NOMBRES", "")
        apellidos    = pares.get("APELLIDOS", "")

        # Devuelve la forma que espera tu creación de Candidato
        return {
            "cedula": numero_res,
            "tipo_doc": tipo_doc_res,
            "nombre": nombres,
            "apellido": apellidos,
            "fecha_nacimiento": None,  
            "fecha_expedicion": None,  
            "tipo_persona": "natural",
            "sexo": "",                 
        }
