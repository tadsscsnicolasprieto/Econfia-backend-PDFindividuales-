# core/bots/cpae_verify_certification.py
import os
import re
import asyncio
from datetime import datetime
from django.conf import settings
from asgiref.sync import sync_to_async
from playwright.async_api import async_playwright

from core.models import Resultado, Fuente

URL = "https://tramites.cpae.gov.co/public?show=verifyCertification"
NOMBRE_SITIO = "cpae_verify_certification"

def _safe(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

async def consultar_cpae_verify_certification(consulta_id: int, cedula: str):
    """
    CPAE – Verificar certificaciones expedidas:
      - Abre el popup de verificación
      - Escribe el 'código de verificación' (usamos la cédula provista)
      - Click en 'Verificar' cuando el botón se habilite
      - Espera el resultado y hace screenshot
      - Registra Resultado en BD (ok/error)
    """
    browser = None

    # 1) Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id, fuente=None, score=0,
            estado="Sin Validar", mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}", archivo=""
        )
        return

    try:
        # 2) Carpeta resultados/<consulta_id>
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

            # 3) Ir a la página (popup visible)
            await page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # 4) Campo de código (usamos la cédula)
            code_input = page.locator("input[formcontrolname='code']").first
            await code_input.wait_for(state="visible", timeout=20000)
            await code_input.click()
            try:
                await code_input.fill("")
            except Exception:
                pass
            await code_input.type(str(cedula or ""), delay=20)
            await asyncio.sleep(0.4)

            # 5) Botón Verificar: esperar que se habilite y click
            btn = page.locator("button.btn-govco.fill-btn-govco").first
            for _ in range(12):  # ~6s
                if (await btn.get_attribute("disabled")) is None:
                    break
                await asyncio.sleep(0.5)

            try:
                await btn.click(timeout=5000)
            except Exception:
                # Si no se habilita, seguimos para capturar el estado actual
                pass

            # 6) Esperar resultado / estabilización y screenshot
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
                await asyncio.sleep(3)
            except Exception:
                pass

            # Señales de resultado (best-effort)
            for sel in [
                ".mat-dialog-container",
                "text=/resultado/i",
                "text=/certificaci[oó]n/i",
                "text=/verificaci[oó]n/i",
            ]:
                try:
                    await page.locator(sel).first.wait_for(state="visible", timeout=3500)
                    break
                except Exception:
                    continue

            await page.screenshot(path=out_png_abs, full_page=True)

            await ctx.close()
            await browser.close()
            browser = None

        # 7) Guardar OK
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
