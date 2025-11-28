# bots/moci_qatar_search.py
import os, re, urllib.parse, unicodedata
from datetime import datetime

from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

NOMBRE_SITIO = "moci_qatar_search"
URL_SEARCH   = "https://www.moci.gov.qa/en/?s={q}"
GOTO_TIMEOUT_MS = 180_000

# Selectores
SEL_NORES_H1   = "h1.page-title"
SEL_RESULTS_CT = "main, #main, .site-main"
SEL_ARTICLE    = "article[id^='post-']"
SEL_TITLE_A    = f"{SEL_ARTICLE} h2 a, {SEL_ARTICLE} .entry-title a"

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s


async def consultar_moci_qatar_search(consulta_id: int, nombre: str, apellido: str):
    navegador = None
    full_name = f"{(nombre or '').strip()} {(apellido or '').strip()}".strip()

    # 1) Validación de fuente
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

    norm_query = _norm(full_name)

    try:
        async with async_playwright() as p:
            # --- 3) Lanzar navegador con fingerprint humano ---
            navegador = await p.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-gpu",
                    "--no-sandbox",
                ],
                proxy={
                    "server": "http://USER:PASS@IP_PROXY:PUERTO"  # CAMBIAR AQUÍ
                }
            )

            context = await navegador.new_context(
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                timezone_id="Asia/Qatar",
                java_script_enabled=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.google.com/",
                    "DNT": "1",
                }
            )

            page = await context.new_page()

            # Anti-bot - eliminar señales de automatización
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            """)

            # Construir URL real
            q = urllib.parse.quote_plus(full_name)
            search_url = URL_SEARCH.format(q=q)

            # --- 4) Navegar una sola vez ---
            try:
                await page.goto(search_url, timeout=180000, wait_until="domcontentloaded")
                await page.wait_for_load_state("networkidle", timeout=60000)
            except:
                pass

            # Guardar HTML recibido para depuración
            html_path = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{safe_name}_{ts}.html")
            try:
                html_content = await page.content()
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html_content)
            except Exception as e:
                print("No se pudo guardar el HTML:", e)

            # Tiempo anti-WAF (muy importante)
            await page.wait_for_timeout(4000)

            # --- 5) Lógica de resultados ---
            nores_h1 = page.locator(SEL_NORES_H1, has_text="Nothing Found")

            if await nores_h1.count() > 0 and await nores_h1.first.is_visible():
                mensaje_final = (await nores_h1.first.inner_text()).strip()
                score_final = 1
                success = True

            else:
                articles = page.locator(SEL_ARTICLE)
                n = await articles.count()
                exact_hit = False

                for i in range(n):
                    art = articles.nth(i)
                    try:
                        title = (await art.locator(SEL_TITLE_A).first.inner_text(timeout=3000)).strip()
                    except:
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

                success = True

            # --- 6) Screenshot final ---
            try:
                await page.screenshot(path=absolute_png, full_page=True)
            except:
                pass

            try:
                await navegador.close()
            except:
                pass

        # --- 7) Guardar resultado ---
        if success:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=score_final,
                estado="Validada",
                mensaje=mensaje_final,
                archivo=relative_png
            )
        else:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1, estado="Sin Validar",
                mensaje="No fue posible obtener resultados.",
                archivo=relative_png
            )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id, fuente=fuente_obj,
                score=1, estado="Sin Validar",
                mensaje=str(e), archivo=""
            )
        finally:
            try:
                if navegador is not None:
                    await navegador.close()
            except:
                pass
