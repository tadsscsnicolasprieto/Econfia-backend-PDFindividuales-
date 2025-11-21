# consulta/dea.py
import os
import re
import asyncio
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://www.dea.gov/es/node/11286"
NOMBRE_SITIO = "dea"

# Detectores de bloqueo (Akamai / edgesuite)
_DEA_BLOCK_RE = re.compile(
    r"(Access\s+Denied|You\s+don't\s+have\s+permission|errors\.edgesuite\.net|Reference\s+#)",
    re.IGNORECASE,
)

def _is_blocked_html(html: str, current_url: str) -> bool:
    if not html:
        return False
    if _DEA_BLOCK_RE.search(html):
        return True
    if "errors.edgesuite.net" in (current_url or ""):
        return True
    return False


async def consultar_dea(consulta_id: int, cedula: str):
    """
    Busca en DEA por cédula y genera evidencia PNG.
    - Si hay bloqueo/error => Sin Validar, score=0, PNG de evidencia.
    - Si no hay bloqueo:
        * sin resultados => Validada, score=0, PNG
        * con resultados => Validada, score=10, PNG
    """
    navegador = None

    # Fuente
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

    # Rutas/evidencia
    carpeta_rel = os.path.join("resultados", str(consulta_id))
    carpeta_abs = os.path.join(settings.MEDIA_ROOT, carpeta_rel)
    os.makedirs(carpeta_abs, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_name = f"{NOMBRE_SITIO}_{consulta_id}_{ts}.png"
    png_abs = os.path.join(carpeta_abs, png_name)
    png_rel = os.path.join(carpeta_rel, png_name).replace("\\", "/")

    cedula = (cedula or "").strip()
    if not cedula:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje="La cédula llegó vacía.",
            archivo="",
        )
        return

    try:
        # ---------- Navegación y búsqueda ----------
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=False)
            page = await navegador.new_page()
            await page.goto(URL, timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            # ¿Bloqueo temprano?
            try:
                html0 = await page.content()
                if _is_blocked_html(html0, page.url):
                    try:
                        await page.screenshot(path=png_abs, full_page=True)
                    except Exception:
                        pass
                    await navegador.close()
                    navegador = None
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=0,
                        estado="Sin Validar",
                        mensaje="La fuente está presentando problemas para la consulta (DEA bloqueó el acceso).",
                        archivo=png_rel if os.path.exists(png_abs) else "",
                    )
                    return
            except Exception:
                pass

            # Rellenar buscador con la CÉDULA y buscar
            try:
                await page.fill("#edit-keywords", cedula)
            except Exception:
                # Fallback: enfocar input y tipear
                await page.click("#edit-keywords", timeout=4000)
                await page.keyboard.type(cedula, delay=30)

            # disparar búsqueda (botón + Enter como respaldo)
            try:
                await page.click(".menu--search-box-button", timeout=4000)
            except Exception:
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass

            # Esperar resultados
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(1.2)

            # ¿Bloqueo luego de buscar?
            try:
                html1 = await page.content()
                if _is_blocked_html(html1, page.url):
                    try:
                        await page.screenshot(path=png_abs, full_page=True)
                    except Exception:
                        pass
                    await navegador.close()
                    navegador = None
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=0,
                        estado="Sin Validar",
                        mensaje="La fuente está presentando problemas para la consulta (DEA bloqueó el acceso).",
                        archivo=png_rel if os.path.exists(png_abs) else "",
                    )
                    return
            except Exception:
                pass

            # === Detectar “no resultados” ===
            score = 0
            mensaje = ""
            try:
                empty_div = page.locator(".l-view__empty")
                if await empty_div.count() > 0 and await empty_div.first.is_visible():
                    # Texto del bloque vacío (solo el texto)
                    mensaje = (await empty_div.first.inner_text() or "").strip() or \
                              "Sorry, no results found. Try entering fewer or more general search terms."
                    score = 0
                else:
                    # Si no aparece el bloque de vacío, asumimos hallazgos
                    mensaje = "se encontraron hallazgos"
                    score = 10
            except Exception:
                mensaje = "se encontraron hallazgos"
                score = 10

            # Evidencia (siempre)
            try:
                await page.screenshot(path=png_abs, full_page=True)
            except Exception:
                pass

            await navegador.close()
            navegador = None

        # ---------- Registrar ----------
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=score,
            estado="Validada",
            mensaje=mensaje,
            archivo=png_rel if os.path.exists(png_abs) else "",
        )

    except Exception as e:
        # Error genérico → Sin Validar + screenshot si hubo
        try:
            if navegador:
                # intentar última evidencia
                try:
                    page = await navegador.new_page()
                    await page.goto(URL, timeout=15000)
                    await page.screenshot(path=png_abs, full_page=True)
                except Exception:
                    pass
                await navegador.close()
        except Exception:
            pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje=str(e),
            archivo=png_rel if os.path.exists(png_abs) else "",
        )
