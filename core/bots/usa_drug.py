# consulta/usa_drug.py
import os
from datetime import datetime
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

NOMBRE_SITIO = "usa_drug"

def _mk_out_paths(consulta_id: int, cedula: str):
    rel_dir = os.path.join("resultados", str(consulta_id))
    abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{NOMBRE_SITIO}_{cedula}_{ts}.png"
    return abs_dir, rel_dir, os.path.join(abs_dir, fname), os.path.join(rel_dir, fname)

async def consultar_usa_drug(consulta_id: int, nombre: str, apellido: str, cedula: str):
    query = quote_plus(f"{(nombre or '').strip()} {(apellido or '').strip()}".strip())
    url = f"https://www.dea.gov/what-we-do/news/press-releases?keywords={query}"

    abs_dir, rel_dir, shot_abs, shot_rel = _mk_out_paths(consulta_id, cedula)

    navegador = None
    pagina = None

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()

            await pagina.goto(url, timeout=60000, wait_until="domcontentloaded")
            try:
                await pagina.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Screenshot SIEMPRE (para evidenciar Access Denied, resultados, o vacío)
            await pagina.screenshot(path=shot_abs, full_page=True)

            # Detectar bloqueos típicos de Akamai/edgesuite
            body_text = (await pagina.inner_text("body")).strip()
            low = body_text.lower()
            if (
                "access denied" in low
                or "you don't have permission to access" in low
                or "errors.edgesuite.net" in low
            ):
                mensaje = ("El portal de la DEA bloqueó el acceso automático "
                           "(Access Denied). Intenta nuevamente más tarde.")
                fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin validar",
                    mensaje=mensaje,
                    archivo=shot_rel,
                )
                await navegador.close()
                return

            # Si no hay bloqueo, intentar leer contenedor
            nombre_completo = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()
            score = 0
            mensaje = "No se encontraron coincidencias"

            # El sitio renderiza lista de notas; buscamos por texto visible
            try:
                # contenedor general de resultados
                content_sel = "div.l-view__content, main, #main-content"
                try:
                    content = await pagina.inner_text(content_sel, timeout=5000)
                except Exception:
                    content = body_text  # fallback
                if nombre_completo and nombre_completo.lower() in (content or "").lower():
                    score = 10
                    mensaje = "Se encontraron coincidencias"
            except Exception:
                # si falla la lectura, quedamos conservadores
                score = 0
                mensaje = "No se pudieron leer los resultados; revisar evidencia"

            await navegador.close()

            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validada",
                mensaje=mensaje,
                archivo=shot_rel,
            )

    except Exception as e:
        try:
            if pagina is not None and not os.path.exists(shot_abs):
                await pagina.screenshot(path=shot_abs, full_page=True)
        except Exception:
            pass
        try:
            if navegador is not None:
                await navegador.close()
        except Exception:
            pass

        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin validar",
            mensaje="La fuente no se ha podido consultar en este momento.",
            archivo=shot_rel if os.path.exists(shot_abs) else "",
        )
