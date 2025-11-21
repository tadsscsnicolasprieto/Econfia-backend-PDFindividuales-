import asyncio
import capsolver
from decouple import config

capsolver.api_key = config('CAPTCHA_TOKEN')

def resolver_captcha_v2_sync(url, sitekey):
    print(config('CAPTCHA_TOKEN'))
    solution = capsolver.solve({
        "type": "ReCaptchaV2TaskProxyLess",
        "websiteURL": url,
        "websiteKey": sitekey,
    })
    return solution['gRecaptchaResponse']

# Versión async
async def resolver_captcha_v2(url, sitekey):
    return await asyncio.to_thread(resolver_captcha_v2_sync, url, sitekey)
