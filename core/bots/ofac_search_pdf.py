import os
import datetime
import aiohttp
from bs4 import BeautifulSoup
from asgiref.sync import sync_to_async
from django.conf import settings
from PIL import Image, ImageDraw, ImageFont
from core.models import Resultado, Fuente  # ajusta según tu app


nombre_sitio="ofac"
async def consultar_ofac_pdf(consulta_id: int, cedula: str, nombre: str):
    url = f"https://search.usa.gov/search?utf8=%E2%9C%93&affiliate=ofac&query={cedula}&commit=Search"

    # Descargar HTML con aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            html = await resp.text()

    soup = BeautifulSoup(html, "html.parser")
    print(soup)
    # Buscar coincidencias de la cédula en el texto de la página
    coincidencias = soup.get_text().count(cedula)

    if coincidencias > 2:
        estado = "validado"
        mensaje = f"Se encontraron {coincidencias} coincidencias para {cedula}- {nombre}"
        print(mensaje)
        score = 5
    else:
        estado = "validado"
        mensaje = f"No se encontraron coincidencias relevantes para {cedula} - {nombre}"
        print(mensaje)
        score = 1

    # Crear carpeta de resultados
    relative_folder = os.path.join("resultados", str(consulta_id))
    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
    os.makedirs(absolute_folder, exist_ok=True)

    # Crear imagen con PIL
    fecha = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    imagen_path = os.path.join(absolute_folder, "resultado.png")

    img = Image.new("RGB", (800, 400), color="white")
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("arial.ttf", 18)  # ajusta fuente según tu servidor
    except:
        font = ImageFont.load_default()

    draw.text((20, 40), f"Página: OFAC - search.usa.gov", fill="black", font=font)
    draw.text((20, 80), f"Consulta: {cedula} - {nombre}", fill="black", font=font)
    draw.text((20, 120), f"URL: {url}", fill="black", font=font)
    draw.text((20, 160), f"Fecha: {fecha}", fill="black", font=font)
    draw.text((20, 200), f"Estado: {estado}", fill="black", font=font)

    img.save(imagen_path)

    # Guardar resultado en BD
    fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_sitio)  # ajusta si usas otra lógica
    archivo_relativo = os.path.join(relative_folder, "resultado.png")

    await sync_to_async(Resultado.objects.create)(
        consulta_id=consulta_id,
        fuente=fuente_obj,
        score=score,
        estado=estado,
        archivo=archivo_relativo,
        mensaje=mensaje
    )

    return {"estado": estado, "mensaje": mensaje, "archivo": archivo_relativo}
