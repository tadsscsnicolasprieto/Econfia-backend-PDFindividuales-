# core/bots/bicibogota.py
import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async

from core.models import Consulta, Resultado, Fuente, TipoFuente  # ajusta 'core' si tu app se llama distinto
import traceback

url = "https://registrobicibogota.movilidadbogota.gov.co/rdbici/#/consultarEstado"
nombre_sitio = "bicibogota"

TIPO_DOC_MAP = {
    'CC': 'CC',
    'CE': 'CE',
    'NIT': 'NIT',
    'NUIP': 'NUIP',
    'PAS': 'PAS',
    'PEP': 'PEP',
}

async def consultar_bicibogota(consulta_id: int, cedula: str, tipo_doc: str):
    """
    Abre Registro Bici Bogotá, llena tipo y número, envía y toma screenshot.
    Lógica:
      - Si se muestra modal "Usuario No Existe" => mensaje, score=0.
      - De lo contrario => "Se han encontrado hallazgos", score=10.
    """
    async def _get_fuente_by_nombre(nombre: str):
        """Obtiene la `Fuente` por nombre; si no existe, la crea usando el primer `TipoFuente` disponible o uno por defecto."""
        def _get_or_create():
            f = Fuente.objects.filter(nombre=nombre).first()
            if f:
                return f
            tipo = TipoFuente.objects.first()
            if not tipo:
                tipo = TipoFuente.objects.create(nombre="default", peso=1, probabilidad=1)
            f = Fuente.objects.create(tipo=tipo, nombre=nombre, nombre_pila=nombre)
            return f

        return await sync_to_async(_get_or_create)()

    async def _crear_resultado(estado: str, archivo: str, mensaje: str, fuente, score: float):
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente,
            estado=estado,
            mensaje=mensaje,
            archivo=archivo,
            score=score
        )

    try:
        # Validar que exista la consulta
        await sync_to_async(Consulta.objects.get)(id=consulta_id)

        print(f"[bicibogota] Inicio consulta_id={consulta_id} cedula={cedula}")

        fuente = await _get_fuente_by_nombre(nombre_sitio)
        tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())

        # Leer variables de entorno para pruebas locales (visual/slow_mo)
        headless_env = os.environ.get("BICIBOGOTA_HEADLESS", "true").lower()
        headless_flag = headless_env not in ["false", "0", "no"]
        slow_mo_env = os.environ.get("BICIBOGOTA_SLOW_MO", "0")
        try:
            slow_mo = int(slow_mo_env)
        except Exception:
            slow_mo = 0

        navegador = None
        try:
            async with async_playwright() as p:
                navegador = await p.chromium.launch(headless=headless_flag, slow_mo=slow_mo)
                # Crear contexto con opciones que imitan un navegador real
                # Allow override via env BICIBOGOTA_USER_AGENT and BICIBOGOTA_HEADERS (simple comma separated key:value)
                env_ua = os.environ.get("BICIBOGOTA_USER_AGENT")
                default_ua = env_ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                extra_headers = {
                    "referer": "https://registrobicibogota.movilidadbogota.gov.co/",
                    "accept-language": "es-ES,es;q=0.9,en;q=0.8",
                }
                # env headers support: KEY:VALUE,KEY:VALUE
                hdrs_env = os.environ.get("BICIBOGOTA_HEADERS")
                if hdrs_env:
                    for item in hdrs_env.split(","):
                        if ":" in item:
                            k, v = item.split(":", 1)
                            extra_headers[k.strip()] = v.strip()

                # Intentar navegar y comprobar status; si recibimos 403, reintentar con distintos lanzamientos
                context = await navegador.new_context(user_agent=default_ua, extra_http_headers=extra_headers, ignore_https_errors=True)
                pagina = await context.new_page()
                try:
                    resp = await pagina.goto(url, wait_until="domcontentloaded", timeout=120_000)
                    status = resp.status if resp else None
                    if status == 403:
                        debug_path = os.path.join(settings.MEDIA_ROOT, relative_folder, f"{nombre_sitio}_{cedula}_403_{timestamp}.png")
                        try:
                            await pagina.screenshot(path=debug_path, full_page=True)
                        except Exception:
                            pass
                        # intentar fallback lanzando chrome por canal del sistema con flags más permisivos
                        try:
                            await context.close()
                        except Exception:
                            pass
                        try:
                            await navegador.close()
                        except Exception:
                            pass
                        print(f"[bicibogota] Recibimos 403, intentando relanzar Chromium con flags alternativos")
                        try:
                            navegador = await p.chromium.launch(headless=True, args=['--no-sandbox','--disable-gpu','--disable-dev-shm-usage'], channel='chrome')
                            context = await navegador.new_context(user_agent=default_ua, extra_http_headers=extra_headers, ignore_https_errors=True)
                            pagina = await context.new_page()
                            resp2 = await pagina.goto(url, wait_until="domcontentloaded", timeout=120_000)
                            status2 = resp2.status if resp2 else None
                            if status2 == 403:
                                raise Exception("Recibido 403 incluso tras fallback launch")
                        except Exception as e2:
                            print(f"[bicibogota] Fallback navigation also failed: {e2}")
                            raise
                except Exception as e:
                    msg = str(e)
                    print(f"[bicibogota] Navegación falló: {msg}")
                    # volver a lanzar para manejo superior
                    raise

                # Esperar a que monte el formulario (SPA)
                await pagina.wait_for_timeout(1500)

                # Prepara carpeta de debug temprana por si hay fallos al encontrar selectores
                relative_folder = os.path.join('resultados', str(consulta_id))
                absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
                os.makedirs(absolute_folder, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

                # Intentar selector principal; si falla, probar alternativas y tomar captura de depuración
                tipo_selector = None
                try:
                    await pagina.wait_for_selector('select[name="tipoDocumento"]', timeout=30_000)
                    tipo_selector = 'select[name="tipoDocumento"]'
                except Exception:
                    # tomar screenshot de depuración
                    debug_path = os.path.join(absolute_folder, f"{nombre_sitio}_{cedula}_no_selector_{timestamp}.png")
                    try:
                        await pagina.screenshot(path=debug_path, full_page=True)
                    except Exception:
                        pass

                    # probar selectores alternativos
                    alt_selectors = [
                        'select#tipoDocumento',
                        'select[id="tipoDocumento"]',
                        'select[name="tipoDocumento"]',
                        'select[name="tipo"]',
                        'select'
                    ]
                    for sel in alt_selectors:
                        try:
                            await pagina.wait_for_selector(sel, timeout=3000)
                            tipo_selector = sel
                            break
                        except Exception:
                            continue

                    if not tipo_selector:
                        raise Exception(f"No se encontró selector de tipo de documento. Captura de depuración: {debug_path}")

                # Completar y enviar (solo si hay valor válido)
                if tipo_doc_val and tipo_selector:
                    await pagina.select_option(tipo_selector, tipo_doc_val)
                await pagina.fill('input[name="numeroDocumento"]', str(cedula))
                await pagina.click('button[type="submit"]')

                # Dar tiempo a la consulta / render
                try:
                    await pagina.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                await pagina.wait_for_timeout(2500)

                # Detectar modal "Usuario No Existe"
                not_found = False
                try:
                    # Espera breve por el modal de SweetAlert
                    await pagina.wait_for_selector(".swal-modal .swal-title", timeout=3000)
                    titulo = (await pagina.locator(".swal-modal .swal-title").inner_text() or "").strip().lower()
                    if "usuario no existe" in titulo:
                        not_found = True
                except Exception:
                    # No apareció el modal en el tiempo dado => asumimos hallazgos
                    not_found = False

                # Guardar screenshot
                relative_folder = os.path.join('resultados', str(consulta_id))
                absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
                os.makedirs(absolute_folder, exist_ok=True)

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
                absolute_path = os.path.join(absolute_folder, screenshot_name)
                relative_path = os.path.join(relative_folder, screenshot_name).replace("\\", "/")

                # tomar screenshot más ligera para ahorrar tiempo
                try:
                    await pagina.screenshot(path=absolute_path, full_page=False)
                except Exception:
                    await pagina.screenshot(path=absolute_path, full_page=True)

        finally:
            if navegador:
                try:
                    await navegador.close()
                except Exception:
                    pass

        # Mensaje / score según detección
        if not_found:
            mensaje = "Usuario No Existe"
            score = 0
        else:
            mensaje = "Se han encontrado hallazgos"
            score = 10

        await _crear_resultado("Validada", relative_path, mensaje, fuente, score)

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[bicibogota] Error consulta_id={consulta_id} cedula={cedula}: {e}\n{tb}")
        try:
            fuente = await _get_fuente_by_nombre(nombre_sitio)
        except Exception:
            fuente = None
        await _crear_resultado("Sin validar", "", str(e) + "\n" + tb, fuente, score=0)
