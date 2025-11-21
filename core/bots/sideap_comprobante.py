# core/bots/sideap_comprobante.py
import os
import re
import asyncio
from datetime import datetime, date
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from core.models import Resultado, Fuente
from core.resolver.captcha_v2 import resolver_captcha_v2

NOMBRE_SITIO = "sideap_comprobante"
URL = "https://sideap.serviciocivil.gov.co:4443/sideap/publico/bogotaTieneTalento/comprobante/index.xhtml"

SEL_TIPO_LABEL = "#consultarInformaciion\\:selectTipoDocumento_label"
SEL_TIPO_ITEMS = "#consultarInformaciion\\:selectTipoDocumento_items li.ui-selectonemenu-item"

SEL_DOC    = "#consultarInformaciion\\:inputNumeroDocumento"
SEL_FECHA  = "#consultarInformaciion\\:calFechaNacimiento_input"  # dd/mm/yy
SEL_CORREO = "#consultarInformaciion\\:inputCorreoElectronicoPersonal"

BTN_CONSULTAR = [
    "#consultarInformaciion\\:btnConsultar",
    "button:has-text('Consultar')",
    "a:has-text('Consultar')",
    "span:has-text('Consultar')"
]

SEL_TEXTO_ERROR_GLOBAL = ".texterror"
SEL_GROWL_ITEM   = ".ui-growl-item"
SEL_GROWL_TITLE  = ".ui-growl-title"
SEL_GROWL_DETAIL = ".ui-growl-message p, .ui-growl-item p"

SITEKEY = "6LfAjSYTAAAAAFuFHLr7vBAF9zK0Y2USc5DkoVqR"

TIPO_DOC_LABEL = {
    "CC":  "CÉDULA DE CIUDADANÍA",
    "CE":  "CÉDULA DE EXTRANJERÍA",
    "PEP": "PERMISO ESPECIAL DE PERMANENCIA - PEP",
    "PPT": "PERMISO POR PROTECCIÓN TEMPORAL - PPT",
}

NAV_TIMEOUT = 120_000
SHORT  = 1200
MEDIUM = 2500
LONG   = 4000
XLONG  = 9000


def _norm_fecha_ddmmyy(s) -> str:
    """
    Normaliza a formato dd/mm/yy.
    Acepta: datetime.date, datetime.datetime, números y strings con
    separadores (-, /, .) o pegados (ddmmyyyy, yyyymmdd, ddmmyy).
    """
    if s is None or s == "":
        return ""

    # date / datetime
    if isinstance(s, (date, datetime)):
        d = s if isinstance(s, date) and not isinstance(s, datetime) else s.date() if isinstance(s, datetime) else s
        return d.strftime("%d/%m/%y")

    # numérico (p.ej. 19790710)
    if isinstance(s, (int, float)):
        s = str(int(s))

    # string
    s = str(s).strip()
    s_norm = s.replace(".", "/").replace("-", "/").replace("\\", "/")

    def _try_parse(val: str, fmts: list[str]) -> str | None:
        for f in fmts:
            try:
                return datetime.strptime(val, f).strftime("%d/%m/%y")
            except Exception:
                pass
        return None

    # Con separadores
    parsed = _try_parse(s_norm, [
        "%d/%m/%y", "%d/%m/%Y",
        "%m/%d/%y", "%m/%d/%Y",
        "%Y/%m/%d", "%Y-%m-%d",  # por si quedó alguno con '-'
    ])
    if parsed:
        return parsed

    # Sin separadores
    only_digits = re.sub(r"\D+", "", s)
    if len(only_digits) == 8:
        # intentos: yyyymmdd -> ddmmyyyy -> mmddyyyy
        for pat in ("%Y%m%d", "%d%m%Y", "%m%d%Y"):
            parsed = _try_parse(only_digits, [pat])
            if parsed:
                return parsed
    elif len(only_digits) == 6:
        # ddmmyy o yymmdd
        for pat in ("%d%m%y", "%y%m%d"):
            parsed = _try_parse(only_digits, [pat])
            if parsed:
                return parsed

    # fallback: devolver tal cual (el servidor validará)
    return s


async def _guardar_resultado(consulta_id, fuente_obj, estado, mensaje, rel_png, score=1):
    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        estado=estado,
        mensaje=mensaje,
        archivo=(rel_png or "").replace("\\", "/"),
        score=score,
    )


