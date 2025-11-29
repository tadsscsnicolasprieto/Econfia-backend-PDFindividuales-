# bots/moci_qatar_search.py
import os, re, urllib.parse, unicodedata, asyncio
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "moci_qatar_search"  
URL_SEARCH   = "https://www.moci.gov.qa/en/?s={q}"
GOTO_TIMEOUT_MS = 180_000

# Selectores (WordPress típico)
SEL_NORES_H1   = "h1.page-title"                  # "Nothing Found"
SEL_RESULTS_CT = "main, #main, .site-main"        # contenedor general
SEL_ARTICLE    = "article[id^='post-']"           # cada resultado
SEL_TITLE_A    = f"{SEL_ARTICLE} h2 a, {SEL_ARTICLE} .entry-title a"

def _norm(s: str) -> str:
    """Normaliza: trim, lower, sin tildes, comprime espacios."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

async def consultar_moci_qatar_search(consulta_id: int, nombre: str, apellido: str):
    """Consulta con hasta 3 intentos la fuente moci_qatar_search.

    Mantiene evidencia (screenshot) del último intento exitoso o fallido.
    """
    navegador = None
    full_name = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=1,
            estado="Sin Validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo=""
        )
        return

    if not full_name:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj, score=1,
            estado="Sin Validar",
            mensaje="Nombre y/o apellido vacíos para la consulta.",
            archivo=""
        )
        return

    # 2) Carpeta / archivo
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^\w\.-]+", "_", full_name)
    png_name = f"{NOMBRE_SITIO}_{safe_name}_{ts}.png"
    absolute_png = os.path.join(absolute_folder, png_name)
    relative_png = os.path.join(relative_folder, png_name).replace("\\", "/")

    mensaje_final = "No hay coincidencias."
    score_final = 1
    success = False
    last_error = None

    norm_query = _norm(full_name)

    for intento in range(3):
        try:
            async with async_playwright() as p:
                navegador = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                # Contexto más realista: UA y headers típicos
                context = await navegador.new_context(
                    viewport={"width": 1400, "height": 900},
                    locale="en-US",
                    timezone_id="America/Bogota",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    extra_http_headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9,es-ES;q=0.8,es;q=0.7",
                        "Cache-Control": "max-age=0",
                        "Connection": "keep-alive",
                        "Upgrade-Insecure-Requests": "1",
                    }
                )
                # Evitar detección básica de webdriver
                await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                page = await context.new_page()

                # 3) Ir a la URL de búsqueda
                q = urllib.parse.quote_plus(full_name)
                search_url = URL_SEARCH.format(q=q)
                # Previsitar la homepage puede ayudar a establecer cookies de sesión
                try:
                    await page.goto("https://www.moci.gov.qa/en/", timeout=60_000)
                    await asyncio.sleep(1.0)
                except Exception:
                    pass
                await page.goto(search_url, timeout=GOTO_TIMEOUT_MS)
                await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=30_000)
                except Exception:
                    pass

                # Detección temprana de bloqueo WAF
                try:
                    content = await page.content()
                except Exception:
                    content = ""
                if "Web Page Blocked" in content or "Web Page Blocked!" in content:
                    try:
                        await page.screenshot(path=absolute_png, full_page=True)
                        relative_png = relative_png or (os.path.join(relative_folder, png_name).replace("\\", "/"))
                    except Exception:
                        pass
                    # Extraer Attack ID / Message ID si visibles
                    try:
                        import re as _re
                        attack = _re.search(r"Attack ID:\s*(\d+)", content)
                        message = _re.search(r"Message ID:\s*(\d+)", content)
                        extra = []
                        if attack:
                            extra.append(f"AttackID={attack.group(1)}")
                        if message:
                            extra.append(f"MessageID={message.group(1)}")
                        extra_txt = (" "+" ".join(extra)) if extra else ""
                    except Exception:
                        extra_txt = ""
                    mensaje_final = f"Acceso bloqueado por el sitio (WAF).{extra_txt}"
                    score_final = 1
                    success = False
                    last_error = "Bloqueo WAF"
                    # Evitar continuar el flujo normal en este intento
                    raise Exception("WAF Blocked")

                # 4) Detectar "Nothing Found"
                nores_h1 = page.locator(SEL_NORES_H1, has_text="Nothing Found")
                if await nores_h1.count() > 0 and await nores_h1.first.is_visible():
                    try:
                        mensaje_final = (await nores_h1.first.inner_text()).strip() or "Nothing Found"
                    except Exception:
                        mensaje_final = "Nothing Found"
                    try:
                        await page.screenshot(path=absolute_png, full_page=True)
                        relative_png = relative_png or (os.path.join(relative_folder, png_name).replace("\\", "/"))
                    except Exception:
                        pass
                    success = True
                    score_final = 1
                else:
                    # 5) Iterar resultados
                    articles = page.locator(SEL_ARTICLE)
                    n = await articles.count()
                    exact_hit = False
                    for i in range(n):
                        art = articles.nth(i)
                        try:
                            title = (await art.locator(SEL_TITLE_A).first.inner_text(timeout=3_000)).strip()
                        except Exception:
                            title = ""
                        if title and _norm(title) == norm_query:
                            exact_hit = True
                            break
                    if exact_hit:
                        score_final = 5
                        mensaje_final = f"Coincidencia exacta con el nombre buscado: '{full_name}'."
                    else:
                        score_final = 1
                        mensaje_final = "Se encontraron resultados, pero sin coincidencia exacta del nombre."
                    try:
                        await page.screenshot(path=absolute_png, full_page=True)
                        relative_png = relative_png or (os.path.join(relative_folder, png_name).replace("\\", "/"))
                    except Exception:
                        pass
                    success = True

                # cierre explícito del navegador por intento
                try:
                    await navegador.close()
                except Exception:
                    pass
                navegador = None

            # Persistencia y salida si éxito
            if success:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj,
                    score=score_final,
                    estado="Validada",
                    mensaje=mensaje_final,
                    archivo=relative_png or ""
                )
                break
        except Exception as e:
            last_error = str(e)
            try:
                if navegador is not None:
                    await navegador.close()
            except Exception:
                pass
            navegador = None
            # si es último intento registrar falla definitiva
            if intento == 2:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id, fuente=fuente_obj,
                    score=1, estado="Sin Validar",
                    mensaje=last_error or "No fue posible obtener resultados.",
                    archivo=relative_png or ""
                )
        # pequeño delay entre intentos fallidos
        if not success and intento < 2:
            # no bloquear demasiado, pero dar tiempo a recuperar
            await asyncio.sleep(1.5)

    # Si tras el bucle no hubo éxito y no se registró (p.e. error sin excepción final)
    if not success and last_error and relative_png is None:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=fuente_obj,
            score=1, estado="Sin Validar",
            mensaje=last_error,
            archivo=""
        )
