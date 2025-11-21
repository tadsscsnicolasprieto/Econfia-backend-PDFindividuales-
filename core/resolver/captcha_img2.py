import asyncio
from twocaptcha import TwoCaptcha
from decouple import config

api_key = config('CAPTCHA_TOKEN_2CAPTCHA')

def resolver_captcha_imagen_sync(ruta_imagen):
    solver = TwoCaptcha(api_key)
    try:
        resultado = solver.normal(ruta_imagen)
        return resultado['code']
    except Exception as e:
        print(f"[ERROR] No se pudo resolver el captcha: {e}")
        return None

# Versión async que llama a la sync sin bloquear
async def resolver_captcha_imagen(ruta_imagen):
    return await asyncio.to_thread(resolver_captcha_imagen_sync, ruta_imagen)
