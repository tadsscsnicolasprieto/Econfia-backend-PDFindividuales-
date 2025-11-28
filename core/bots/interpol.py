# bots/interpol.py
import os
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Resultado, Fuente

INTERPOL_URL = "https://www.interpol.int/es/Como-trabajamos/Notificaciones/Notificaciones-rojas/Ver-las-notificaciones-rojas"
NOMBRE_SITIO = "interpol"

# Script de stealth para sortear detección de Playwright
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {
  get: () => false,
});
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5],
});
Object.defineProperty(navigator, 'languages', {
  get: () => ['es-ES'],
});
"""


async def consultar_interpol(nombre: str, apellido: str, cedula: str, consulta_id: int):
    """
    BOT Interpol corporativo:
    ✔ Busca por nombre y apellido
    ✔ Si hay resultados → guarda pantallazos individuales
    ✔ Siempre guarda pantallazo general
    ✔ Devuelve score=10 si hay coincidencias NO verificables por documento
    ✔ Devuelve score=0 si no existe ninguna coincidencia
    """

    navegador = None
    contexto = None

    # Obtener Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"Fuente no encontrada: {e}", archivo=""
        )
        return

    # Crear carpeta resultados/<id>
    base_folder = os.path.join("resultados", str(consulta_id))
    abs_base_folder = os.path.join(settings.MEDIA_ROOT, base_folder)
    os.makedirs(abs_base_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    general_screenshot = f"interpol_general_{ts}.png"
    abs_general = os.path.join(abs_base_folder, general_screenshot)
    rel_general = os.path.join(base_folder, general_screenshot)

    score_final = 0
    mensaje_final = ""
    cantidad_resultados = 0

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            contexto = await navegador.new_context(
                locale="es-ES",
                viewport={"width": 1500, "height": 950},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                extra_http_headers={
                    "Accept-Language": "es-ES,es;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "DNT": "1",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Cache-Control": "max-age=0",
                }
            )
            page = await contexto.new_page()

            # Aplicar stealth script
            await page.add_init_script(STEALTH_JS)

            # 1️⃣ Ir a Interpol con reintentos
            print("[interpol] Intentando conectar a INTERPOL...")
            for attempt in range(3):
                try:
                    await page.goto(INTERPOL_URL, wait_until="domcontentloaded", timeout=120000)
                    print(f"[interpol] Página cargada en intento {attempt + 1}")
                    break
                except Exception as e:
                    print(f"[interpol] Intento {attempt + 1} falló: {e}")
                    if attempt < 2:
                        await asyncio.sleep(2 + attempt)

            # Esperar a que el contenido esté disponible
            await page.wait_for_timeout(2000)

            # Detectar si fue bloqueado por seguridad
            body_text = ""
            try:
                body_text = (await page.locator("body").inner_text()).strip()
            except:
                pass

            if "ACCESS TO THIS SITE WAS DENIED" in body_text or "ACCESO A ESTE SITIO" in body_text:
                print("[interpol] Sitio bloqueó acceso (WAF/seguridad). Esperando y reintentando...")
                await page.wait_for_timeout(5000)
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    print("[interpol] Página recargada, esperando 3s más...")
                    await page.wait_for_timeout(3000)
                    body_text = (await page.locator("body").inner_text()).strip()
                except Exception as e:
                    print(f"[interpol] Error en recarga: {e}")

                if "ACCESS TO THIS SITE WAS DENIED" in body_text or "ACCESO A ESTE SITIO" in body_text:
                    print("[interpol] Sitio continúa bloqueado después de reintentos. Abortando.")
                    await contexto.close()
                    await navegador.close()
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id, fuente=fuente_obj, score=0,
                        estado="Sin Validar", 
                        mensaje="Sitio INTERPOL bloqueó la solicitud. Intente nuevamente en unos minutos.",
                        archivo=""
                    )
                    return


            # Aceptar cookies
            for sel in [
                "button#onetrust-accept-btn-handler",
                "button:has-text('Aceptar')",
                "button:has-text('Accept')"
            ]:
                try:
                    await page.locator(sel).first.click(timeout=1200)
                    print("[interpol] Cookies aceptadas")
                    break
                except:
                    pass

            await page.wait_for_timeout(2000)

            # 2️⃣ Completar formulario
            try:
                await page.locator("#forename").fill(nombre.strip())
                print(f"[interpol] Nombre rellenado: {nombre}")
            except Exception as e:
                print(f"[interpol] Error rellenando nombre: {e}")

            await page.wait_for_timeout(500)

            try:
                await page.locator("#name").fill(apellido.strip())
                print(f"[interpol] Apellido rellenado: {apellido}")
            except Exception as e:
                print(f"[interpol] Error rellenando apellido: {e}")

            await page.wait_for_timeout(500)

            # 3️⃣ Buscar
            try:
                await page.locator("button[type='submit']").click(timeout=4000)
                print("[interpol] Botón de búsqueda clickeado")
            except:
                try:
                    await page.keyboard.press("Enter")
                    print("[interpol] Enter presionado para búsqueda")
                except Exception as e:
                    print(f"[interpol] Error en búsqueda: {e}")

            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
                print("[interpol] Página en estado networkidle")
            except:
                print("[interpol] Timeout en networkidle, continuando...")
                pass

            await page.wait_for_timeout(3000)

            # 4️⃣ Obtener contador de resultados
            texto = ""
            try:
                texto = (await page.locator("p.lightText").first.inner_text()).strip()
                print(f"[interpol] Texto encontrado: {texto}")
            except Exception as e:
                print(f"[interpol] Error leyendo resultados: {e}")
                texto = ""

            # Analizar texto para ver cuántos resultados hay
            if "No hay resultados" in texto or "No results" in texto:
                cantidad_resultados = 0
                print("[interpol] No hay resultados")
            else:
                # Texto típico: "14 results for Luis"
                try:
                    cantidad_resultados = int(texto.split()[0])
                    print(f"[interpol] Cantidad de resultados: {cantidad_resultados}")
                except:
                    cantidad_resultados = 0
                    print("[interpol] No se pudo extraer cantidad de resultados")

            # 5️⃣ Tomar pantallazo general SIEMPRE
            await page.screenshot(path=abs_general, full_page=True)

            # 6️⃣ Si NO hay resultados
            if cantidad_resultados == 0:
                score_final = 0
                mensaje_final = (
                    f"No se encontraron coincidencias en INTERPOL para el nombre "
                    f"{nombre} {apellido} y no es posible validar el documento {cedula}."
                )

            # 7️⃣ Si hay resultados → tomar pantallazos individuales
            else:
                score_final = 10
                mensaje_final = (
                    f"Se encontraron {cantidad_resultados} coincidencias públicas en INTERPOL "
                    f"para el nombre {nombre} {apellido}. "
                    f"No es posible verificar coincidencias con el documento {cedula}, "
                    "ya que INTERPOL no publica números de identidad."
                )

                # Seleccionar mini-cards
                cards = page.locator(".redNoticesList .noticeTile__card")

                count_cards = await cards.count()

                for i in range(min(count_cards, cantidad_resultados)):
                    elemento = cards.nth(i)
                    detalle_url = await elemento.locator("a").get_attribute("href")

                    if detalle_url:
                        detalle_url = "https://www.interpol.int" + detalle_url

                        # Abrimos nueva página para detalles
                        page_detalle = await contexto.new_page()
                        await page_detalle.goto(detalle_url, wait_until="domcontentloaded")

                        await page_detalle.wait_for_timeout(2000)

                        # Screenshot del detalle individual
                        detalle_name = f"interpol_detalle_{i+1}_{ts}.png"
                        abs_detalle = os.path.join(abs_base_folder, detalle_name)
                        await page_detalle.screenshot(path=abs_detalle, full_page=True)

                        await page_detalle.close()

            # Cerrar navegador
            await contexto.close()
            await navegador.close()

        # 8️⃣ Guardar resultado general
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score_final,
            estado="Validada",
            mensaje=mensaje_final,
            archivo=rel_general,
        )

    except Exception as e:
        try:
            if contexto:
                await contexto.close()
        except:
            pass

        try:
            if navegador:
                await navegador.close()
        except:
            pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje=str(e),
            archivo="",
        )
