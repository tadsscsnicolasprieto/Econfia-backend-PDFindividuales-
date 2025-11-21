import re
import asyncio
from playwright.async_api import async_playwright

PAGE_URL = "https://www.procuraduria.gov.co/Pages/Consulta-de-Antecedentes.aspx"

TIPO_DOC_MAP = {
    'CC': '1',
    'PEP': '0',
    'NIT': '2',
    'CE': '5',
    'PPT': '10',
    'TI': '3',
}

PREGUNTAS_RESPUESTAS = {
    '¿ Cuanto es 9 - 2 ?': '7',
    '¿ Cuanto es 3 X 3 ?': '9',
    '¿ Cuanto es 6 + 2 ?': '8',
    '¿ Cuanto es 2 X 3 ?': '6',
    '¿ Cuanto es 3 - 2 ?': '1',
    '¿ Cuanto es 4 + 3 ?': '7'
}


async def procuraduria_bio(cedula, tipo_doc):
    """
    Consulta antecedentes en Procuraduría.
    Hace hasta 3 intentos en caso de fallas.
    """
    resultado_json = {
        "datos": {
            'cedula': '',
            'tipo_doc': '',
            'nombre': '',
            'apellido': '',
            'fecha_nacimiento': '',
            'fecha_expedicion': '',
            'tipo_persona': '',
            'sexo': ''
        }
    }

    async def _procuraduria_bio_run():
        browser = None
        try:

            tipo_doc_val = TIPO_DOC_MAP.get((tipo_doc or "").upper())
            if not tipo_doc_val:
                return resultado_json  

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=False)  # pon False para debug
                context = await browser.new_context()
                page = await context.new_page()

                await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=120000)
                await page.wait_for_timeout(2000)

                # localizar iframe
                frame = None
                for f in page.frames:
                    if "webcert/Certificado.aspx" in (f.url or ""):
                        frame = f
                        break
                if not frame and page.frames and len(page.frames) > 1:
                    frame = page.frames[-1]
                if not frame:
                    return resultado_json

                # llenar formulario
                await frame.wait_for_selector('#ddlTipoID', timeout=15000)
                await frame.select_option('#ddlTipoID', value=tipo_doc_val)
                await frame.fill('#txtNumID', str(cedula))

                # resolver pregunta
                for i in range(10):
                    try:
                        pregunta = (await frame.inner_text('#lblPregunta')).strip()
                    except Exception as e:
                        pregunta = ""
                    respuesta = PREGUNTAS_RESPUESTAS.get(pregunta)
                    if respuesta:
                        await frame.fill('#txtRespuestaPregunta', respuesta)
                        break
                    try:
                        await frame.click('#ImageButton1')  # refrescar
                    except Exception as e:
                        print("⚠️ Error al recrgar:", e)
                    await asyncio.sleep(1)

                # enviar
                await frame.click('#btnConsultar')
                await frame.wait_for_selector("#divSec", state="attached", timeout=30000)

                await asyncio.sleep(5)

                div_text = (await frame.locator("#divSec").inner_text()).strip()
                # extraer spans con nombre y apellidos
                spans = await frame.locator("#divSec .datosConsultado span").all_inner_texts()
                palabras = [s.strip() for s in spans if s.strip()]
                print("📌 Palabras detectadas:", palabras)

                if palabras:
                    if len(palabras) == 3:
                        resultado_json['datos']['nombre'] = " ".join(palabras[:-1])
                        resultado_json['datos']['apellido'] = palabras[-1]
                    elif len(palabras) == 4:
                        resultado_json['datos']['nombre'] = " ".join(palabras[:-2])
                        resultado_json['datos']['apellido'] = " ".join(palabras[-2:])
                    elif len(palabras) > 4:
                        resultado_json['datos']['nombre'] = " ".join(palabras[:-2])
                        resultado_json['datos']['apellido'] = " ".join(palabras[-2:])
                    elif len(palabras) == 2:
                        resultado_json['datos']['nombre'] = palabras[0]
                        resultado_json['datos']['apellido'] = palabras[1]
                    elif len(palabras) == 1:
                        resultado_json['datos']['nombre'] = palabras[0]

                # extraer cédula
                cedula_match = re.search(r"Número\s+(\d+)", div_text)
                if cedula_match:
                    resultado_json['datos']['cedula'] = cedula_match.group(1)
                    resultado_json['datos']['tipo_doc'] = tipo_doc.upper()

        except Exception as e:
            print("🔥 Error en bot:", e)
            raise  # relanzamos el error para que lo capture el loop de intentos

        finally:
            if browser:
                await browser.close()
                print("🔹 Navegador cerrado")

        return resultado_json

    # Reintentos (máx 3)
    for intento in range(1, 4):
        try:
            print(f"\n===== 🔄 Intento {intento} de 3 =====")
            return await _procuraduria_bio_run()
        except Exception as e:
            print(f"⚠️ Falló el intento {intento}: {e}")
            if intento < 3:
                await asyncio.sleep(3)
                print("🔁 Reintentando...")
            else:
                print("❌ Todos los intentos fallaron")
                return resultado_json
