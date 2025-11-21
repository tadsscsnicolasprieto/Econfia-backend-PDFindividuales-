#  core/bots/certificado_sena.py
# import os
# from datetime import datetime
# from playwright.async_api import async_playwright
# from django.conf import settings
# from asgiref.sync import sync_to_async

# from core.resolver.captcha_img2 import resolver_captcha_imagen
# from core.models import Consulta, Resultado, Fuente  # ajusta 'core' si tu app tiene otro nombre

# url = "https://certificados.sena.edu.co/CertificadoDigital/com.sena.consultacer"
# nombre_sitio = "certificado_sena"

# TIPO_DOC_MAP = {
#     "CC": "CC", "TI": "TI", "CE": "CE", "PEP": "PEP", "PAS": "PAS", "RC": "RC",
#     "DNI": "DNI", "PR": "PR", "PPT": "PPT", "SCP": "SCP", "NIS": "NIS", "RUI": "RUI",
#     "NCS": "NCS", "NIT": "NIT", "PEPFF": "PEPFF", "CUR": "CUR"
# }

# async def consultar_certificado_sena(consulta_id: int, cedula: str, tipo_doc: str):
#     async def _get_fuente():
#         return await sync_to_async(lambda: Fuente.objects.filter(nombre=nombre_sitio).first())()

#     async def _crear_resultado_ok(relative_path: str, mensaje: str = ""):
#         await sync_to_async(Resultado.objects.create)(
#             consulta_id=consulta_id,
#             fuente=await _get_fuente(),
#             estado="Validada",
#             mensaje=mensaje,
#             archivo=relative_path
#         )

#     async def _crear_resultado_error(mensaje: str):
#         await sync_to_async(Resultado.objects.create)(
#             consulta_id=consulta_id,
#             fuente=await _get_fuente(),
#             estado="Sin Validar",
#             mensaje=mensaje,
#             archivo=""
#         )

#     try:
#         # verifica que exista la consulta
#         await sync_to_async(Consulta.objects.get)(id=consulta_id)

#         tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())

#         async with async_playwright() as p:
#             navegador = await p.chromium.launch(headless=True)
#             pagina = await navegador.new_page()
#             await pagina.goto(url)

#             # Rellenar formulario
#             if tipo_doc_val:
#                 await pagina.select_option('#vTIPO_DOCUMENTO', tipo_doc_val)
#             await pagina.fill('#vNUMERO_DOCUMENTO', str(cedula))

#             # Esperar a que el captcha aparezca
#             await pagina.wait_for_selector('#vCAPTCHAIMAGE')

#             # Carpeta por consulta
#             relative_folder = os.path.join('resultados', str(consulta_id))
#             absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
#             os.makedirs(absolute_folder, exist_ok=True)

#             exito = False
#             for intento in range(1, 20 + 1):
#                 # Capturar imagen del captcha
#                 ts = datetime.now().strftime("%Y%m%d_%H%M%S")
#                 captcha_filename = f"captcha_{nombre_sitio}_{cedula}_{ts}.png"
#                 abs_captcha_path = os.path.join(absolute_folder, captcha_filename)

#                 await pagina.locator('#vCAPTCHAIMAGE').screenshot(path=abs_captcha_path)

#                 # Resolver captcha (ASÍNCRONO directo, sin sync_to_async)
#                 texto_captcha = await resolver_captcha_imagen(abs_captcha_path)

#                 # limpiar archivo temporal
#                 try:
#                     os.remove(abs_captcha_path)
#                 except Exception:
#                     pass

#                 # Enviar
#                 await pagina.fill('#vCAPTCHATEXT', (texto_captcha or "").strip())
#                 await pagina.click('#CONSULTAR')

#                 # Si aparece toast-error, reintenta
#                 try:
#                     await pagina.wait_for_selector('.toast-error', timeout=3000)
#                     # error de captcha -> reintento
#                     continue
#                 except Exception:
#                     # No hubo toast-error: asumimos captcha OK
#                     pass

#                 await pagina.wait_for_timeout(5000)

#                 # Contenedor de resultados
#                 contenedor = pagina.locator('div#COLUMNS1_MAINCOLUMNSTABLE')
#                 if await contenedor.count():
#                     result_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
#                     result_filename = f"{nombre_sitio}_{cedula}_{result_ts}.png"
#                     abs_result = os.path.join(absolute_folder, result_filename)
#                     rel_result = os.path.join(relative_folder, result_filename).replace("\\", "/")
#                     await contenedor.screenshot(path=abs_result)
#                     exito = True
#                     break
#                 else:
#                     # A veces el resultado se pinta en toda la página
#                     page_filename = f"{nombre_sitio}_{cedula}_{ts}.png"
#                     abs_result = os.path.join(absolute_folder, page_filename)
#                     rel_result = os.path.join(relative_folder, page_filename).replace("\\", "/")
#                     await pagina.screenshot(path=abs_result, full_page=True)
#                     exito = True
#                     break

#             await navegador.close()

#         if not exito:
#             await _crear_resultado_error(f"No se pudo resolver el captcha en {intento} intentos")
#             return

#         # Persistir OK
#         await _crear_resultado_ok(rel_result, "Consulta realizada correctamente")

#     except Exception as e:
#         await _crear_resultado_error(str(e))
