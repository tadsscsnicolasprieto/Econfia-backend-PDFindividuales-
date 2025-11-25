# scripts/check_page.py
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            r = await page.goto("https://registrobicibogota.movilidadbogota.gov.co/rdbici/#/consultarEstado", timeout=30000)
            print("Status:", r.status if r else "No response")
            print("URL:", page.url)
            await page.screenshot(path="check_bici.png", full_page=True)
            print("Screenshot saved: check_bici.png")
        except Exception as e:
            print("Error:", e)
        await browser.close()

import asyncio
asyncio.run(main())