import asyncio
import capsolver
from decouple import config

capsolver.api_key = config('CAPTCHA_TOKEN')

def resolver_captcha_v2_sync(url, sitekey):
    print(config('CAPTCHA_TOKEN'))
    def resolver_captcha_v2_sync(url, sitekey, isInvisible=None):
        payload = {
            "type": "RecaptchaV2TaskProxyless",
            "websiteURL": url,
            "websiteKey": sitekey,
        }
        if isInvisible is not None:
            payload["isInvisible"] = isInvisible
        solution = capsolver.solve(payload)
        return solution['gRecaptchaResponse']

# Versión async
async def resolver_captcha_v2(url, sitekey):
    async def resolver_captcha_v2(url, sitekey, isInvisible=None):
        return await asyncio.to_thread(resolver_captcha_v2_sync, url, sitekey, isInvisible)
