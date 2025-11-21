# consulta/pruebas_icfes.py
import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Fuente, Resultado
from core.resolver.captcha_v2 import resolver_captcha_v2
import urllib.parse

URL = "https://resultados.icfes.edu.co/resultados-saber2016-web/pages/publicacionResultados/autenticacion/consultaSnp.jsf#No-back-button_x000a_"
NOMBRE_SITIO = "pruebas_icfes"

TIPO_DOC_MAP = {
    "TI": "Tarjeta de identidad",
    "CC": "Cédula de ciudadanía",
    "CE": "Cédula de extranjería",
}

async def consultar_pruebas_icfes(tipo_doc: str, consulta_id: int, fecha_nacimiento: str, cedula: str):
    """
    Automatiza la consulta de ICFES:
      - Si hay resultados: score=0, mensaje="Consulta realizada."
      - Si NO hay resultados/datos inválidos: score=10, mensaje="Los datos no están correctos."
    Siempre guarda un pantallazo y registra el resultado en la BD.
    """
    navegador = None
    fuente_obj = None

    # 0) Fuente
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

    # 1) Carpeta de salida (estandarizamos a 'resultados')
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    # 2) Nombre de archivo
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_name = f"{NOMBRE_SITIO}_{cedula}_{timestamp}.png"
    absolute_path = os.path.join(absolute_folder, screenshot_name)
    relative_path = os.path.join(relative_folder, screenshot_name)

    try:
        async with async_playwright() as p:
            # Validar tipo de documento
            tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())
            if not tipo_doc_val:
                raise ValueError(f"Tipo de documento no válido: {tipo_doc}")

            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()
            await pagina.goto(URL, wait_until="domcontentloaded", timeout=120000)

            # Seleccionar tipo de documento (PrimeFaces selectOneMenu)
            await pagina.click("#selTipoDoc")
            await pagina.wait_for_selector("#selTipoDoc_panel", timeout=15000)
            await pagina.click(f"li.ui-selectonemenu-item:has-text('{tipo_doc_val}')")

            # Llenar formulario
            await pagina.fill('input[id="txtDoc"]', str(cedula))
            await pagina.fill('input[id="calFecNac_input"]', str(fecha_nacimiento))

            # Enviar
            await pagina.click('button[id="autenticarButton"]')

            # Esperar a que aparezca el error O la tabla de resultados
            try:
                await pagina.wait_for_selector("div.ui-messages-error, #tabla_data tr", timeout=15000)
            except Exception:
                # Si nada apareció, esperamos un poco y seguimos evaluando
                await pagina.wait_for_timeout(2000)

            # Tomar pantallazo de la vista post-submit
            await pagina.screenshot(path=absolute_path)

            # Detectar estado
            has_error = False
            try:
                err_loc = pagina.locator("div.ui-messages-error")
                has_error = (await err_loc.count()) > 0 and await err_loc.first.is_visible()
            except Exception:
                has_error = False

            has_table = False
            try:
                has_table = (await pagina.locator("#tabla_data tr").count()) > 0
            except Exception:
                has_table = False

            # Mensaje/score según tu requerimiento
            if has_table and not has_error:
                score_final = 0
                mensaje_final = "Consulta realizada."
            else:
                score_final = 10
                mensaje_final = "Los datos no están correctos."

            # (Opcional) Validación rápida en histórico si hubo tabla (no afecta el registro principal)
            if has_table and not has_error:
                try:
                    # Extraer número de registro por si te sirve en logs
                    numero_registro = await pagina.inner_text("#tabla_data tr td:nth-child(3) label")

                    url_historico = "https://resultadoshistoricos.icfes.edu.co/login"
                    pagina_hist = await navegador.new_page()
                    await pagina_hist.goto(url_historico, wait_until="domcontentloaded", timeout=120000)

                    # Selección de tipo doc (ng-select)
                    await pagina_hist.click("div.ng-select-container")
                    await pagina_hist.wait_for_selector("ng-dropdown-panel div.ng-option", timeout=15000)
                    await pagina_hist.click(f"div.ng-option:has-text('{tipo_doc_val}')")

                    await pagina_hist.fill("input#identificacion", str(cedula))
                    await pagina_hist.fill('input#fechaNacimiento', str(fecha_nacimiento))
                    await pagina_hist.fill("input#numeroRegistro", numero_registro)

                    # Resolver reCAPTCHA v2 (best-effort)
                    try:
                        iframe_el = await pagina_hist.wait_for_selector("iframe[src*='recaptcha/api2']", timeout=15000)
                        iframe_src = await iframe_el.get_attribute("src") or ""
                        parsed = urllib.parse.urlparse(iframe_src)
                        params = urllib.parse.parse_qs(parsed.query)
                        site_key = (params.get("k") or [""])[0]

                        if site_key:
                            token = await resolver_captcha_v2(url_historico, site_key)
                            # Inyectar el token
                            await pagina_hist.evaluate(
                                """(token) => {
                                    let textarea = document.querySelector('#g-recaptcha-response');
                                    if (!textarea) {
                                        textarea = document.createElement('textarea');
                                        textarea.id = 'g-recaptcha-response';
                                        textarea.name = 'g-recaptcha-response';
                                        textarea.style.display = 'none';
                                        document.body.appendChild(textarea);
                                    }
                                    textarea.value = token;
                                    textarea.dispatchEvent(new Event('input', { bubbles: true }));
                                }""",
                                token,
                            )
                            # Intentar enviar
                            await pagina_hist.click('button[type="submit"]', timeout=5000)
                            await pagina_hist.wait_for_timeout(5000)
                    except Exception:
                        pass
                except Exception:
                    pass

            # Cerrar navegador
            await navegador.close()
            navegador = None

            # Registrar en BD (siempre “Validada” porque el flujo corrió)
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score_final,
                estado="Validada",
                mensaje=mensaje_final,
                archivo=relative_path,
            )

    except Exception as e:
        try:
            if navegador is not None:
                await navegador.close()
        except Exception:
            pass

        # Error de ejecución del bot
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje=str(e),
            archivo="",
        )
