import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
import asyncio
# Ajusta según tu proyecto
from core.models import Resultado, Fuente
from core.resolver.captcha_v2 import resolver_captcha_v2

url = "https://wsp.registraduria.gov.co/jurados_atipicas/consultar_jurados.php"
site_key = "6LcthjAgAAAAAFIQLxy52074zanHv47cIvmIHglH"
nombre_sitio = "jurados_votacion"


async def consultar_jurados_votacion(consulta_id: int, cedula: str):
    MAX_INTENTOS = 3
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

    # Crear carpeta resultados/<consulta_id>
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
    absolute_path = os.path.join(absolute_folder, screenshot_name)
    relative_path = os.path.join(relative_folder, screenshot_name)

    for intento in range(1, MAX_INTENTOS + 1):
        try:
            print(f"🔄 Intento {intento} de {MAX_INTENTOS}")

            async with async_playwright() as p:
                navegador = await p.chromium.launch(headless=False)
                pagina = await navegador.new_page()
                await pagina.goto(url)

                # Llenar cédula
                await pagina.fill('input[id="cedula"]', str(cedula))

                # Resolver captcha
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
                    token,
                )

                # Enviar formulario
                await pagina.click("input[type='submit']")
                await asyncio.sleep(2) 
                await pagina.wait_for_selector("#consulta_resp", timeout=8000)

                # Guardar pantallazo completo de la página
                await pagina.screenshot(path=absolute_path, full_page=True)

                # Extraer mensaje completo de #consulta_resp
                elemento = pagina.locator("#consulta_resp")
                texto_portal = (await elemento.inner_text() or "").strip()
                mensaje_final = " ".join(texto_portal.split())  # limpiar saltos y espacios extra

                # Determinar score en base al texto
                low = mensaje_final.lower()
                if ("no ha sido designado" in low) or ("no figura" in low) or ("aún no figura" in low):
                    score_final = 0
                else:
                    score_final = 10

                await navegador.close()
                navegador = None

            # Registrar resultado
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score_final,
                estado="Validada",
                mensaje=mensaje_final,   # ahora guardamos el texto real del portal
                archivo=relative_path    # screenshot de página completa
            )
            return  # ✅ éxito, no seguimos reintentando

        except Exception as e:
            print(f"❌ Error en intento {intento}: {e}")
            error_path = ""
            try:
                if navegador is not None:
                    error_path = os.path.join(
                        absolute_folder,
                        f"{nombre_sitio}_{cedula}_{timestamp}_error.png"
                    )
                    await pagina.screenshot(path=error_path, full_page=True)
                    relative_path = os.path.join(relative_folder, os.path.basename(error_path))
            except Exception:
                relative_path = ""

            finally:
                try:
                    if navegador is not None:
                        await navegador.close()
                except Exception:
                    pass

            if intento == MAX_INTENTOS:
                # Registrar error solo si agotamos los intentos
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin Validar",
                    mensaje="No se pudo realizar la consulta en el momento.",
                    archivo=relative_path
                )
