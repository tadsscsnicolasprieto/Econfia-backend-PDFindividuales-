import os
import asyncio
from urllib.parse import quote_plus
from django.conf import settings
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

NOMBRE_SITIO = "garantias_mobiliarias_nooficial"


async def consultar_garantias_mobiliarias_nooficial(consulta_id, nombre, nro_bien):
    nombre = (nombre or "").strip()
    url_base = "https://www.garantiasmobiliarias.com.co/rgm/Garantias/ConsultaGarantia.aspx"

    # Buscar fuente
    try:
        fuente_obj = await sync_to_async(Fuente.objects.get)(nombre="garantias_mobiliarias_nooficial")
        print(f"[RGM] Fuente encontrada: {getattr(fuente_obj, 'id', None)}")
    except Exception as e:
        print(f"[RGM][ERROR] No se encontró la fuente: {e}")
        await sync_to_async(Resultado.objects.create)(
            consulta_id=consulta_id,
            fuente=None,
            score=0,
            estado="Sin Validar",
            mensaje="No se encontró la fuente",
            archivo=""
        )
        return

    try:
        async with async_playwright() as p:
            print("[RGM] Lanzando navegador...")
            browser = await p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"]
            )

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="es-CO",
                viewport={"width": 1366, "height": 768},
            )

            # ANTI-BOT: reforzar señales de navegador real
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', { get: () => ['es-CO', 'es', 'en-US'] });
                Object.defineProperty(navigator, 'plugins', { 
                  get: () => [ { name: 'Chrome PDF Viewer' }, { name: 'Chromium PDF Viewer' }, { name: 'Native Client' } ] 
                });
                const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
                if (originalQuery) {
                  window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications'
                      ? Promise.resolve({ state: 'granted' })
                      : originalQuery(parameters)
                  );
                }
            """)

            page = await context.new_page()

            # 1 Cargar página inicial
            print(f"[RGM] Cargando URL base: {url_base}")
            await page.goto(url_base, wait_until="domcontentloaded")
            await asyncio.sleep(1)
            print("[RGM] Página base cargada")

            # Intento 1: navegar directamente con parámetros (RECOMENDADO si funciona)
            try:
                url_directa = f"https://www.garantiasmobiliarias.com.co/rgm/Garantias/ConsultaGarantia.aspx?NombreDeudor={quote_plus(nombre)}&ConsultaOficial=false"
                print("[RGM] Intentando URL directa con nombre:", url_directa)
                await page.goto(url_directa, wait_until="domcontentloaded")
                await page.wait_for_load_state("networkidle", timeout=10000)
                # si aparece el label de resultado, continuar con captura final
                if await page.locator("#ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_lblMensajeBusqueda").count():
                    print("[RGM] URL directa cargó resultados (lblMensajeBusqueda detectado)")
                    # preparar carpeta y captura final
                    relative_folder = os.path.join("resultados", str(consulta_id))
                    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
                    os.makedirs(absolute_folder, exist_ok=True)
                    nombre_archivo = f"NOOFICIAL_{consulta_id}.png"
                    abs_path = os.path.join(absolute_folder, nombre_archivo)
                    rel_path = os.path.join(relative_folder, nombre_archivo)
                    print("[RGM] Tomando captura final (URL directa) en:", abs_path)
                    await page.screenshot(path=abs_path, full_page=True)
                    # leer mensaje de resultado
                    score = 6
                    mensaje = "Consulta ejecutada correctamente."
                    span = page.locator("#ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_lblMensajeBusqueda")
                    try:
                        if await span.is_visible():
                            txt = (await span.inner_text()).strip()
                            print(f"[RGM] Texto en lblMensajeBusqueda: '{txt}'")
                            if txt:
                                score = 0
                                mensaje = txt
                    except Exception:
                        pass
                    # guardar resultado
                    try:
                        await sync_to_async(Resultado.objects.create)(
                            consulta_id=consulta_id,
                            fuente=fuente_obj,
                            score=score,
                            estado="Validada",
                            mensaje=mensaje,
                            archivo=rel_path
                        )
                        print(f"[RGM] Resultado guardado en BD (URL directa): consulta_id={consulta_id}, archivo={rel_path}")
                    except Exception as save_exc:
                        print(f"[RGM][ERROR] Falló guardar Resultado (URL directa): {save_exc}")
                    await browser.close()
                    print("[RGM] Navegador cerrado. Flujo finalizado (URL directa).")
                    return
                else:
                    print("[RGM] URL directa no mostró lblMensajeBusqueda; continuar con UI forzada")
            except Exception as e_direct:
                print("[RGM][WARN] Intento URL directa falló o no mostró resultados:", e_direct)
                # continuar con UI forzada

            # 2 Forzar UI: activar 'Consultas no oficiales' y pestaña 'Por nombre'
            radio_no_oficial = "input[id*='rbNoOficial']"
            print("[RGM] Intentando activar 'Consultas no oficiales' (UI forzada)")
            try:
                # forzar click nativo en radio no oficial
                await page.evaluate("""(sel) => {
                  const el = document.querySelector(sel);
                  if (el) { el.scrollIntoView({behavior:'instant', block:'center'}); el.click(); }
                }""", radio_no_oficial)
                await asyncio.sleep(0.6)
            except Exception as e:
                print(f"[RGM][WARN] Forzar click en rbNoOficial falló: {e}")

            # verificar radios (si existen)
            try:
                radio_oficial = "#ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_rbOficial"
                radio_no_oficial_id = "#ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_rbNoOficial"
                oficial_checked = False
                no_oficial_checked = False
                if await page.locator(radio_oficial).count():
                    oficial_checked = await page.locator(radio_oficial).is_checked()
                if await page.locator(radio_no_oficial_id).count():
                    no_oficial_checked = await page.locator(radio_no_oficial_id).is_checked()
                print(f"[RGM] radio_oficial checked: {oficial_checked}")
                print(f"[RGM] radio_no_oficial checked: {no_oficial_checked}")
            except Exception as e:
                print(f"[RGM][WARN] No se pudo verificar estado de radios: {e}")

            await asyncio.sleep(0.8)

            # 3 Seleccionar pestaña POR NOMBRE (ID preferido, fallback por texto)
            tab_por_nombre = "#ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_lnkPorNombre"
            print("[RGM] Intentando seleccionar pestaña 'Por nombre'")
            try:
                if await page.locator(tab_por_nombre).count():
                    await page.click(tab_por_nombre)
                    print("[RGM] Click en pestaña 'Por nombre' por ID realizado")
                else:
                    await page.click("text=Por nombre")
                    print("[RGM] Click en pestaña 'Por nombre' por texto realizado")
                await asyncio.sleep(0.8)
            except Exception as e:
                print(f"[RGM][ERROR] No se pudo activar pestaña 'Por nombre': {e}")
                # guardar HTML de debug y salir
                try:
                    relative_folder = os.path.join("resultados", str(consulta_id))
                    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
                    os.makedirs(absolute_folder, exist_ok=True)
                    html_debug_path = os.path.join(absolute_folder, f"NOOFICIAL_{consulta_id}_debug_no_tab.html")
                    content = await page.content()
                    with open(html_debug_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    print("[RGM] HTML de debug guardado en:", html_debug_path)
                except Exception as e2:
                    print(f"[RGM][WARN] No se pudo guardar HTML de debug: {e2}")
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin Validar",
                    mensaje="No se pudo activar la pestaña 'Por nombre'",
                    archivo=""
                )
                await browser.close()
                return

            # CHANGED: selector real del campo de nombre según el HTML que mostraste
            campo_nombre_id = "#ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_txtNombre"  # CHANGED

            # 4 Llenar campo de nombre (NO OFICIAL)
            print(f"[RGM] Preparando para llenar el campo de nombre: '{nombre}'")
            try:
                await page.wait_for_selector(campo_nombre_id, timeout=7000)
                # asegurar viewport
                await page.evaluate("""(sel) => {
                  const el = document.querySelector(sel);
                  if (el) el.scrollIntoView({behavior:'instant', block:'center'});
                }""", campo_nombre_id)

                # Intento principal: type()
                await page.fill(campo_nombre_id, "")
                if nombre:
                    await page.type(campo_nombre_id, nombre, delay=80)
                else:
                    raise RuntimeError("El parámetro 'nombre' está vacío.")
                await asyncio.sleep(0.5)

                # Verificar que el campo quedó con el texto esperado
                try:
                    valor_input = await page.locator(campo_nombre_id).input_value()
                    print(f"[RGM] Valor del input nombre (input_value): '{valor_input}'")
                    if valor_input.strip() != nombre.strip():
                        print(f"[RGM][WARN] El input no contiene el nombre esperado. input_value='{valor_input}' nombre='{nombre}'")
                        # Fallback: forzar valor via evaluate y disparar eventos
                        await page.evaluate("""(sel, val) => {
                          const el = document.querySelector(sel);
                          if (el) {
                            el.focus();
                            el.value = val;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                          }
                        }""", campo_nombre_id, nombre)
                        await asyncio.sleep(0.5)
                        valor_input2 = await page.locator(campo_nombre_id).input_value()
                        print(f"[RGM] Valor del input nombre tras fallback: '{valor_input2}'")
                    else:
                        print("[RGM] El campo nombre fue llenado correctamente.")
                except Exception as e:
                    print(f"[RGM][WARN] No se pudo leer input_value: {e}")

            except PlaywrightTimeoutError:
                print("[RGM][ERROR] Timeout esperando el campo de nombre")
                # guardar HTML de debug
                try:
                    relative_folder = os.path.join("resultados", str(consulta_id))
                    absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
                    os.makedirs(absolute_folder, exist_ok=True)
                    html_debug_path = os.path.join(absolute_folder, f"NOOFICIAL_{consulta_id}_debug_no_input.html")
                    content = await page.content()
                    with open(html_debug_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    print("[RGM] HTML de debug guardado en:", html_debug_path)
                except Exception as e:
                    print(f"[RGM][WARN] No se pudo guardar HTML de debug: {e}")

                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin Validar",
                    mensaje="No se encontró el campo de nombre tras activar 'Por nombre'",
                    archivo=""
                )
                await browser.close()
                return
            except Exception as e:
                print(f"[RGM][ERROR] Error al llenar el campo de nombre: {e}")
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin Validar",
                    mensaje=f"Error al llenar el campo de nombre: {e}",
                    archivo=""
                )
                await browser.close()
                return

            await asyncio.sleep(0.8)

            # 5 Botón correcto para “Consultar por nombre” (NO OFICIAL)
            btn_consultar_nombre = "#ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_btnConsultarNombre"
            print("[RGM] Buscando botón 'Consultar por nombre'")
            try:
                await page.wait_for_selector(btn_consultar_nombre, timeout=7000)
                # asegurar scroll al botón
                await page.evaluate("""(sel) => {
                  const el = document.querySelector(sel);
                  if (el) el.scrollIntoView({behavior:'instant', block:'center'});
                }""", btn_consultar_nombre)

                visible_btn = await page.locator(btn_consultar_nombre).is_visible()
                print(f"[RGM] Botón 'Consultar por nombre' visible: {visible_btn}")
                if not visible_btn:
                    raise RuntimeError("El botón 'Consultar por nombre' no está visible")

                await page.click(btn_consultar_nombre)
                print("[RGM] Click en Consultar por nombre realizado")
            except PlaywrightTimeoutError:
                print("[RGM][WARN] Botón por ID no encontrado, intentando fallback por texto")
                try:
                    botones = page.locator("button:has-text('Consultar'), input[type='submit'][value='Consultar'], a:has-text('Consultar')")
                    count = await botones.count()
                    print(f"[RGM] Botones con texto 'Consultar' encontrados: {count}")
                    if count:
                        await botones.nth(count - 1).click()
                        print("[RGM] Click en Consultar (fallback último botón) realizado")
                    else:
                        raise RuntimeError("No se encontró el botón Consultar.")
                except Exception as exc:
                    print(f"[RGM][ERROR] Error al intentar hacer clic en Consultar: {exc}")
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=fuente_obj,
                        score=0,
                        estado="Sin Validar",
                        mensaje=f"Error al intentar hacer clic en Consultar: {exc}",
                        archivo=""
                    )
                    await browser.close()
                    return
            except Exception as e:
                print(f"[RGM][ERROR] Error buscando o clickeando el botón Consultar: {e}")
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=0,
                    estado="Sin Validar",
                    mensaje=f"Error buscando o clickeando el botón Consultar: {e}",
                    archivo=""
                )
                await browser.close()
                return

            # esperar que la consulta termine
            try:
                print("[RGM] Esperando que termine la carga (networkidle)...")
                await page.wait_for_load_state("networkidle", timeout=15000)
                # esperar label de resultado para mayor certeza
                await page.wait_for_selector("#ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_lblMensajeBusqueda", timeout=8000)
                print("[RGM] networkidle y label de resultado detectados")
            except PlaywrightTimeoutError:
                print("[RGM][WARN] Timeout esperando networkidle o label de resultado; continuar de todas formas")
                await asyncio.sleep(2)

            await asyncio.sleep(1.0)

            # 6 Leer mensaje de resultado
            score = 6
            mensaje = "Consulta ejecutada correctamente."

            span = page.locator("#ContentPlaceHolderSeguridad_ContentPlaceHolderContenido_lblMensajeBusqueda")
            try:
                if await span.is_visible():
                    txt = (await span.inner_text()).strip()
                    print(f"[RGM] Texto en lblMensajeBusqueda: '{txt}'")
                    if txt:
                        score = 0
                        mensaje = txt
                else:
                    print("[RGM] lblMensajeBusqueda no visible; puede que los resultados estén en otra parte de la página")
            except Exception as e:
                print(f"[RGM][WARN] Error leyendo lblMensajeBusqueda: {e}")

            # 7 Guardar captura FINAL (después de la carga y lectura del resultado)
            relative_folder = os.path.join("resultados", str(consulta_id))
            absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
            os.makedirs(absolute_folder, exist_ok=True)

            nombre_archivo = f"NOOFICIAL_{consulta_id}.png"
            abs_path = os.path.join(absolute_folder, nombre_archivo)
            rel_path = os.path.join(relative_folder, nombre_archivo)

            print("[RGM] Tomando captura final en:", abs_path)
            try:
                await page.screenshot(path=abs_path, full_page=True)
                print("[RGM] Captura final tomada")
            except Exception as e:
                print(f"[RGM][WARN] Falló tomar screenshot: {e}")

            # Guardar HTML de debug también (útil para inspección)
            try:
                html_debug_path = os.path.join(absolute_folder, f"NOOFICIAL_{consulta_id}_debug.html")
                content = await page.content()
                with open(html_debug_path, "w", encoding="utf-8") as f:
                    f.write(content)
                print("[RGM] HTML de debug guardado en:", html_debug_path)
            except Exception as e:
                print(f"[RGM][WARN] No se pudo guardar HTML de debug: {e}")

            # 8 Registrar resultado (protegido)
            try:
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validada",
                    mensaje=mensaje,
                    archivo=rel_path
                )
                print(f"[RGM] Resultado guardado en BD: consulta_id={consulta_id}, archivo={rel_path}")
            except Exception as save_exc:
                print(f"[RGM][ERROR] Falló guardar Resultado para {consulta_id}: {save_exc}")
                # intentar guardar sin fuente si falla
                try:
                    await sync_to_async(Resultado.objects.create)(
                        consulta_id=consulta_id,
                        fuente=None,
                        score=score,
                        estado="Validada",
                        mensaje=f"{mensaje} (guardado sin fuente por error: {save_exc})",
                        archivo=rel_path
                    )
                    print(f"[RGM] Resultado guardado en BD sin fuente: consulta_id={consulta_id}, archivo={rel_path}")
                except Exception as db_exc:
                    print(f"[RGM][FATAL] No se pudo crear Resultado en BD: {db_exc}")

            await browser.close()
            print("[RGM] Navegador cerrado. Flujo finalizado correctamente.")

    except Exception as e:
        # CHANGED: asegurar que fuente_obj puede no existir
        fuente_para_guardar = locals().get('fuente_obj', None)
        try:
            await sync_to_async(Resultado.objects.create)(
                consulta_id=consulta_id,
                fuente=fuente_para_guardar,
                score=0,
                estado="Sin Validar",
                mensaje=f"Error durante consulta: {e}",
                archivo=""
            )
            print(f"[RGM][ERROR] Se registró Resultado de error para consulta {consulta_id}: {e}")
        except Exception as db_exc:
            print(f"[RGM][FATAL] No se pudo crear Resultado en BD para consulta {consulta_id}: {db_exc}")
            print(f"[RGM][FATAL] Error original: {e}")
