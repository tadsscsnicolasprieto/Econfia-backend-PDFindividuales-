# bots/idb_sanctioned_png.py
import os, re, base64, unicodedata
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://www.iadb.org/es/quienes-somos/transparencia/sistema-de-sanciones/empresas-e-individuos-sancionados"
NOMBRE_SITIO = "idb_sanctioned_png"


def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"


def _norm(s: str) -> str:
    """Normaliza: quita tildes, compacta espacios y pasa a minúsculas (casefold)."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s).strip()
    return s.casefold()


async def _accept_and_strip_cookies(page):
    for sel in [
        "#onetrust-accept-btn-handler",
        "button[aria-label*='Aceptar' i][id*='onetrust']",
        "button:has-text('Aceptar todas las cookies')",
        "button:has-text('Aceptar todas')",
    ]:
        try:
            await page.locator(sel).click(timeout=1500)
            await page.wait_for_timeout(200)
            break
        except Exception:
            pass
    # Quitar overlays/banners para que la captura se vea limpia
    try:
        await page.evaluate("""
          (() => {
            const sels = [
              '#onetrust-banner-sdk','#onetrust-consent-sdk',
              '.onetrust-pc-dark-filter','.ot-floating-button',
              '.ot-sdk-container','.ot-backdrop','.cookie','.cookies'
            ];
            for (const s of sels) document.querySelectorAll(s).forEach(n => n.remove());
            document.documentElement.style.overflow = 'auto';
            document.body.style.overflow = 'auto';
          })();
        """)
    except Exception:
        pass


def _fallback_png(path_abs: str, text: str = "Evidencia no disponible"):
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
        os.makedirs(os.path.dirname(path_abs), exist_ok=True)
        img = Image.new("RGB", (1200, 600), "white")
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("arial.ttf", 20)
        except Exception:
            font = ImageFont.load_default()
        draw.multiline_text((40, 40), text, fill="black", font=font, spacing=6)
        img.save(path_abs, "PNG")
        return True
    except Exception:
        try:
            os.makedirs(os.path.dirname(path_abs), exist_ok=True)
            png_1x1 = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO3+8s8AAAAASUVORK5CYII="
            )
            with open(path_abs, "wb") as f:
                f.write(png_1x1)
            return True
        except Exception:
            return False


async def consultar_idb_sanctioned_png(consulta_id: int, nombre: str):
    """
    Busca `nombre` y genera una captura PNG.
    Score:
      - 5 si el nombre aparece EXACTAMENTE (ignorando tildes/mayúsculas) dentro de
        <section class="section-content">…</section>
      - 1 si NO aparece.
    """
    navegador = None
    context = None
    page = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    nombre = (nombre or "").strip()
    if not nombre:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar", mensaje="El nombre llegó vacío.", archivo=""
        )
        return

    # 2) Rutas de salida
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    safe = _safe_name(nombre)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_png_abs = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe}_{ts}.png")
    out_png_rel = os.path.join(relative_folder, os.path.basename(out_png_abs)).replace("\\", "/")

    # Valores por defecto (se sustituyen tras analizar las secciones)
    score_final = 1
    mensaje_final = "Sin coincidencia exacta en resultados"

    try:
        # 3) Navegación y búsqueda
        async with async_playwright() as p:
            navegador = await p.chromium.launch(headless=True)
            context = await navegador.new_context(
                viewport={"width": 1440, "height": 1200},
                locale="es-ES",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"),
            )
            page = await context.new_page()

            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            await _accept_and_strip_cookies(page)

            # Ir a /es/search (si hay enlace visible); si no, navegar directo
            went = False
            for sel in ["a[slot='search']", "a[href*='/es/search']",
                        "a[aria-label*='Buscar' i]", "a[title*='Buscar' i]"]:
                try:
                    await page.locator(sel).first.click(timeout=4000)
                    went = True
                    break
                except Exception:
                    continue
            if not went:
                await page.goto("https://www.iadb.org/es/search", wait_until="domcontentloaded")

            try:
                await page.wait_for_url("**/es/search**", timeout=20000)
            except Exception:
                pass

            await _accept_and_strip_cookies(page)

            # Rellenar y enviar
            await page.wait_for_selector("input#edit-query--3, input[id^='edit-query']", timeout=12000)
            search_input = page.locator("input#edit-query--3, input[id^='edit-query']").first
            await search_input.fill(nombre)
            await page.keyboard.press("Enter")

            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(1200)

            # ====== NUEVO: coincidencia EXACTA solo en <section.section-content> ======
            norm_name = _norm(nombre)
            norm_blob = ""

            try:
                # Espera a que exista al menos una sección de contenido (si la hay)
                try:
                    await page.wait_for_selector("section.section-content", timeout=15000)
                except Exception:
                    pass

                sections = page.locator("section.section-content")
                count = await sections.count()
                texts = []
                # Leemos varias secciones (por si hay más de una); límite razonable
                for i in range(min(count, 8)):
                    try:
                        t = await sections.nth(i).inner_text()
                        if t:
                            texts.append(t)
                    except Exception:
                        continue
                norm_blob = _norm(" ".join(texts))
            except Exception:
                norm_blob = ""

            if norm_blob:
                # palabra/expresión exacta (ignorando tildes y mayúsculas)
                pattern = r"(?<!\w)" + re.escape(norm_name) + r"(?!\w)"
                if re.search(pattern, norm_blob):
                    score_final = 5
                    mensaje_final = "Coincidencia exacta en resultados"
                else:
                    score_final = 1
                    mensaje_final = "Sin coincidencia exacta en resultados"
            else:
                # Si no pudimos leer secciones, mantenemos score=1 por defecto
                score_final = 1
                mensaje_final = "Sin coincidencia exacta en resultados"

            # Captura completa
            try:
                await page.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass
            try:
                await page.screenshot(path=out_png_abs, full_page=True)
            except Exception:
                _fallback_png(out_png_abs, "BID – evidencia no disponible (captura fallida).")

            await context.close()
            await navegador.close()
            navegador = None
            context = None

        if not os.path.exists(out_png_abs) or os.path.getsize(out_png_abs) < 500:
            _fallback_png(out_png_abs, f"BID – evidencia de búsqueda: {nombre}")

        # 4) Registrar
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=score_final,
            estado="Validada", mensaje=mensaje_final, archivo=out_png_rel
        )

    except Exception as e:
        try:
            if not os.path.exists(out_png_abs):
                _fallback_png(out_png_abs, f"BID – error: {e}")
        except Exception:
            pass
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if navegador is not None:
                await navegador.close()
        except Exception:
            pass

        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=0,
            estado="Sin Validar",
            mensaje="No fue posible consultar la fuente del BID. Se adjunta evidencia.",
            archivo=(os.path.join("resultados", str(consulta_id), os.path.basename(out_png_abs)).replace("\\", "/")
                     if os.path.exists(out_png_abs) else "")
        )
