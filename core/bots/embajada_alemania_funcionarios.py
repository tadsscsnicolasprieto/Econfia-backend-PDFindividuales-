# core/bots/embajada_alemania_funcionarios.py
"""
Bot para consultar la página de funcionarios de la Embajada de Alemania
con soporte para:
 - detección de reCAPTCHA v2/v3 y Cloudflare Turnstile
 - integración con CapSolver (o proveedor compatible) con proxy en la tarea
 - inyección de token y submit forzado
 - perfil persistente (launch_persistent_context) y storageState opcional
 - fingerprint hardening, movimientos humanos y logging detallado
Variables de entorno:
 - EMB_HEADLESS (true|false)
 - EMB_STORAGE_STATE (ruta opcional a storageState.json)
 - EMB_PROXY (opcional, e.g. http://user:pass@host:port)
 - EMB_USER_AGENT
 - EMB_CAPTCHA_SOLVER (true|false)
 - CAPSOLVER_API_KEY
 - CAPSOLVER_API_URL (por defecto https://api.capsolver.com)
 - EMB_HUMAN_IN_LOOP (true|false)
 - EMB_SOLVER_TIMEOUT (segundos)
 - EMB_CHROME_PATH (opcional)
"""

import os
import re
import json
import time
import asyncio
import logging
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import httpx
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, BrowserContext, Page

from core.models import Resultado, Fuente

# -------------------------
# Configuración
# -------------------------
NOMBRE_SITIO = "embajada_alemania_funcionarios"
URL = "https://alemania.embajada.gov.co/acerca/funcionarios"

SEL_INPUT_VISIBLE = "#edit-keys:visible"
SEL_INPUT_FALLBACK = "main input#edit-keys.form-search:visible, main input[name='keys'].form-search:visible"

WAIT_NAV_MS = 20000
WAIT_POST_MS = 3000

HEADLESS = os.getenv("EMB_HEADLESS", "false").lower() == "true"
STORAGE_STATE = os.getenv("EMB_STORAGE_STATE")
PROXY = os.getenv("EMB_PROXY")
USER_AGENT = os.getenv(
    "EMB_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
)
USE_CAPTCHA_SOLVER = os.getenv("EMB_CAPTCHA_SOLVER", "true").lower() == "true"
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY")
CAPSOLVER_API_URL = os.getenv("CAPSOLVER_API_URL", "https://api.capsolver.com")
HUMAN_IN_LOOP = os.getenv("EMB_HUMAN_IN_LOOP", "false").lower() == "true"
SOLVER_TIMEOUT = int(os.getenv("EMB_SOLVER_TIMEOUT", "180"))
CHROME_EXECUTABLE = os.getenv("EMB_CHROME_PATH")

