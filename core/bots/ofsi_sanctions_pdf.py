# core/bots/ofsi_sanctions_pdf.py
import os
import re
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://sanctionssearchapp.ofsi.hmtreasury.gov.uk/"
NOMBRE_SITIO = "ofsi_sanctions"


def _norm(s: str) -> str:
    """Normaliza para comparar: quita espacios múltiples y pasa a minúsculas."""
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s.lower()


async def consultar_ofsi_pdf(consulta_id: int, nombre: str, cedula):
    """
    - Busca 'nombre' en OFSI.
    - Toma SIEMPRE un screenshot full-page y lo guarda como 'archivo' del Resultado.
    - Tabla:
        * Coincidencia exacta en 'Name' => score 5, mensaje 'Se ha encontrado un hallazgo'.
        * Con resultados pero sin coincidencia => score 1, mensaje 'No se han encontrado hallazgos'.
    - Si aparece el H2 'No results found...' => score 1 y ese texto como mensaje.
    - Reintenta hasta 3 veces. Si falla todo: estado 'Sin validar'.
    """
    # Carpetas/paths
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"{NOMBRE_SITIO}_{cedula}_{ts}"

    png_path_abs = os.path.join(absolute_folder, base_name + ".png")
    png_path_rel = os.path.join(relative_folder, base_name + ".png").replace("\\", "/")

    # (opcional) PDF de respaldo
    pdf_path_abs = os.path.join(absolute_folder, base_name + ".pdf")

    intentos = 0
    success = False

    mensaje = ""
    estado = "Sin validar"
    score = 0
    archivo = ""  # este será el PNG (screenshot)

    name_norm = _norm(nombre)

    while intentos < 3 and not success:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    locale="en-GB",
                    viewport={"width": 1440, "height": 1200}
                )
                page = await context.new_page()

                # 1) Abrir sitio
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                # 2) Buscar
                await page.locator("#txtSearch").wait_for(state="visible", timeout=15000)
                await page.fill("#txtSearch", nombre or "")
                await page.click("#btnSearch")

                # 3) Esperar algo: o la tabla con filas o el H2 de "No results..."
                #    Damos un tiempo generoso porque es Angular.
                #    Intentamos en ciclo hasta 15s.
                found_anything = False
                for _ in range(15):
                    # ¿Mensaje 'No results'?
                    nores = page.locator("h2:has-text('No results found')")
                    if await nores.count():
                        found_anything = True
                        break
                    # ¿Filas en tabla?
                    rows = page.locator("table#table tbody tr")
                    if await rows.count() > 0:
                        found_anything = True
                        break
                    await page.wait_for_timeout(1000)

                # 4) Tomar screenshot SIEMPRE
                try:
                    await page.screenshot(path=png_path_abs, full_page=True)
                    archivo = png_path_rel  # el archivo reportado será el screen
                except Exception:
                    archivo = ""  # si falla, se queda vacío (pero raro)

                # 5) (Opcional) PDF de respaldo
                try:
                    await page.emulate_media(media="print")
                except Exception:
                    pass
                try:
                    await page.pdf(
                        path=pdf_path_abs,
                        format="A4",
                        print_background=True,
                        margin={"top": "10mm", "right": "10mm", "bottom": "10mm", "left": "10mm"},
                    )
                except Exception:
                    pass  # si falla el PDF no bloquea

                # 6) Lógica de resultados
                #    a) Mensaje explícito de "No results found..."
                nores_h2 = page.locator("h2:has-text('No results found')")
                if await nores_h2.count():
                    txt = (await nores_h2.first.inner_text() or "").strip()
                    mensaje = txt or "No results found. Please try a different search term or increase the fuzzy distance of your search."
                    score = 1
                    estado = "Validado"
                else:
                    #    b) Leer filas de la tabla
                    rows = page.locator("table#table tbody tr")
                    nrows = await rows.count()
                    if nrows == 0:
                        # Nada reconocible; tratamos como sin hallazgos
                        mensaje = "No se han encontrado hallazgos"
                        score = 1
                        estado = "Validado"
                    else:
                        # Extraer nombres de la columna Name (2da columna, <td> con <a>)
                        # Comparamos exacto (normalizado como arriba)
                        exact_match = False
                        try:
                            # Extraer textos de todos los <td:nth-child(2) a> (en cada fila)
                            names = await page.evaluate("""
                                () => Array.from(document.querySelectorAll("table#table tbody tr td:nth-child(2)"))
                                          .map(td => (td.innerText || "").trim())
                            """)
                        except Exception:
                            names = []

                        for nm in names:
                            if _norm(nm) == name_norm:
                                exact_match = True
                                break

                        if exact_match:
                            mensaje = "Se ha encontrado un hallazgo"
                            score = 5
                            estado = "Validado"
                        else:
                            mensaje = "No se han encontrado hallazgos"
                            score = 1
                            estado = "Validado"

                await browser.close()
                success = True

        except Exception as e:
            intentos += 1
            mensaje = f"Error en intento {intentos}: {e}"
            # Intentar dejar evidencia incluso en error
            try:
                if 'page' in locals():
                    await page.screenshot(path=png_path_abs, full_page=True)
                    archivo = png_path_rel
            except Exception:
                pass
            if intentos >= 3:
                estado = "Sin validar"

    # Guardar en la BD
    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=score,
        estado=estado,
        mensaje=mensaje,
        archivo=archivo  # <- el screenshot full-page
    )
