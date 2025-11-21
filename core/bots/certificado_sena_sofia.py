# bots/certificado_sena_sofia_async.py
# import os
# from datetime import datetime
# from playwright.async_api import async_playwright
# from django.conf import settings

# try:
#     from core.resolver.captcha_img import resolver_captcha_imagen
# except Exception:
#     resolver_captcha_imagen = None  # modo tolerante

# URL = "https://oferta.senasofiaplus.edu.co/sofia-oferta/certificaciones.html"
# NOMBRE_SITIO = "certificado_sena_sofia"

# TIPO_DOC_MAP = {
#     "CC": "CC", "TI": "TI", "CE": "CE", "PEP": "PEP", "PAS": "PAS", "RC": "RC",
#     "DNI": "DNI", "PR": "PR", "PPT": "PPT", "SCP": "SCP", "NIS": "NIS", "RUI": "RUI",
#     "NCS": "NCS", "NIT": "NIT", "PEPFF": "PEPFF", "CUR": "CUR"
# }

# async def consultar_certificado_sena_sofia(cedula: str, tipo_doc: str):
#     tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())
#     if not tipo_doc_val:
#         return {
#             "sitio": NOMBRE_SITIO,
#             "estado": "error",
#             "archivo": "",
#             "mensaje": f"Tipo de documento no soportado: {tipo_doc!r}",
#         }

#     # Carpeta de salida
#     relative_folder = os.path.join("resultados", str(cedula))
#     absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
#     os.makedirs(absolute_folder, exist_ok=True)
#     ts = datetime.now().strftime("%Y%m%d_%H%M%S")
#     screenshot_name = f"{NOMBRE_SITIO}_{cedula}_{ts}.png"
#     screenshot_abs = os.path.join(absolute_folder, screenshot_name)
#     screenshot_rel = os.path.join(relative_folder, screenshot_name)

#     SEL_TIPO = "select#tipoDocumento, select[name='tipoDocumento'], select[id*='tipo']"
#     SEL_NUM  = "input#numeroDocumento, input[name='numeroDocumento'], input[name*='documento'], input[id*='numero']"
#     SEL_BTN  = "button#buscarCertificados, button:has-text('Consultar'), button[type='button']"
#     SEL_CAPTCHA_IMG   = "img#imgCaptcha, img[id*='captcha'], #imgCaptcha img, img[alt*='captcha' i]"
#     SEL_CAPTCHA_INPUT = "input#captcha, input[name='captcha'], input[id*='captcha']"

#     try:
#         async with async_playwright() as p:
#             browser = await p.chromium.launch(
#                 headless=True,
#                 args=["--disable-blink-features=AutomationControlled"]
#             )
#             ctx = await browser.new_context(
#                 viewport={"width": 1440, "height": 1000},
#                 user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
#                             "AppleWebKit/537.36 (KHTML, like Gecko) "
#                             "Chrome/119.0.0.0 Safari/537.36"),
#                 locale="es-CO",
#             )
#             page = await ctx.new_page()

#             await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
#             try:
#                 await page.wait_for_load_state("networkidle", timeout=12000)
#             except Exception:
#                 pass

#             # Seleccionar tipo de documento
#             await page.wait_for_selector(SEL_TIPO, state="visible", timeout=15000)
#             await page.select_option(SEL_TIPO, value=tipo_doc_val)

#             # Ingresar número
#             await page.wait_for_selector(SEL_NUM, state="visible", timeout=15000)
#             num = page.locator(SEL_NUM).first
#             await num.click(force=True)
#             try:
#                 await num.fill("")
#             except Exception:
#                 pass
#             await num.type(str(cedula), delay=20)

#             # Resolver CAPTCHA si existe
#             try:
#                 img = page.locator(SEL_CAPTCHA_IMG).first
#                 if await img.count() and await img.is_visible():
#                     captcha_path = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{cedula}_{ts}_captcha.png")
#                     try:
#                         await img.screenshot(path=captcha_path)
#                     except Exception:
#                         captcha_path = ""

#                     code = ""
#                     if resolver_captcha_imagen and captcha_path:
#                         try:
#                             code = resolver_captcha_imagen(captcha_path)
#                         except Exception:
#                             pass

#                     if code:
#                         cap_input = page.locator(SEL_CAPTCHA_INPUT).first
#                         if await cap_input.count() and await cap_input.is_visible():
#                             try:
#                                 await cap_input.fill("")
#                             except Exception:
#                                 pass
#                             await cap_input.type(code.strip(), delay=30)
#             except Exception:
#                 pass

#             # Click en Consultar
#             btn = page.locator(SEL_BTN).first
#             if await btn.count() and await btn.is_visible():
#                 await btn.click()
#             else:
#                 try:
#                     await num.press("Enter")
#                 except Exception:
#                     pass

#             try:
#                 await page.wait_for_load_state("networkidle", timeout=20000)
#             except Exception:
#                 pass
#             await page.wait_for_timeout(3000)

#             # Captura
#             await page.screenshot(path=screenshot_abs)

#             await ctx.close()
#             await browser.close()

#         return {
#             "sitio": NOMBRE_SITIO,
#             "estado": "ok",
#             "archivo": screenshot_rel,
#             "mensaje": "",
#         }

#     except Exception as e:
#         return {
#             "sitio": NOMBRE_SITIO,
#             "estado": "error",
#             "archivo": "",
#             "mensaje": str(e),
#         }