# logging
LOG_DIR = os.path.join(settings.BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("embajada_alemania_bot")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(os.path.join(LOG_DIR, "embajada_alemania_bot.log"))
fh.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
fh.setFormatter(formatter)
ch.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(fh)
    logger.addHandler(ch)

# -------------------------
# Helpers DB
# -------------------------
@sync_to_async
def _get_fuente(nombre):
    return Fuente.objects.filter(nombre=nombre).first()

@sync_to_async
def _crear_resultado(consulta_id, fuente, score, estado, mensaje, archivo):
    return Resultado.objects.create(
        consulta_id=consulta_id,
        fuente=fuente,
        score=score,
        estado=estado,
        mensaje=mensaje,
        archivo=archivo,
    )

# -------------------------
# Utilidades
# -------------------------
def _is_cloudflare_challenge(body_text: str) -> bool:
    if not body_text:
        return False
    low = body_text.lower()
    checks = [
        "verifique que usted es un ser humano",
        "verifica que eres un ser humano",
        "verify that you are a human",
        "checking your browser",
        "cloudflare",
        "verificando",
    ]
    return any(c in low for c in checks)

async def _save_screenshot(page: Page, path: str):
    try:
        await page.screenshot(path=path, full_page=True)
    except Exception:
        try:
            await page.screenshot(path=path)
        except Exception:
            try:
                open(path, "wb").close()
            except Exception:
                pass

async def _human_like_movements(page: Page):
    try:
        await page.mouse.move(100, 100)
        await asyncio.sleep(0.2)
        await page.mouse.move(200, 200, steps=6)
        await asyncio.sleep(0.15)
        await page.mouse.move(300, 250, steps=4)
        await asyncio.sleep(0.2)
    except Exception:
        pass

async def _get_public_ip(timeout: int = 10) -> str:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get("https://ifconfig.me")
            if r.status_code == 200:
                return r.text.strip()
    except Exception:
        pass
    return ""

def _extract_ray_id(body_text: str) -> str:
    if not body_text:
        return ""
    m = re.search(r"Ray ID[:\s]*([A-Za-z0-9]+)", body_text)
    if m:
        return m.group(1)
    m2 = re.search(r"ray id[:\s]*([A-Za-z0-9]+)", body_text, re.IGNORECASE)
    if m2:
        return m2.group(1)
    return ""

# -------------------------
# CapSolver integration
# -------------------------
async def _capsolver_create_task(payload: dict) -> dict:
    if not CAPSOLVER_API_KEY:
        logger.warning("No CAPSOLVER_API_KEY configurada")
        return {}
    url = CAPSOLVER_API_URL.rstrip("/") + "/createTask"
    body = {"clientKey": CAPSOLVER_API_KEY, **payload}
    logger.info("Enviando createTask a CapSolver: %s", json.dumps(payload, default=str))
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(url, json=body)
            logger.info("createTask status: %s", r.status_code)
            try:
                j = r.json()
                logger.info("createTask response: %s", json.dumps(j, default=str))
                return j
            except Exception:
                logger.exception("No se pudo parsear JSON de createTask")
                return {}
        except Exception as e:
            logger.exception("Error en createTask: %s", e)
            return {}

async def _capsolver_get_result(task_id: str, timeout: int = SOLVER_TIMEOUT) -> dict:
    if not CAPSOLVER_API_KEY:
        return {}
    url = CAPSOLVER_API_URL.rstrip("/") + "/getTaskResult"
    body = {"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}
    logger.info("Iniciando polling getTaskResult para taskId=%s", task_id)
    async with httpx.AsyncClient(timeout=30) as client:
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = await client.post(url, json=body)
                logger.info("getTaskResult status: %s", r.status_code)
                try:
                    j = r.json()
                    logger.info("getTaskResult response: %s", json.dumps(j, default=str))
                    if j.get("errorId") == 0 and j.get("status") == "ready":
                        return j
                except Exception:
                    logger.exception("No se pudo parsear JSON de getTaskResult")
            except Exception as e:
                logger.exception("Error en getTaskResult: %s", e)
            await asyncio.sleep(5)
    logger.warning("Timeout esperando resultado del solver para taskId=%s", task_id)
    return {}

def _capsolver_proxy_payload(proxy: str):
    if not proxy:
        return {}
    parsed = proxy.replace("http://", "").replace("https://", "")
    auth = None
    hostport = parsed
    if "@" in parsed:
        auth, hostport = parsed.split("@", 1)
    if ":" in hostport:
        host, port = hostport.split(":", 1)
    else:
        host, port = hostport, ""
    proxy_payload = {
        "proxyType": "http",
        "proxyAddress": host,
        "proxyPort": int(port) if port.isdigit() else 0,
    }
    if auth and ":" in auth:
        user, pwd = auth.split(":", 1)
        proxy_payload["proxyLogin"] = user
        proxy_payload["proxyPassword"] = pwd
    return proxy_payload

async def resolver_capsolver_recaptcha_v2(page_url: str, sitekey: str, invisible: bool = False, timeout: int = SOLVER_TIMEOUT) -> str:
    if not CAPSOLVER_API_KEY:
        return ""
    task = {
        "task": {
            "type": "RecaptchaV2TaskProxyless",
            "websiteURL": page_url,
            "websiteKey": sitekey,
            "isInvisible": bool(invisible),
        }
    }
    proxy_payload = _capsolver_proxy_payload(PROXY)
    if proxy_payload:
        task["task"]["type"] = "RecaptchaV2Task"
        task["task"].update(proxy_payload)
    create = await _capsolver_create_task(task)
    task_id = create.get("taskId") or create.get("id")
    logger.info("CapSolver createTask id: %s", task_id)
    if not task_id:
        return ""
    result = await _capsolver_get_result(task_id, timeout=timeout)
    if not result:
        return ""
    sol = result.get("solution") or {}
    token = sol.get("gRecaptchaResponse") or sol.get("token") or ""
    logger.info("Token recibido (len): %s", len(token) if token else 0)
    return token or ""

async def resolver_capsolver_recaptcha_v3(page_url: str, sitekey: str, action: str = None, timeout: int = SOLVER_TIMEOUT) -> str:
    if not CAPSOLVER_API_KEY:
        return ""
    task = {
        "task": {
            "type": "RecaptchaV3TaskProxyless",
            "websiteURL": page_url,
            "websiteKey": sitekey,
        }
    }
    proxy_payload = _capsolver_proxy_payload(PROXY)
    if proxy_payload:
        task["task"]["type"] = "RecaptchaV3Task"
        task["task"].update(proxy_payload)
    if action:
        task["task"]["pageAction"] = action
    create = await _capsolver_create_task(task)
    task_id = create.get("taskId") or create.get("id")
    logger.info("CapSolver createTask v3 id: %s", task_id)
    if not task_id:
        return ""
    result = await _capsolver_get_result(task_id, timeout=timeout)
    if not result:
        return ""
    sol = result.get("solution") or {}
    token = sol.get("gRecaptchaResponse") or sol.get("token") or ""
    logger.info("Token v3 recibido (len): %s", len(token) if token else 0)
    return token or ""

# -------------------------
# Turnstile (Cloudflare) support
# -------------------------
async def _detect_turnstile(page: Page):
    try:
        el = await page.query_selector("div[data-sitekey].cf-turnstile, div.cf-turnstile[data-sitekey], div[data-sitekey][class*='turnstile']")
        if el:
            sitekey = await el.get_attribute("data-sitekey")
            return {"type": "turnstile", "sitekey": sitekey, "action": None}
        iframe = await page.query_selector("iframe[src*='turnstile'], iframe[src*='cloudflare']")
        if iframe:
            src = await iframe.get_attribute("src") or ""
            q = parse_qs(urlparse(src).query)
            sitekey = (q.get("k") or q.get("sitekey") or [None])[0]
            return {"type": "turnstile", "sitekey": sitekey, "action": None}
        scripts = await page.query_selector_all("script")
        for s in scripts:
            try:
                text = await s.inner_text()
                if text and "turnstile" in text and "sitekey" in text:
                    m = re.search(r"sitekey\s*[:=]\s*['\"]([^'\"]+)['\"]", text)
                    if m:
                        return {"type": "turnstile", "sitekey": m.group(1), "action": None}
            except Exception:
                continue
        return None
    except Exception:
        return None

async def resolver_capsolver_turnstile(page_url: str, sitekey: str, timeout: int = SOLVER_TIMEOUT) -> str:
    if not CAPSOLVER_API_KEY:
        logger.warning("No CAPSOLVER_API_KEY configurada para Turnstile")
        return ""
    task = {
        "task": {
            "type": "TurnstileTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": sitekey,
        }
    }
    proxy_payload = _capsolver_proxy_payload(PROXY)
    if proxy_payload:
        task["task"]["type"] = "TurnstileTask"
        task["task"].update(proxy_payload)
    create = await _capsolver_create_task(task)
    logger.info("createTask Turnstile response: %s", json.dumps(create, default=str))
    task_id = create.get("taskId") or create.get("id")
    if not task_id:
        logger.warning("No se obtuvo taskId para Turnstile")
        return ""
    result = await _capsolver_get_result(task_id, timeout=timeout)
    logger.info("getTaskResult Turnstile: %s", json.dumps(result, default=str))
    if not result:
        return ""
    sol = result.get("solution") or {}
    token = sol.get("gRecaptchaResponse") or sol.get("token") or sol.get("response") or ""
    return token or ""

async def _inject_turnstile_token(page: Page, token: str):
    try:
        await page.evaluate(
            """(token) => {
                let ta = document.querySelector('textarea#g-recaptcha-response');
                if (!ta) {
                    ta = document.createElement('textarea');
                    ta.id = 'g-recaptcha-response';
                    ta.name = 'g-recaptcha-response';
                    ta.style.display = 'none';
                    document.body.appendChild(ta);
                }
                ta.value = token;
                ta.dispatchEvent(new Event('input', {bubbles:true}));
                ta.dispatchEvent(new Event('change', {bubbles:true}));

                let ta2 = document.querySelector('textarea#cf-turnstile-response');
                if (!ta2) {
                    ta2 = document.createElement('textarea');
                    ta2.id = 'cf-turnstile-response';
                    ta2.name = 'cf-turnstile-response';
                    ta2.style.display = 'none';
                    document.body.appendChild(ta2);
                }
                ta2.value = token;
                ta2.dispatchEvent(new Event('input', {bubbles:true}));
                ta2.dispatchEvent(new Event('change', {bubbles:true}));
            }""",
            token,
        )
        await asyncio.sleep(0.6)
        return True
    except Exception:
        logger.exception("inject_turnstile_token failed")
        return False

# -------------------------
# reCAPTCHA detection & injection
# -------------------------
async def _detect_recaptcha(page: Page):
    try:
        el = await page.query_selector("div.g-recaptcha[data-sitekey], div.recaptcha[data-sitekey]")
        if el:
            sitekey = await el.get_attribute("data-sitekey")
            cls = await el.get_attribute("class") or ""
            size = await el.get_attribute("data-size") or ""
            if "invisible" in cls.lower() or size.lower() == "invisible":
                return {"type": "v2_invisible", "sitekey": sitekey, "action": None}
            return {"type": "v2", "sitekey": sitekey, "action": None}
        iframe = await page.query_selector("iframe[src*='recaptcha/api2/anchor']")
        if iframe:
            src = await iframe.get_attribute("src")
            if src:
                q = parse_qs(urlparse(src).query)
                sitekey = (q.get("k") or [None])[0]
                return {"type": "v2", "sitekey": sitekey, "action": None}
        scripts = await page.query_selector_all("script")
        for s in scripts:
            try:
                src = await s.get_attribute("src")
                if src and "recaptcha/api.js" in src and "render=" in src:
                    q = parse_qs(urlparse(src).query)
                    sitekey = (q.get("render") or [None])[0]
                    return {"type": "v3", "sitekey": sitekey, "action": None}
                text = await s.inner_text()
                if "grecaptcha.execute" in (text or ""):
                    m = re.search(r"grecaptcha\.execute\(\s*['\"]([^'\"]+)['\"]\s*,\s*\{?\s*action\s*:\s*['\"]([^'\"]+)['\"]", text or "")
                    if m:
                        return {"type": "v3", "sitekey": m.group(1), "action": m.group(2)}
            except Exception:
                continue
        return None
    except Exception:
        return None

async def _inject_token_v2(page: Page, token: str):
    try:
        await page.evaluate(
            """(token) => {
                const form = document.querySelector('form') || document;
                let ta = form.querySelector('textarea#g-recaptcha-response');
                if (!ta) {
                    ta = document.createElement('textarea');
                    ta.id = 'g-recaptcha-response';
                    ta.name = 'g-recaptcha-response';
                    ta.style.display = 'none';
                    form.appendChild(ta);
                }
                ta.value = token;
                ta.dispatchEvent(new Event('input', {bubbles:true}));
                ta.dispatchEvent(new Event('change', {bubbles:true}));
            }""",
            token,
        )
        await asyncio.sleep(0.6)
        return True
    except Exception:
        logger.exception("inject v2 failed")
        return False

async def _inject_token_v3(page: Page, token: str):
    try:
        await page.evaluate(
            """(token) => {
                const form = document.querySelector('form') || document;
                let ta = form.querySelector('textarea#g-recaptcha-response');
                if (!ta) {
                    ta = document.createElement('textarea');
                    ta.id = 'g-recaptcha-response';
                    ta.name = 'g-recaptcha-response';
                    ta.style.display = 'none';
                    form.appendChild(ta);
                }
                ta.value = token;
                ta.dispatchEvent(new Event('input', {bubbles:true}));
                ta.dispatchEvent(new Event('change', {bubbles:true}));
            }""",
            token,
        )
        await asyncio.sleep(0.6)
        return True
    except Exception:
        logger.exception("inject v3 failed")
        return False

async def _try_click_checkbox_in_iframe(page: Page, timeout=8000):
    try:
        iframe_el = await page.query_selector("iframe[src*='recaptcha'], iframe[src*='turnstile']")
        if iframe_el:
            frame = await iframe_el.content_frame()
            if frame:
                for sel in ["#recaptcha-anchor", ".recaptcha-checkbox-border", ".recaptcha-checkbox", ".cf-turnstile-checkbox"]:
                    try:
                        el = frame.locator(sel).first
                        if await el.count() > 0 and await el.is_visible():
                            await el.click(timeout=3000)
                            await asyncio.sleep(0.8)
                            return True
                    except Exception:
                        pass
        cb = page.locator("div.recaptcha-checkbox-border, #recaptcha-anchor, .cf-turnstile-checkbox, input[type='checkbox']")
        if await cb.count() > 0:
            await cb.first.click(timeout=3000)
            await asyncio.sleep(0.8)
            return True
    except Exception:
        logger.exception("click checkbox failed")
    return False

# -------------------------
# Diagnóstico extendido
# -------------------------
async def _dump_html_and_iframes(page: Page, absolute_folder: str, safe_query: str, ts: str):
    try:
        html = await page.content()
        diag_html_path = os.path.join(absolute_folder, f"diagnostic_{safe_query}_{ts}.html")
        with open(diag_html_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("HTML volcado a: %s", diag_html_path)

        iframe_info = []
        for f in page.frames:
            try:
                src = f.url or ""
                iframe_info.append(src)
            except Exception:
                pass
        logger.info("Iframes detectados (count=%s): %s", len(iframe_info), iframe_info)
        with open(os.path.join(absolute_folder, f"iframes_{safe_query}_{ts}.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(iframe_info))

        candidates = set()
        for m in re.finditer(r"(?:data-sitekey|sitekey|k)=['\"]?([A-Za-z0-9_-]{8,})['\"]?", html, re.IGNORECASE):
            candidates.add(m.group(1))
        for m in re.finditer(r"(turnstile|cf-turnstile|recaptcha|grecaptcha|g-recaptcha)", html, re.IGNORECASE):
            candidates.add(m.group(0))
        logger.info("Candidates encontrados en HTML (sitekeys/pistas): %s", list(candidates))
        with open(os.path.join(absolute_folder, f"candidates_{safe_query}_{ts}.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(list(candidates)))

        await asyncio.sleep(8)
        html2 = await page.content()
        if html2 != html:
            changed_path = os.path.join(absolute_folder, f"diagnostic_afterwait_{safe_query}_{ts}.html")
            with open(changed_path, "w", encoding="utf-8") as f:
                f.write(html2)
            logger.info("HTML tras espera volcado a: %s", changed_path)
    except Exception:
        logger.exception("Error al volcar HTML/diagnóstico")

# -------------------------
# Human-in-the-loop helper
# -------------------------
async def _wait_for_human_confirmation(user_data_dir: str, timeout: int = 300):
    flag = os.path.join(user_data_dir, "HUMAN_DONE")
    start = time.time()
    try:
        loop = asyncio.get_event_loop()
        fut = loop.run_in_executor(None, input, "Presiona Enter cuando hayas resuelto el CAPTCHA manualmente...\n")
    except Exception:
        fut = None
    while time.time() - start < timeout:
        if os.path.exists(flag):
            return True
        if fut and fut.done():
            return True
        await asyncio.sleep(2)
    return False

# -------------------------
# Función principal
# -------------------------
async def consultar_embajada_alemania_funcionarios(consulta_id: int, nombre: str, apellido: str, cedula: str ):
    browser = None
    fuente_obj = None
    from core.models import Candidato
    try:
        fuente_obj = await _get_fuente(NOMBRE_SITIO)
    except Exception:
        fuente_obj = None

    # Si nombre o apellido no vienen, buscar por la cédula
    if not nombre or not apellido:
        cedula = None
        # Buscar la cédula desde la consulta si existe
        try:
            from core.models import Consulta
            consulta = await sync_to_async(Consulta.objects.get)(id=consulta_id)
            cedula = consulta.candidato.cedula
        except Exception:
            cedula = None
        # Si no se pudo obtener por consulta, intentar por argumento
        if not cedula:
            cedula = None
        # Buscar el candidato por cédula
        if cedula:
            try:
                candidato = await sync_to_async(Candidato.objects.get)(cedula=cedula)
                if not nombre:
                    nombre = candidato.nombre
                if not apellido:
                    apellido = candidato.apellido
            except Exception:
                pass

    try:
        relative_folder = os.path.join("resultados", str(consulta_id))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        query = (f"{(nombre or '').strip()} {(apellido or '').strip()}").strip() or "consulta"
        safe_query = re.sub(r"\s+", "_", query)
        png_name = f"{NOMBRE_SITIO}_{safe_query}_{ts}.png"
        abs_png = os.path.join(absolute_folder, png_name)
        rel_png = os.path.join(relative_folder, png_name).replace("\\", "/")

        ip_pub = await _get_public_ip()
        logger.info("IP pública del servidor: %s", ip_pub)

        async with async_playwright() as p:
            user_data_dir = os.path.join(settings.BASE_DIR, "playwright_user_data", f"user_{consulta_id}")
            os.makedirs(user_data_dir, exist_ok=True)

            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-infobars",
                "--start-maximized",
            ]
            launch_kwargs = {"headless": HEADLESS, "args": launch_args}
            if PROXY:
                launch_kwargs["proxy"] = {"server": PROXY}
            if CHROME_EXECUTABLE:
                launch_kwargs["executable_path"] = CHROME_EXECUTABLE

            logger.info("Lanzando navegador con launch_kwargs: %s", {k: v for k, v in launch_kwargs.items() if k != "executable_path"})
            context: BrowserContext = await p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                **launch_kwargs,
                viewport=None,
                locale="es-CO",
                user_agent=USER_AGENT,
                ignore_https_errors=True,
            )

            try:
                await context.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'languages', { get: () => ['es-ES','es'] });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                    window.chrome = { runtime: {} };
                    const originalQuery = window.navigator.permissions.query;
                    window.navigator.permissions.query = (parameters) => (
                        parameters.name === 'notifications' ?
                            Promise.resolve({ state: Notification.permission }) :
                            originalQuery(parameters)
                    );
                    """
                )
            except Exception:
                pass

            try:
                await context.set_extra_http_headers({
                    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
                    "Referer": "https://www.google.com/",
                })
            except Exception:
                pass

            pages = context.pages
            page: Page = pages[0] if pages else await context.new_page()

            try:
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            except Exception:
                try:
                    await asyncio.sleep(1.0)
                    await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                except Exception:
                    logger.exception("initial goto failed")

            try:
                async with async_playwright() as p:
                    launch_args = [
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                        "--disable-infobars",
                        "--start-maximized",
                    ]
                    launch_kwargs = {"headless": HEADLESS, "args": launch_args}
                    if PROXY:
                        launch_kwargs["proxy"] = {"server": PROXY}
                    if CHROME_EXECUTABLE:
                        launch_kwargs["executable_path"] = CHROME_EXECUTABLE

                    browser = await p.chromium.launch(**launch_kwargs)
                    page: Page = await browser.new_page()

                    try:
                        await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                    except Exception:
                        try:
                            await asyncio.sleep(1.0)
                            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
                        except Exception:
                            pass

                    try:
                        await page.wait_for_load_state("networkidle", timeout=WAIT_NAV_MS)
                    except Exception:
                        pass

                    # Detectar captcha v3 y resolverlo
                    info = await _detect_recaptcha(page)
                    solved = False
                    if info and info.get("type") == "v3" and USE_CAPTCHA_SOLVER and CAPSOLVER_API_KEY:
                        sitekey = info.get("sitekey")
                        action = info.get("action")
                        token = await resolver_capsolver_recaptcha_v3(page.url, sitekey, action=action, timeout=SOLVER_TIMEOUT)
                        if token:
                            await _inject_token_v3(page, token)
                            await asyncio.sleep(1.0)
                            solved = True

                    # Ingresar datos y hacer click en buscar
                    query_text = (f"{(nombre or '').strip()} {(apellido or '').strip()}").strip()
                    if query_text:
                        input_loc = page.locator(SEL_INPUT_VISIBLE)
                        if await input_loc.count() == 0:
                            input_loc = page.locator(SEL_INPUT_FALLBACK)
                        if await input_loc.count() > 0:
                            await input_loc.first.click()
                            await input_loc.first.fill("")
                            await input_loc.first.type(query_text, delay=25)
                            btn = page.locator('button[type="submit"], button:has-text("Buscar"), input[type="submit"]').first
                            if await btn.count() > 0 and await btn.is_visible():
                                await btn.click(timeout=4000)
                            else:
                                await input_loc.press("Enter")
                            await asyncio.sleep(2)

                    # Guardar solo la captura después de buscar
                    await _save_screenshot(page, abs_png)
                    if solved:
                        await _crear_resultado(consulta_id, fuente_obj, 10, "Validada", "Consulta realizada y captcha v3 resuelto.", rel_png)
                    else:
                        await _crear_resultado(consulta_id, fuente_obj, 0, "Sin Validar", "No se pudo resolver captcha v3.", rel_png)

                    try:
                        await browser.close()
                    except Exception:
                        pass

            except Exception as e:
                await _crear_resultado(consulta_id, fuente_obj, 0, "Sin Validar", f"Error: {e}", "")
                # Definir input_loc antes de usarlo
                input_loc = page.locator(SEL_INPUT_VISIBLE)
                if await input_loc.count() == 0:
                    input_loc = page.locator(SEL_INPUT_FALLBACK)
                if await input_loc.count() > 0:
                    await input_loc.first.fill("")
            except Exception:
                pass
            query_text = (f"{(nombre or '').strip()} {(apellido or '').strip()}").strip()
            if query_text:
                input_loc = page.locator(SEL_INPUT_VISIBLE)
                if await input_loc.count() == 0:
                    input_loc = page.locator(SEL_INPUT_FALLBACK)
                if await input_loc.count() > 0:
                    await input_loc.first.click()
                    await input_loc.first.fill("")
                    await input_loc.first.type(query_text, delay=25)
                    # Buscar el botón de submit y hacer click
                    btn = page.locator('button[type="submit"], button:has-text("Buscar"), input[type="submit"]').first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click(timeout=4000)
                    else:
                        await input_loc.press("Enter")

            # esperar render y captura final
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_NAV_MS)
            except Exception:
                pass
            await asyncio.sleep(WAIT_POST_MS / 1000)

            nores_sel = "div.content h3:has-text('Su búsqueda no produjo resultados'), h3:has-text('Su búsqueda no produjo resultados')"
            nores = page.locator(nores_sel).first

            if (await nores.count()) > 0 and (await nores.is_visible()):
                score_final = 0
                mensaje_final = "Su búsqueda no produjo resultados"
            else:
                score_final = 10
                mensaje_final = "Se encontraron hallazgos"

            await _save_screenshot(page, abs_png)

            try:
                await context.close()
            except Exception:
                pass

        await _crear_resultado(consulta_id, fuente_obj, score_final, "Validada", mensaje_final, rel_png)

    except Exception as e:
        logger.exception("Error fatal en el bot: %s", e)
        try:
            await _crear_resultado(consulta_id, fuente_obj, 0, "Sin Validar", str(e), "")
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass    
