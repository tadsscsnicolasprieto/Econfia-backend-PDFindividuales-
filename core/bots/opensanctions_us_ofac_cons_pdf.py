# bots/opensanctions_us_ofac_cons_img.py
import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://www.opensanctions.org/datasets/us_ofac_cons/"
NOMBRE_SITIO = "opensanctions_us_ofac_cons"

PRINT_CLEAN = """
@media print{
  header, nav, footer, .navbar, .site-footer, .cookiebox,
  .btn-back, .breadcrumb, .sidebar, .alert, .hero,
  .offcanvas, .btn, .pagination, .site-banner { display:none !important; }
  body { margin:0 !important; padding:0 !important; }
}
html, body { overflow: visible !important; }
"""


def _safe_name(s: str) -> str:
    import re
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"


async def _run_scraper(nombre, cedula, out_img_abs):
    input_sel = "form[action*='entities'] input[name='q'], form input[name='q']"
    submit_sel = "form[action*='entities'] button[type='submit'], form button.btn.btn-secondary[type='submit']"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
        page = await ctx.new_page()

        # 1) Ir al sitio
        await page.goto(URL, wait_until="domcontentloaded", timeout=120000)

        # 2) Llenar campo de búsqueda
        await page.wait_for_selector(input_sel, state="visible", timeout=15000)
        campo = page.locator(input_sel).first
        await campo.fill(nombre)

        # 3) Click buscar
        try:
            await page.locator(submit_sel).first.click(timeout=2000)
        except Exception:
            await campo.press("Enter")

        # 4) Esperar carga de resultados
        try:
            await page.wait_for_url("**/entities/**", timeout=20000)
        except Exception:
            pass

        # ===== Nueva lógica: detectar contenedor de alerta =====
        score = 10
        mensaje = "Se encontraron resultados."
        try:
            alert_loc = page.locator("div[role='alert'].alert-warning .alert-heading")
            if await alert_loc.count() > 0:
                alert_text = (await alert_loc.inner_text()).strip()
                if "No matching entities were found" in alert_text:
                    score = 0
                    # Tomar el mensaje completo del contenedor <div role="alert">
                    contenedor = page.locator("div[role='alert'].alert-warning").first
                    mensaje = (await contenedor.inner_text()).strip()
        except Exception:
            pass

        # 5) Captura de pantalla completa
        await page.add_style_tag(content=PRINT_CLEAN)
        await page.screenshot(path=out_img_abs, full_page=True)

        await browser.close()
        return score, mensaje


async def consultar_opensanctions_us_ofac_cons_pdf(consulta_id: int, nombre: str, cedula):
    nombre = (nombre or "").strip()
    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)

    if not nombre:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje="El nombre llegó vacío.",
            archivo="",
        )
        return

    # Carpeta de resultados
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_img_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{cedula}_{ts}.png")
    out_img_rel = os.path.join(relative_folder, os.path.basename(out_img_abs))

    # Intentos con reintento hasta 3 veces
    success = False
    for intento in range(1, 4):
        try:
            score, mensaje = await _run_scraper(nombre, cedula, out_img_abs)
            if os.path.exists(out_img_abs) and os.path.getsize(out_img_abs) > 500:
                success = True
                break
        except Exception as e:
            mensaje = str(e)
            score = 0

    # Guardar resultado
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=score if success else 0,
        estado="Validado" if success else "Sin validar",
        mensaje=mensaje if success else "Ocurrió un problema al obtener la información de la fuente",
        archivo=out_img_rel if success else "",
    )
