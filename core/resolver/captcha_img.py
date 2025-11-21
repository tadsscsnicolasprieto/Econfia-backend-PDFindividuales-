import asyncio
import base64
from decouple import config
import capsolver

capsolver.api_key = config('CAPTCHA_TOKEN')

def resolver_captcha_imagen_sync(ruta_imagen: str) -> str:
    # Leer y codificar la imagen en base64
    with open(ruta_imagen, "rb") as f:
        encoded_image = base64.b64encode(f.read()).decode("utf-8")

    solution = capsolver.solve({
        "type": "ImageToTextTask",  # Tipo de tarea: convertir imagen a texto
        "body": encoded_image       # Imagen codificada en base64
    })
    
    return solution['text']

# Versión async para bots asíncronos
async def resolver_captcha_imagen(ruta_imagen: str) -> str:
    return await asyncio.to_thread(resolver_captcha_imagen_sync, ruta_imagen)
