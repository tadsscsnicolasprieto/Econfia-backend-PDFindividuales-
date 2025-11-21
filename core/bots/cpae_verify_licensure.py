# core/bots/cpae_verify_licensure.py
import os
import re
import asyncio
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://tramites.cpae.gov.co/public?show=verifyLicensure"
NOMBRE_SITIO = "cpae_verify_licensure"

# Mapea tus claves internas al texto del mat-select
TIPO_DOC_MAP = {
    "CC": "CÉDULA DE CIUDADANÍA",
    "CE": "CÉDULA DE EXTRANJERÍA",
}

def _safe(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

async def consultar_cpae_verify_licensure(consulta_id: int, tipo_doc: str, cedula: str):
    """
    CPAE – Verificar profesionales matriculados:
      - Abre la página (aparece popup)
      - Selecciona tipo de documento (Angular Material)
      - Digita el número y pulsa 'Verificar' (cuando se habilite)
      - Espera el resultado y toma un screenshot
      - Registra un Resultado en la BD (ok/error)
    """
    browser = None
    page = None

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    try:
        # Carpeta resultados/<consulta_id>
        rel_folder = os.path.join("resultados", str(consulta_id))
        abs_folder = os.path.join(settings.MEDIA_ROOT, rel_folder)
        os.makedirs(abs_folder, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_num = _safe(cedula)
        out_png_abs = os.path.join(abs_folder, f"{NOMBRE_SITIO}_{safe_num}_{ts}.png")
        out_png_rel = os.path.join(rel_folder, os.path.basename(out_png_abs))

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000})
            page = await ctx.new_page()

            # 1) Ir a la página (aparece el popup)
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # 2) Tipo de documento (mat-select)
            mat_select = page.locator("mat-select[formcontrolname='documentType']").first
            await mat_select.wait_for(state="visible", timeout=20000)
            await mat_select.click()

            etiqueta = TIPO_DOC_MAP.get((tipo_doc or "").upper(), "CÉDULA DE CIUDADANÍA")
            await page.locator("mat-option .mat-option-text", has_text=etiqueta).first.click()

            # 3) Número de documento
            inp_doc = page.locator("input[formcontrolname='documentNumber']").first
            await inp_doc.wait_for(state="visible", timeout=15000)
            await inp_doc.click()
            try:
                await inp_doc.fill("")
            except Exception:
                pass
            await inp_doc.type(str(cedula or ""), delay=20)
            await asyncio.sleep(0.4)

            # 4) Botón 'Verificar' (se habilita con datos válidos)
            btn = page.locator("button.btn-govco.fill-btn-govco").first
            for _ in range(10):
                disabled = await btn.get_attribute("disabled")
                if disabled is None:
                    break
                await asyncio.sleep(0.5)

            try:
                await btn.click(timeout=5000)
            except Exception:
                # si no se habilita, igual capturamos el estado actual
                pass

            # 5) Esperar algo de resultado (o estabilización) y screenshot
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            # Señales opcionales de resultado (si aparecen, mejor)
            for sel in [
                "text=/Resultado/i",
                "text=/Profesional/i",
                "text=/Matr[ií]cula/i",
                ".mat-dialog-container",
            ]:
                try:
                    await page.locator(sel).first.wait_for(state="visible", timeout=2500)
                    break
                except Exception:
                    continue

            await page.screenshot(path=out_png_abs, full_page=True)

            await ctx.close()
            await browser.close()
            browser = None

        # Guardar en BD
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=fuente_obj,
            score=0,
            estado="Validada",
            mensaje="",
            archivo=out_png_rel,
        )

    except Exception as e:
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_obj,
                score=0,
                estado="Sin Validar",
                mensaje=str(e),
                archivo="",
            )
        finally:
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
