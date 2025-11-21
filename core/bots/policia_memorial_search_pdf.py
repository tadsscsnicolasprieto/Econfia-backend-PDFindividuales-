import os
import re
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Fuente, Resultado

URL = "https://www.policia.es/_es/tupolicia_memorial_timeline_victimas.php"
NOMBRE_SITIO = "policia_memorial_search"

def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

PRINT_CLEAN = """
@media print {
  header, nav, footer, #aceptaCookies, .cookies, .cookie, .modal, .navbar,
  .navbar-fixed-top, .offcanvas, .btn-back, .share, .banner, .ads, .social {
    display:none !important;
  }
  body { margin: 0 !important; padding: 0 !important; }
}
"""

async def consultar_policia_memorial_search_pdf(consulta_id: int, nombre: str, cedula):
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

    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = _safe_name(nombre)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{cedula}_{ts}.png")
    screenshot_rel = os.path.join(relative_folder, os.path.basename(screenshot_abs))

    COOKIE_BTN = "#aceptaCookies"
    SEARCH_ICON = "#searchAll"
    SEARCH_INPUT = "#txtbusqueda"
    RESULTS_HINTS = ["#resultado", "main", "body"]

    # -------- Bucle de reintentos --------
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
                ctx = await browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    locale="es-ES",
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/122.0.0.0 Safari/537.36")
                )
                page = await ctx.new_page()

                # 1) Abrir
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except:
                    pass

                # 2) Aceptar cookies
                try:
                    await page.locator(COOKIE_BTN).click(timeout=2000)
                except:
                    pass

                # 3) Lupa de búsqueda
                try:
                    await page.locator(SEARCH_ICON).click(timeout=4000)
                except:
                    pass

                # 4) Escribir nombre y Enter
                await page.locator(SEARCH_INPUT).wait_for(state="visible", timeout=5000)
                campo = page.locator(SEARCH_INPUT)
                await campo.fill("")
                await campo.type(nombre, delay=25)
                await campo.press("Enter")

                # 5) Esperar resultados
                for sel in RESULTS_HINTS:
                    try:
                        await page.wait_for_selector(sel, timeout=5000)
                        break
                    except:
                        continue

                await page.wait_for_timeout(500)

                # 6) Aplicar estilos limpios (opcional)
                try:
                    await page.add_style_tag(content=PRINT_CLEAN)
                except:
                    pass

                # 7) Guardar pantallazo completo
                await page.screenshot(path=screenshot_abs, full_page=True)
                print(f"Pantallazo guardado en: {screenshot_abs}")

                # Guardar en BD como éxito
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=1,
                    estado="Validado",
                    mensaje="No se econtraron coincidencias con el criterio de busqueda",
                    archivo=screenshot_rel
                )

                await browser.close()
                exito = True

        except Exception as e:
            last_exception = e
            try:
                if 'page' in locals():
                    await page.screenshot(path=screenshot_abs, full_page=True)
            except:
                pass
            if 'browser' in locals():
                await browser.close()

    # -------- Si después de 3 intentos falló --------
    if not exito:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje=f"Ocurrió un problema al obtener la información de la fuente: {last_exception}",
            archivo=screenshot_rel if os.path.exists(screenshot_abs) else ""
        )
