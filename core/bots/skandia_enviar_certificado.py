#     # core/bots/skandia_certificados.py
# import os
# import re
# import asyncio
# from datetime import datetime, date

# from django.conf import settings
# from asgiref.sync import sync_to_async
# from playwright.async_api import async_playwright

# from core.models import Resultado, Fuente
# from core.resolver.captcha_v2 import resolver_captcha_v2  # el mismo helper que usas en otros bots

# URL = "https://www.skandia.co/consulta-extractos-y-certificados"
# NOMBRE_SITIO = "skandia_certificados"

# HEADLESS = False  # pon True cuando termines de probar

# # --- Selectores ---
# SEL_TIPO = "#_com_skandia_co_certificates_web_portlet_SkandiaCoCertificatesWebPortlet_typeDocument"
# SEL_DOC  = "#_com_skandia_co_certificates_web_portlet_SkandiaCoCertificatesWebPortlet_document"
# SEL_YEAR = "#_com_skandia_co_certificates_web_portlet_SkandiaCoCertificatesWebPortlet_yearBirth"
# SEL_BTN  = "#sendCertificate"

# SEL_CAPTCHA_ANCHOR_IFRAME = "iframe[src*='recaptcha/api2/anchor']"
# SEL_CAPTCHA_CONTAINER     = ".g-recaptcha, #g-recaptcha, [data-sitekey]"

# # modal de resultado (éxito / falla)
# SEL_H3_SUCCESS = "h3:has-text('Tu solicitud ha sido enviada con éxito')"
# SEL_H3_FAIL    = "h3:has-text('Tu solicitud ha fallado')"

# # mapeo de tipo de documento
# TIPO_MAP = {
#     "CC": "C",  # Cédula de ciudadanía
#     "CE": "E",
#     "L":  "L",
#     "M":  "M",
#     "N":  "N",
#     "P":  "P",
#     "R":  "R",
#     "TI": "T",
# }

# # sitekey visible en la página (fallback si no se puede leer dinámicamente)
# SITEKEY_FALLBACK = "6Le7iZAgAAAAABS-YU1fbnxjcxEvLtb77q4Z_YvK"


# def _year_only(fecha_nacimiento) -> str:
#     """Acepta date, datetime o string y devuelve AAAA."""
#     if isinstance(fecha_nacimiento, (datetime, date)):
#         return f"{fecha_nacimiento.year:04d}"
#     if isinstance(fecha_nacimiento, str):
#         s = fecha_nacimiento.strip()
#         # intenta detectar AAAA o dd/mm/aa/aaaa
#         m = re.search(r"(\d{4})", s)
#         if m:
#             return m.group(1)
#         m2 = re.search(r"\b(\d{2})/(\d{2})/(\d{2})\b", s)  # dd/mm/aa
#         if m2:
#             aa = int(m2.group(3))
#             return f"20{aa:02d}" if aa <= 30 else f"19{aa:02d}"
#     # último recurso: vacío para que el campo quede en rojo y tengamos evidencia
#     return ""


# async def _crear_resultado(consulta_id, fuente, estado, mensaje, archivo, score=1):
#     rel = archivo.replace("\\", "/") if archivo else ""
#     await sync_to_async(Resultado.objects.create)(
#         consulta_id=consulta_id,
#         fuente=fuente,
#         estado=estado,
#         mensaje=mensaje,
#         archivo=rel,
#         score=score,
#     )


# async def consultar_skandia_certificados(
#     consulta_id: int,
#     tipo_doc: str,         # "CC" | "CE" | ...
#     numero: str,
#     fecha_nacimiento,      # str "dd/mm/aa" o "aaaa", o datetime/date
# ):
#     fuente = await sync_to_async(lambda: Fuente.objects.filter(nombre=NOMBRE_SITIO).first())()
#     if not fuente:
#         await _crear_resultado(consulta_id, None, "Sin Validar",
#                                f"No existe Fuente con nombre='{NOMBRE_SITIO}'", "", score=0)
#         return

#     # rutas
#     folder_rel = os.path.join("resultados", str(consulta_id))
#     folder_abs = os.path.join(settings.MEDIA_ROOT, folder_rel)
#     os.makedirs(folder_abs, exist_ok=True)

#     ts = datetime.now().strftime("%Y%m%d_%H%M%S")
#     base = f"skandia_{numero}_{ts}.png"
#     abs_png = os.path.join(folder_abs, base)
#     rel_png = os.path.join(folder_rel, base).replace("\\", "/")

#     browser = context = page = None

#     # parámetros “rápidos”
#     SHORT = 900
#     TINY  = 350
#     NAV_TIMEOUT = 45_000
#     MAX_RETRIES = 3           # reintentos totales
#     CAPTCHA_WAIT = 2_000      # tiempo para decidir si “no hay captcha”

#     # valores a enviar
#     tipo_val = TIPO_MAP.get((tipo_doc or "").upper(), "C")
#     year_val = _year_only(fecha_nacimiento)
#     numero = str(numero or "").strip()

#     async def close_overlays(p):
#         # banner cookies
#         for sel in [
#             "button:has-text('Aceptar el uso de cookies')",
#             "button:has-text('Acepto')",
#             "button[title*='cookies']",
#         ]:
#             try:
#                 btn = p.locator(sel).first
#                 if await btn.is_visible(timeout=1200):
#                     await btn.click()
#                     await p.wait_for_timeout(TINY)
#             except Exception:
#                 pass
#         # popup marketing “Sí, de una / No, gracias”
#         for sel in ["button:has-text('No, gracias')", "button:has-text('Cerrar')", "button[aria-label='Cerrar']"]:
#             try:
#                 b = p.locator(sel).first
#                 if await b.is_visible(timeout=800):
#                     await b.click()
#                     await p.wait_for_timeout(TINY)
#             except Exception:
#                 pass

