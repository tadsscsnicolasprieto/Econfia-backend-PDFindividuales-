#!/usr/bin/env python3
# run_medicaldevices.py
"""
Script único que contiene:
- la función `consultar_medical_devices(...)` (Playwright + diagnóstico + guardado)
- un CLI mínimo para invocar la función desde línea de comandos

Comportamiento:
- Normaliza el flag headless a booleano.
- Reintentos en navegación y espera de networkidle.
- Intenta aceptar el modal de términos.
- Oculta elementos visuales molestos antes de la captura.
- Si hay resultados, abre el primer resultado (click) y toma captura de la página completa.
- No guarda HTML de depuración; solo screenshots.
- No imprime logs en consola.
"""

import os
import urllib.parse
import asyncio
import argparse
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

# -------------------------
# Config / utilitarios
# -------------------------
NOMBRE_SITIO = "medicaldevices"

def _safe_filename(s: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in (s or "")).strip("_")

async def _goto_with_retries(page, url, attempts=3, base_delay=1.0, timeout=120000):
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return resp
        except Exception as e:
            last_exc = e
            if i < attempts:
                await asyncio.sleep(base_delay * (2 ** (i - 1)))
    raise last_exc

async def _wait_for_networkidle_with_retries(page, retries=3, base_delay=1.0, timeout=15000):
    for attempt in range(1, retries + 1):
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
            return True
        except Exception:
            if attempt < retries:
                await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
    return False

async def _save_screenshot_only(page, folder, prefix, full_page=True):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_path = os.path.join(folder, f"{prefix}_{ts}.png")
    try:
        await page.screenshot(path=png_path, full_page=full_page)
    except Exception:
        png_path = ""
    return png_path

