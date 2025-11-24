# core/bots/boletin_fiscalia.py
import os
import re
import unicodedata
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Fuente, Resultado

nombre_sitio = "boletin_fiscalia"

# ------------ helpers ------------
def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))

def _norm_lower(s: str) -> str:
    s = _strip_accents(s)
    s = re.sub(r"\s+", " ", s or "")
    return s.strip().lower()

def _norm_rel(path: str) -> str:
    return path.replace("\\", "/")
# ---------------------------------

async def consultar_boletin_fiscalia(cedula: str, consulta_id: int, nombre: str):
    """
    - score 0: bloque 'no resultados'
    - score 5: hay cards y el nombre completo aparece EXACTO (ignorando acentos/mayúsculas) en alguno
    - score 1: hay cards pero sin coincidencia exacta
    """
    url = f"https://www.fiscalia.gov.co/colombia/?s={nombre}"
    navegador = None
    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            pagina = await navegador.new_page()
            await pagina.goto(url, wait_until="domcontentloaded")

            # Espera conservadora para que se renderice el listado
            try:
                await pagina.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await pagina.wait_for_timeout(1500)

            # ---------- screenshot ----------
            relative_folder = os.path.join("resultado", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_name = f"{nombre_sitio}_{cedula}_{timestamp}.png"
            absolute_path = os.path.join(absolute_folder, screenshot_name)
            relative_path = _norm_rel(os.path.join(relative_folder, screenshot_name))

            bloque = pagina.locator(".td-ss-main-content")
            if await bloque.count() > 0:
                await bloque.first.screenshot(path=absolute_path)
            else:
                await pagina.screenshot(path=absolute_path, full_page=True)

            # ---------- lógica de resultados ----------
            # 0) "No hay resultados"
            nores = pagina.locator(".no-results h2")
            if await nores.count() > 0:
                h2_text = (await nores.first.inner_text() or "").strip()
                mensaje = h2_text or "No hay ningún resultado de su búsqueda"
                score = 0
            else:
                # 1) Extraer textos de los cards (td_module_16)
                #    (si quieres ampliar, agrega otras variantes de módulos a la lista)
                cards_sel = ".td_module_16.td_module_wrap, .td_module_16"
                try:
                    cards_texts = await pagina.evaluate(
                        """(sel) => Array.from(document.querySelectorAll(sel))
                               .map(el => el.innerText || "")""",
                        cards_sel
                    )
                except Exception:
                    cards_texts = []

                total_cards = len(cards_texts)

                if total_cards == 0:
                    # No aparece el bloque 'no results', pero tampoco hay cards visibles
                    # -> tratamos como hallazgos genéricos
                    mensaje = "Se encontraron hallazgos similares con el nombre introducido"
                    score = 1
                else:
                    # 2) Coincidencia EXACTA del nombre dentro del contenido de al menos un card
                    name_norm = _norm_lower(nombre)
                    patt = re.compile(rf"\b{re.escape(name_norm)}\b")

                    matches = 0
                    for t in cards_texts:
                        if patt.search(_norm_lower(t)):
                            matches += 1

                    if matches > 0:
                        score = 5
                        mensaje = f"Coincidencia exacta del nombre en {matches} resultado(s)."
                    else:
                        score = 1
                        mensaje = "Se encontraron resultados, pero el nombre no aparece exacto en los contenidos."

            # Guardar en BD
            try:
                fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
            except Exception:
                fuente_obj = None

            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=score,
                estado="Validada",
                mensaje=mensaje,
                archivo=relative_path,
            )

            await navegador.close()
            navegador = None

    except Exception as e:
        try:
            fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)
        except Exception:
            fuente_obj = None
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Sin Validar",
            mensaje=str(e),
            archivo="",
        )
        try:
            if navegador:
                await navegador.close()
        except Exception:
            pass
