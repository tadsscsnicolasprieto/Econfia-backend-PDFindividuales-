import httpx
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
import os
from django.conf import settings
from core.models import Resultado
from asgiref.sync import sync_to_async

NOMBRE_SITIO = "dfat_consolidated_pdf"

async def consultar_dfat_consolidated_pdf(consulta_id, nombre, cedula):
    url = f"https://www.dfat.gov.au/search?keys={nombre.replace(' ', '%20')}&page=1"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/117.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }

    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            headers=headers,
            follow_redirects=True,
            verify=False
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente_id=None,
                    estado="error",
                    mensaje=f"Error HTTP {resp.status_code} al acceder a {url}"
                )
                return None

            soup = BeautifulSoup(resp.text, "html.parser")
            resultados_html = soup.select(".search-results .search-result")

            if not resultados_html:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente_id=None,
                    estado="validado",
                    mensaje=f"No se encontraron resultados para {nombre}"
                )
                return None

            # Crear imagen
            ancho, alto = 1000, 600
            img = Image.new("RGB", (ancho, alto), (240, 248, 255))  # azul claro
            draw = ImageDraw.Draw(img)

            try:
                font_title = ImageFont.truetype("arial.ttf", 32)
                font_text = ImageFont.truetype("arial.ttf", 18)
            except:
                font_title = ImageFont.load_default()
                font_text = ImageFont.load_default()

            # Header
            draw.rectangle([(0, 0), (ancho, 80)], fill=(0, 51, 102))
            draw.text((20, 20), f"Resultados DFAT para {nombre}", font=font_title, fill="white")

            # Lista resultados
            y = 100
            for idx, r in enumerate(resultados_html[:5]):  # solo los primeros 5
                titulo = r.get_text(strip=True)
                draw.text((20, y), f"{idx+1}. {titulo}", font=font_text, fill=(0, 0, 0))
                y += 40

            # Guardar imagen
            nombre_archivo = f"consulta_{consulta_id}_{NOMBRE_SITIO}.png"
            ruta_archivo = os.path.join(settings.MEDIA_ROOT, nombre_archivo)
            img.save(ruta_archivo)

            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente_id=None,
                estado="validado",
                archivo=nombre_archivo,
                mensaje=f"Se encontraron {len(resultados_html)} resultados para {nombre}"
            )

            return ruta_archivo

    except Exception as e:
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente_id=None,
            estado="offline",
            mensaje=str(e)
        )
        return None