# -------------------------
# Función principal
# -------------------------
async def consultar_medical_devices(consulta_id: int, nombre_empresa: str, headless=True):
    # Normalizar headless a booleano
    if isinstance(headless, str):
        headless = headless.strip().lower() in ("1", "true", "yes", "y", "t")
    else:
        headless = bool(headless)

    navegador = None
    fuente_obj = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    nombre_empresa = (nombre_empresa or "").strip()
    nombre_busqueda = urllib.parse.quote_plus(nombre_empresa) if nombre_empresa else ""
    search_url = f"https://medicaldevices.icij.org/search?q%5Bdisplay_cont%5D={nombre_busqueda}"

    # 2) Carpetas
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = _safe_filename(nombre_empresa or "consulta")
    screenshot_name = f"{NOMBRE_SITIO}_{safe_name}_{ts}.png"
    abs_png = os.path.join(absolute_folder, screenshot_name)
    rel_png = os.path.join(relative_folder, screenshot_name).replace("\\", "/")

    # Selectores
    TERMS_CHECK = 'label[for="termsCheck"]'
    TERMS_BTN   = 'button.btn.btn-primary.font-weight-bold.text-uppercase.ml-2'
    TABLE_BODY  = "table.search__results tbody"
    NORES_BOX   = "div.text-center.p-3.border.border-light.rounded.text-muted"

    try:
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=headless)
            context = await navegador.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
            )
            page = await context.new_page()

            # Navegar con reintentos
            resp = None
            try:
                resp = await _goto_with_retries(page, search_url, attempts=3, base_delay=1.0, timeout=120000)
            except Exception:
                resp = None

            # Registrar status (interno)
            resp_status = None
            if resp:
                try:
                    resp_status = resp.status
                except Exception:
                    resp_status = None

            # Esperar networkidle
            await _wait_for_networkidle_with_retries(page, retries=3, base_delay=1.0, timeout=15000)

            # Intentar aceptar modal de términos (best-effort)
            try:
                if await page.locator(TERMS_CHECK).count():
                    await page.locator(TERMS_CHECK).first.click()
                    await asyncio.sleep(0.3)
                if await page.locator("button:has-text('SUBMIT'), button:has-text('Submit')").count():
                    await page.locator("button:has-text('SUBMIT'), button:has-text('Submit')").first.click()
                    await _wait_for_networkidle_with_retries(page, retries=3, base_delay=0.5, timeout=10000)
                    await asyncio.sleep(0.3)
            except Exception:
                pass

            # Esperar fuentes
            try:
                await page.evaluate("() => document.fonts.ready")
            except Exception:
                pass

            # Ocultar elementos molestos antes de la captura
            try:
                await page.evaluate("""
                () => {
                  const hide = (sel) => document.querySelectorAll(sel).forEach(e => e.style.display = 'none');
                  ['.cookie-banner', '.site-footer', '.modal-backdrop', '.ads', '.promo'].forEach(hide);
                }
                """)
                await asyncio.sleep(0.2)
            except Exception:
                pass

            # Detectar bloqueo por status o por contenido
            blocked = False
            if resp_status == 403:
                blocked = True
            else:
                try:
                    content_lower = (await page.content()).lower()
                    indicators = ["access denied", "forbidden", "error 403", "error 503", "cloudfront", "cloudflare", "request blocked"]
                    if any(ind in content_lower for ind in indicators):
                        blocked = True
                except Exception:
                    pass

            # Si hay bloqueo y estamos en headless, ejecutar headful para comparar (solo evidencia)
            if headless and blocked:
                try:
                    try:
                        await context.close()
                    except Exception:
                        pass
                    try:
                        await navegador.close()
                    except Exception:
                        pass

                    navegador = await p.chromium.launch(headless=False)
                    context = await navegador.new_context(
                        viewport={"width": 1366, "height": 768},
                        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
                    )
                    page = await context.new_page()
                    try:
                        await _goto_with_retries(page, search_url, attempts=2, base_delay=1.0, timeout=120000)
                    except Exception:
                        pass

                    await _wait_for_networkidle_with_retries(page, retries=2, base_delay=1.0, timeout=15000)

                    # Captura headful para evidencia (solo screenshot)
                    _ = await _save_screenshot_only(page, absolute_folder, "after_goto_headful", full_page=True)

                    mensaje_final = "Bloqueo detectado en modo headless. Artefactos generados para revisión humana."
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id, fuente=fuente_obj,
                        score=0, estado="Sin Validar",
                        mensaje=mensaje_final,
                        archivo=rel_png if os.path.exists(abs_png) else ""
                    )
                    try:
                        await navegador.close()
                    except Exception:
                        pass
                    return
                except Exception:
                    pass

            # Si no está bloqueado, proceder a parsear resultados
            score_final = 0
            mensaje_final = "No se encuentran coincidencias."

            # Buscar filas de resultados
            try:
                rows = page.locator(f"{TABLE_BODY} tr")
                count_rows = await rows.count()
                if count_rows > 0:
                    score_final = 10
                    mensaje_final = "con hallazgo"

                    # Intentar abrir el primer resultado para capturar la página completa
                    first_row = rows.nth(0)
                    link = None
                    try:
                        # Preferir un <a> dentro de la fila
                        if await first_row.locator("a").count():
                            link_el = first_row.locator("a").first
                            href = await link_el.get_attribute("href")
                            if href:
                                # Si el enlace es relativo, construir URL absoluta
                                if href.startswith("/"):
                                    href = urllib.parse.urljoin("https://medicaldevices.icij.org", href)
                                # Intentar navegar directamente a la URL del resultado
                                try:
                                    await _goto_with_retries(page, href, attempts=2, base_delay=0.5, timeout=120000)
                                except Exception:
                                    # fallback: intentar click si la navegación directa falla
                                    try:
                                        await link_el.click()
                                    except Exception:
                                        pass
                            else:
                                # Si no hay href, intentar click
                                try:
                                    await link_el.click()
                                except Exception:
                                    pass
                        else:
                            # Si no hay <a>, intentar hacer click en la fila
                            try:
                                await first_row.click()
                            except Exception:
                                pass
                    except Exception:
                        pass

                    # Esperar que la nueva página cargue completamente
                    await _wait_for_networkidle_with_retries(page, retries=3, base_delay=0.5, timeout=15000)
                    try:
                        await page.evaluate("() => document.fonts.ready")
                    except Exception:
                        pass

                    # Ocultar elementos molestos en la página de resultado antes de capturar
                    try:
                        await page.evaluate("""
                        () => {
                          const hide = (sel) => document.querySelectorAll(sel).forEach(e => e.style.display = 'none');
                          ['.cookie-banner', '.site-footer', '.modal-backdrop', '.ads', '.promo'].forEach(hide);
                        }
                        """)
                        await asyncio.sleep(0.2)
                    except Exception:
                        pass

                    # Captura de la página completa del resultado
                    try:
                        await page.screenshot(path=abs_png, full_page=True)
                    except Exception:
                        try:
                            await page.screenshot(path=abs_png, full_page=False)
                        except Exception:
                            pass

                else:
                    # No hay filas: buscar recuadro "No results"
                    try:
                        nores = page.locator(NORES_BOX).first
                        if await nores.count() > 0 and await nores.is_visible():
                            raw = (await nores.inner_text() or "").strip()
                            raw = " ".join(raw.split())
                            mensaje_final = "No results found" if "no results found" in raw.lower() else raw
                    except Exception:
                        pass

                    # Captura de la página completa cuando no hay resultados
                    try:
                        await page.screenshot(path=abs_png, full_page=True)
                    except Exception:
                        try:
                            await page.screenshot(path=abs_png, full_page=False)
                        except Exception:
                            pass

            except Exception:
                # En caso de error al parsear, tomar captura completa como fallback
                try:
                    await page.screenshot(path=abs_png, full_page=True)
                except Exception:
                    pass

            try:
                await navegador.close()
            except Exception:
                pass
            navegador = None

        # Guardar resultado en BD
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj,
            score=score_final, estado="Validada",
            mensaje=mensaje_final, archivo=rel_png
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=0, estado="Sin Validar",
                mensaje=str(e), archivo=""
            )
        finally:
            try:
                if navegador:
                    await navegador.close()
            except Exception:
                pass