async def consultar_sideap_comprobante(
    consulta_id: int,
    numero: str,
    tipo_doc: str,          # "CC"|"CE"|"PEP"|"PPT"
    fecha_nacimiento: str,  # acepta varios -> se normaliza a dd/mm/yy, ej. "10/07/79"
    correo: str
):
    fuente = await sync_to_async(lambda: Fuente.objects.filter(nombre=NOMBRE_SITIO).first())()
    if not fuente:
        await _guardar_resultado(consulta_id, None, "Sin Validar",
                                 f"No existe Fuente con nombre='{NOMBRE_SITIO}'", "", score=0)
        return

    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_num = re.sub(r"\s+", "_", (numero or "").strip()) or "consulta"
    png_name = f"{NOMBRE_SITIO}_{safe_num}_{ts}.png"
    abs_png  = os.path.join(absolute_folder, png_name)
    rel_png  = os.path.join(relative_folder, png_name)

    browser = context = page = None
    try:
        label = TIPO_DOC_LABEL.get((tipo_doc or "").upper())
        if not label:
            await _guardar_resultado(consulta_id, fuente, "Sin Validar",
                                     f"Tipo de documento no soportado: {tipo_doc!r}", "", score=0)
            return

        fecha_ddmmyy = _norm_fecha_ddmmyy(fecha_nacimiento)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await browser.new_context(locale="es-CO", viewport={"width": 1366, "height": 1000})
            page = await context.new_page()

            await page.goto(URL, wait_until="domcontentloaded", timeout= NAV_TIMEOUT)
            try: await page.wait_for_load_state("networkidle", timeout=LONG)
            except Exception: pass
            await page.wait_for_timeout(SHORT)

            # Tipo de documento (PrimeFaces)
            try:
                await page.click(SEL_TIPO_LABEL, timeout=12_000)
            except Exception:
                try: await page.click("#consultarInformaciion\\:selectTipoDocumento", timeout=12_000)
                except Exception: pass
            item = page.locator(SEL_TIPO_ITEMS, has_text=label).first
            await item.wait_for(state="visible", timeout=8_000)
            await item.click()
            await page.wait_for_timeout(300)

            # Número
            await page.fill(SEL_DOC, str(numero or ""))

            # Fecha dd/mm/yy -> escribir, TAB y mover foco al correo (cierra datepicker y libera reCAPTCHA)
            await page.fill(SEL_FECHA, fecha_ddmmyy)
            try:
                await page.locator(SEL_FECHA).press("Tab")
            except Exception:
                # Blur manual si Tab falla
                await page.eval_on_selector(SEL_FECHA, "el => el.blur()")
            await page.wait_for_timeout(200)
            await page.locator(SEL_CORREO).focus()

            # Correo
            await page.fill(SEL_CORREO, str(correo or "").strip())

            # Asegura que el captcha esté visible (por si quedó tapado)
            try:
                await page.locator(".g-recaptcha").scroll_into_view_if_needed()
            except Exception:
                pass

            # reCAPTCHA v2
            token = await resolver_captcha_v2(page.url, SITEKEY)
            await page.evaluate(
                """(tok) => {
                    let el = document.getElementById('g-recaptcha-response');
                    if (!el) {
                        el = document.createElement('textarea');
                        el.id = 'g-recaptcha-response';
                        el.name = 'g-recaptcha-response';
                        el.style.display = 'none';
                        document.body.appendChild(el);
                    }
                    el.value = tok;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                token
            )
            await page.wait_for_timeout(600)

            # Consultar
            clicked = False
            for sel in BTN_CONSULTAR:
                try:
                    await page.locator(sel).first.click(timeout=4000)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                raise RuntimeError("No se encontró el botón de 'Consultar'.")

            await page.wait_for_timeout(1500)

            # Caminos
            mensaje = ""

            # (1) Servicio caído
            try:
                texterror = page.locator(SEL_TEXTO_ERROR_GLOBAL, has_text=re.compile("inconvenientes", re.I)).first
                if await texterror.is_visible(timeout=1200):
                    mensaje = (await texterror.inner_text() or "Tenemos inconvenientes en este momento").strip()
            except Exception:
                pass

            # (2) Sin resultados (growl)
            if not mensaje:
                try:
                    growl = page.locator(SEL_GROWL_ITEM).first
                    if await growl.is_visible(timeout=1500):
                        title = ""
                        detail = ""
                        try:
                            if await page.locator(SEL_GROWL_TITLE).count():
                                title = (await page.locator(SEL_GROWL_TITLE).first.inner_text() or "").strip()
                        except Exception:
                            pass
                        try:
                            if await page.locator(SEL_GROWL_DETAIL).count():
                                detail = (await page.locator(SEL_GROWL_DETAIL).first.inner_text() or "").strip()
                        except Exception:
                            pass
                        if (title or "").lower().startswith("error") and "no se encontraron datos" in (detail or "").lower():
                            mensaje = f"{title}: {detail}".strip()
                except Exception:
                    pass

            # (3) Resto
            if not mensaje:
                mensaje = "Consulta realizada (ver evidencia)."

            await page.screenshot(path=abs_png, full_page=True)

            await context.close()
            await browser.close()
            context = browser = None

        await _guardar_resultado(consulta_id, fuente, "Validada", mensaje, rel_png, score=1)

    except Exception as e:
        try:
            if page:
                try:
                    await page.screenshot(path=abs_png, full_page=True)
                except Exception:
                    pass
        except Exception:
            pass

        await _guardar_resultado(
            consulta_id, fuente, "Sin Validar",
            f"{type(e).__name__}: {e}",
            rel_png if os.path.exists(abs_png) else "",
            score=0
        )
    finally:
        try:
            if context: await context.close()
        except Exception: pass
        try:
            if browser: await browser.close()
        except Exception: pass
