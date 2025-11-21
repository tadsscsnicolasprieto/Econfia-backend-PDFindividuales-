# bots/policia_busqueda_general_shot.py
import os
import re
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Fuente, Resultado

URL = "https://www.policia.es/_es/busqueda_gral.php"
NOMBRE_SITIO = "policia_busqueda_general"

def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

async def consultar_policia_busqueda_general_shot(consulta_id: int, nombre: str, cedula):
    nombre = (nombre or "").strip()
    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)

    if not nombre:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje="El nombre llegó vacío.",
            archivo=""
        )
        return

    # Carpetas y rutas
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = _safe_name(nombre)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_png_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{cedula}_{ts}.png")
    out_png_rel = os.path.join(relative_folder, os.path.basename(out_png_abs))

    INPUT = "#zoom_searchbox"
    BTN_BUSCAR = "input[type='submit'][value='Buscar'], button:has-text('Buscar')"

    COOKIE_SELECTORS = [
        "#aceptaCookies", "#aceptarCookie", "#aceptarCookies",
        "button#onetrust-accept-btn-handler",
        "button:has-text('Aceptar y cerrar')",
        "button:has-text('Aceptar')",
    ]

    intentos = 0
    exito = False
    last_exception = None

    while intentos < 3 and not exito:
        intentos += 1
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/120.0.0.0 Safari/537.36"),
                    locale="es-ES",
                )
                page = await context.new_page()

                # 1) Ir a la página
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=12000)
                except:
                    pass

                # 2) Cerrar cookies
                for sel in COOKIE_SELECTORS:
                    try:
                        loc = page.locator(sel)
                        if await loc.count() and await loc.first.is_visible():
                            await loc.first.click(timeout=2000)
                            await page.wait_for_timeout(800)
                            break
                    except:
                        continue

                # 3) Campo de búsqueda
                await page.wait_for_selector(INPUT, state="visible", timeout=15000)
                campo = page.locator(INPUT)
                await campo.click(force=True)
                try:
                    await campo.fill("")
                except:
                    pass
                await campo.type(nombre, delay=25)

                # 4) Buscar
                triggered = False
                try:
                    async with page.expect_navigation(timeout=7000):
                        await campo.press("Enter")
                    triggered = True
                except:
                    pass

                if not triggered:
                    try:
                        btn = page.locator(BTN_BUSCAR)
                        if await btn.count() and await btn.first.is_visible():
                            async with page.expect_navigation(timeout=10000):
                                await btn.first.click()
                            triggered = True
                    except:
                        pass

                # 5) Esperar resultados
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except:
                    pass
                await page.wait_for_timeout(4000)

                # 6) Screenshot (siempre)
                await page.screenshot(path=out_png_abs, full_page=True)

                await context.close()
                await browser.close()

            # Validación tamaño screenshot
            if not os.path.exists(out_png_abs) or os.path.getsize(out_png_abs) < 10_000:
                raise Exception("El pantallazo parece vacío o muy pequeño.")

            # Guardar en BD como éxito
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=1,
                estado="Validado",
                mensaje="No se encuentran coincidencias",
                archivo=out_png_rel
            )
            exito = True

        except Exception as e:
            last_exception = e
            # Pantallazo en caso de error
            try:
                if 'page' in locals():
                    await page.screenshot(path=out_png_abs, full_page=True)
            except:
                pass

    # Si después de 3 intentos no funcionó
    if not exito:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje="Ocurrió un problema al obtener la información de la fuente",
            archivo=out_png_rel if os.path.exists(out_png_abs) else ""
        )