#     async def safe_fill(p, selector, value: str):
#         el = p.locator(selector)
#         await el.wait_for(state="visible", timeout=8_000)
#         await el.click()
#         # limpiar robusto
#         try:
#             await el.fill("")
#         except Exception:
#             pass
#         await p.keyboard.press("Control+A")
#         await p.keyboard.press("Delete")
#         if value:
#             await el.type(value, delay=20)
#         await p.wait_for_timeout(80)

#     async def fill_form(p):
#         # tipo doc
#         await p.select_option(SEL_TIPO, value=tipo_val)
#         await p.wait_for_timeout(TINY)
#         # número
#         await safe_fill(p, SEL_DOC, numero)
#         # año (AAAA)
#         await safe_fill(p, SEL_YEAR, year_val)
#         # radio "Afiliación" (suele venir marcado, pero aseguramos)
#         try:
#             await p.locator("label:has-text('Afiliación')").click(timeout=1000)
#         except Exception:
#             pass
#         # desplazamos un poco para que el captcha quede visible y no lo tape el banner
#         await p.evaluate("window.scrollBy(0, 300)")
#         await p.wait_for_timeout(TINY)

#     try:
#         async with async_playwright() as pw:
#             browser = await pw.chromium.launch(headless=HEADLESS)
#             context = await browser.new_context(
#                 locale="es-CO", viewport={"width": 1366, "height": 1000}
#             )
#             page = await context.new_page()

#             mensaje_final = "No se pudo determinar el resultado (revise la evidencia)."

#             for attempt in range(1, MAX_RETRIES + 1):
#                 # navegar
#                 await page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
#                 await page.wait_for_timeout(SHORT)
#                 await close_overlays(page)

#                 # llenar siempre (también en recargas)
#                 await fill_form(page)

#                 # ¿hay captcha visible?
#                 captcha_visible = False
#                 try:
#                     if await page.locator(SEL_CAPTCHA_ANCHOR_IFRAME).first.is_visible(timeout=CAPTCHA_WAIT):
#                         captcha_visible = True
#                 except Exception:
#                     pass
#                 if not captcha_visible:
#                     # intento rápido: recargar y volver a escribir
#                     if attempt < MAX_RETRIES:
#                         await page.reload(wait_until="domcontentloaded")
#                         await page.wait_for_timeout(SHORT)
#                         continue  # siguiente vuelta – reescribe TODO
#                     # último intento: seguimos sin captcha → enviamos igual (dejará modal de fallo)
                
#                 # resolver captcha v2 (checkbox) inyectando token
#                 try:
#                     sitekey = await page.get_attribute(SEL_CAPTCHA_CONTAINER, "data-sitekey")
#                     if not sitekey:
#                         sitekey = SITEKEY_FALLBACK
#                     token = await resolver_captcha_v2(page.url, sitekey)
#                     await page.evaluate(
#                         """(tok)=>{
#                             let el = document.getElementById('g-recaptcha-response');
#                             if(!el){
#                                 el = document.createElement('textarea');
#                                 el.id = 'g-recaptcha-response';
#                                 el.name = 'g-recaptcha-response';
#                                 el.style.display='none';
#                                 document.body.appendChild(el);
#                             }
#                             el.value = tok;
#                             el.dispatchEvent(new Event('input',{bubbles:true}));
#                             el.dispatchEvent(new Event('change',{bubbles:true}));
#                         }""",
#                         token
#                     )
#                 except Exception:
#                     # si falla el solver, seguimos para que muestre “Tu solicitud ha fallado”
#                     pass

#                 # enviar
#                 try:
#                     await page.locator(SEL_BTN).click(timeout=8_000)
#                 except Exception:
#                     # si se movió el botón, intentar por texto
#                     try:
#                         await page.locator("button:has-text('ENVIAR CORREO ELECTRÓNICO')").click(timeout=6_000)
#                     except Exception:
#                         pass

#                 # espera corta a que aparezca modal
#                 await page.wait_for_timeout(1400)

#                 # evaluar 3 estados
#                 if await page.locator(SEL_H3_SUCCESS).first.count():
#                     mensaje_final = "Tu solicitud ha sido enviada con éxito"
#                     break
#                 if await page.locator(SEL_H3_FAIL).first.count():
#                     mensaje_final = "Tu solicitud ha fallado"
#                     # si falló por captcha en 1er/2do intento, recarga y reintenta rápido
#                     if attempt < MAX_RETRIES:
#                         await page.reload(wait_until="domcontentloaded")
#                         await page.wait_for_timeout(SHORT)
#                         continue
#                     break

#                 # si no hay ni éxito ni falla explícita y quedan intentos, reintenta rápido
#                 if attempt < MAX_RETRIES:
#                     await page.reload(wait_until="domcontentloaded")
#                     await page.wait_for_timeout(SHORT)
#                     continue

#             # evidencia
#             await page.wait_for_timeout(400)
#             await page.screenshot(path=abs_png, full_page=True)

#             await context.close()
#             await browser.close()
#             context = browser = None

#         await _crear_resultado(consulta_id, fuente, "Validada", mensaje_final, rel_png, score=1)

#     except Exception as e:
#         try:
#             if page:
#                 try:
#                     await page.screenshot(path=abs_png, full_page=True)
#                 except Exception:
#                     pass
#         except Exception:
#             pass
#         try:
#             if context:
#                 await context.close()
#         except Exception:
#             pass
#         try:
#             if browser:
#                 await browser.close()
#         except Exception:
#             pass

#         await _crear_resultado(
#             consulta_id, fuente, "Sin Validar", f"{type(e).__name__}: {e}",
#             rel_png if os.path.exists(abs_png) else "", score=0
#         )