# -------------------------
# CLI: run desde línea de comandos
# -------------------------
def str2bool(v):
    if isinstance(v, bool):
        return v
    v = str(v).strip().lower()
    if v in ('yes','true','t','y','1'):
        return True
    if v in ('no','false','f','n','0'):
        return False
    raise argparse.ArgumentTypeError('Booleano esperado.')

async def main_async(args):
    bot = args.bot.lower()
    if bot == "medicaldevices":
        await consultar_medical_devices(consulta_id=args.consulta_id, nombre_empresa=args.nombre, headless=args.headless)
    else:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=args.consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No hay implementación para el bot '{args.bot}'", archivo=""
        )

def main():
    parser = argparse.ArgumentParser(description="Ejecutar bot único (medicaldevices)")
    parser.add_argument('--bot', required=True, help='Nombre del bot (ej: medicaldevices)')
    parser.add_argument('--cedula', required=True, help='Identificador de la consulta (se usa para crear carpeta)')
    parser.add_argument('--tipo', required=False, help='Tipo (opcional)')
    parser.add_argument('--nombre', required=False, default="", help='Nombre o empresa a buscar')
    parser.add_argument('--headless', type=str2bool, nargs='?', const=True, default=True,
                        help='True/False para modo headless (acepta true/false, yes/no, 1/0).')
    args = parser.parse_args()

    try:
        consulta_id = int(args.cedula) if args.cedula.isdigit() else int(datetime.now().timestamp())
    except Exception:
        consulta_id = int(datetime.now().timestamp())

    class A: pass
    a = A()
    a.bot = args.bot
    a.consulta_id = consulta_id
    a.tipo = args.tipo
    a.nombre = args.nombre
    a.headless = args.headless

    asyncio.run(main_async(a))

if __name__ == "__main__":
    main()
