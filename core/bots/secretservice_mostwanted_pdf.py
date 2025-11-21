import os
import re
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

URL = "https://www.secretservice.gov/investigations/mostwanted"
NOMBRE_SITIO = "secretservice_mostwanted"
MAX_INTENTOS = 3

def _safe_name(s: str) -> str:
    s = (s or "consulta").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    return s or "consulta"

async def consultar_secretservice_mostwanted_pdf(consulta_id: int, nombre: str, cedula):
    nombre = (nombre or "").strip()
    safe = _safe_name(nombre)

    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = os.path.join(absolute_folder, f"{NOMBRE_SITIO}_{cedula}_{timestamp}.png")
    screenshot_rel = os.path.join(relative_folder, f"{NOMBRE_SITIO}_{cedula}_{timestamp}.png")

    # Fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=NOMBRE_SITIO)
    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin validar",
            mensaje=f"No se encontró la Fuente '{NOMBRE_SITIO}': {e}",
            archivo="",
        )
        return

    intentos = 0
    while intentos < MAX_INTENTOS:
        intentos += 1
        browser = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
                ctx = await browser.new_context(viewport={"width":1440,"height":1024}, locale="en-US")
                page = await ctx.new_page()
                await page.goto(URL, wait_until="domcontentloaded", timeout=120000)

                try:
                    await page.wait_for_selector(".most-wanted-container", timeout=15000)
                    contenedor = page.locator(".most-wanted-container").first
                    texto = await contenedor.inner_text()
                    # Coincidencia exacta
                    if nombre.lower() in texto.lower():
                        score = 10
                        mensaje = "Se encontraron resultados"
                    else:
                        score = 0
                        mensaje = "No se encontraron resultados"
                except Exception:
                    score = 0
                    mensaje = "No se encontraron resultados"

                await page.screenshot(path=screenshot_path, full_page=True)
                await ctx.close()
                await browser.close()
                browser = None

                # Guardar en BD
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validado",
                    mensaje=mensaje,
                    archivo=screenshot_rel
                )
                return

        except Exception as e:
            if browser:
                try:
                    await page.screenshot(path=screenshot_path, full_page=True)
                    await browser.close()
                except:
                    pass
            if intentos >= MAX_INTENTOS:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin validar",
                    mensaje="Ocurrió un problema al obtener la información de la fuente",
                    archivo=screenshot_rel if os.path.exists(screenshot_path) else ""
                )
