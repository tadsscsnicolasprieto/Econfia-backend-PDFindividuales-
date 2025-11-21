import os
from datetime import datetime
from playwright.async_api import async_playwright
from django.conf import settings
from asgiref.sync import sync_to_async
from core.models import Resultado, Fuente

# 🌍 URL principal
BASE_URL = "https://www.opensanctions.org/datasets/default/"

# 🔎 Diccionario de fuentes: clave (texto en la web) -> valor (nombre en BD)
FUENTES = {
    "Austria Public Officials": "autria_public_officials",
    "Canadian Listed Terrorist Entities": "canadian_listed_terrorist_entities",
    "Nepal Prohibited Persons or Groups according per National Strategy and Action Plan (2076-2081)": "nepal_prohibited_persons_groups",
    "Colombian PEP Declarations": "colombian_pep_declarations",
    "Colombian Joining the Dots PEPs": "colombian_joining_the_dots_peps",
    "ACF List of War Enablers": "acf_list_of_war_enablers",
    "Iran Sanctions List": "iran_sanctions_list",
    "Austria Public Officials": "austria_public_officials",
    "China Sanctions Research": "china_sanctions_research",
    "Ukraine SFMS Blacklist": "ukraine_sfms_blacklist",
    "US Colorado Medicaid Terminated Provider List": "us_colorado_medicaid_terminated_providers",
    "US Oregon State Medicaid Fraud Convictions": "us_oregon_medicaid_fraud_convictions",
    "US Pennsylvania Medicheck list": "us_pennsylvania_medicheck_list",
    "US Navy Leadership": "us_navy_leadership",
    "US Mississippi Medicaid Terminated Provider List": "us_mississippi_medicaid_terminated_providers",
    "US Missouri Medicaid Provider Terminations": "us_missouri_medicaid_provider_terminations",
    "US Maine Medicaid Excluded Providers": "us_maine_medicaid_excluded_providers",
    "US Maryland Sanctioned Providers": "us_maryland_sanctioned_providers",
    "US Federal Reserve Enforcement Actions": "us_federal_reserve_enforcement_actions",
    "US FinCEN 311 and 9714 Special Measures": "us_fincen_special_measures",
    "US FINRA Enforcement Actions": "us_finra_enforcement_actions",
    "US Georgia Healthcare provider exclusions": "us_georgia_healthcare_exclusions",
    "US Hawaii Medicaid Exclusions and Reinstatements": "us_hawaii_medicaid_exclusions_reinstatements",
    "US Health and Human Sciences Inspector General Exclusions": "us_hhs_inspector_general_exclusions",
    "US Immigration and Customs Enforcement Most Wanted Fugitives": "us_ice_most_wanted_fugitives",
    "US Indiana Medicaid Terminated Provider List": "us_indiana_medicaid_terminated_providers",
    "US Iowa Medicaid Terminated Provider List": "us_iowa_medicaid_terminated_providers",
    "US Kansas Medicaid Terminated Provider List": "us_kansas_medicaid_terminated_providers",
    "US Delaware Medicaid Sanctioned Providers": "us_delaware_medicaid_sanctioned_providers",
    "US Department of State Foreign Terrorist Organizations": "us_state_foreign_terrorist_organizations",
    "US Department of State Terrorist Exclusion": "us_state_terrorist_exclusion",
    "US Directorate of Defense Trade Controls AECA Debarments": "us_ddtc_aeca_debarments",
    "US Directorate of Defense Trade Controls Penalties & Oversight Agreements": "us_ddtc_penalties_oversight_agreements",
    "US DoD Chinese military companies": "us_dod_chinese_military_companies",
    "South Africa Wanted Persons": "south_africa_wanted_persons",
    "Romania FIU Public Officials": "romania_fiu_public_officials",
    "Russian PMC Wagner mercenaries (Myrotvorets list)": "russia_pmc_wagner_mercenaries",
    "Brazil List of Debarred Bidders": "brazil_debarred_bidders",
    "Estonia International Sanctions Act List": "estonia_international_sanctions_act_list",
    "Venezuela Members of the National Assembly": "venezuela_national_assembly_members",
    "Asian Development Bank Sanctions": "asian_development_bank_sanctions",
}


async def consultar_fuentes(consulta_id: int, cedula: str, nombre_persona: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(BASE_URL)

        # Recorremos las fuentes
        for clave_web, nombre_bd in FUENTES.items():
            try:
                # Buscar el link por el texto (ej: "Austria Public Officials")
                link = await page.query_selector(f'a:has-text("{clave_web}")')
                if not link:
                    print(f"❌ No encontré el link de {clave_web}")
                    continue

                # Abrir en nueva pestaña
                href = await link.get_attribute("href")
                new_page = await browser.new_page()
                await new_page.goto(f"https://www.opensanctions.org{href}")

                # Buscar input de búsqueda y escribir el nombre
                await new_page.fill("input[name='q']", nombre_persona)
                await new_page.click("button[type='submit']")

                # Esperar respuesta
                await new_page.wait_for_load_state("networkidle")

                # Verificar resultados
                no_match = await new_page.query_selector("div.alert-heading.h4")
                if no_match:
                    mensaje = await no_match.inner_text()
                    score = 1
                else:
                    # Contar coincidencias exactas en toda la página
                    matches = await new_page.query_selector_all(f"text={nombre_persona}")
                    num_matches = len(matches)
                    if num_matches >= 2:
                        mensaje = f"Encontradas {num_matches} coincidencias para {nombre_persona}"
                        score = 5
                    else:
                        mensaje = f"Solo {num_matches} coincidencia(s) encontradas."
                        score = 1

                # Guardar pantallazo
                relative_folder = os.path.join("resultados", str(consulta_id))
                absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
                os.makedirs(absolute_folder, exist_ok=True)

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                png_name = f"{nombre_bd}_{consulta_id}_{cedula}_{timestamp}.png"
                absolute_png = os.path.join(absolute_folder, png_name)
                relative_png = os.path.join(relative_folder, png_name)

                await new_page.screenshot(path=absolute_png, full_page=True)

                # Guardar en BD
                fuente_obj = await sync_to_async(Fuente.objects.get)(nombre=nombre_bd)
                await sync_to_async(Resultado.objects.create)(
                    consulta_id=consulta_id,
                    fuente=fuente_obj,
                    score=score,
                    estado="Validado",
                    mensaje=mensaje,
                    archivo=relative_png,
                )

                print(f"✅ Guardado resultado de {clave_web} - {mensaje}")

                # Cerrar pestaña
                await new_page.close()

            except Exception as e:
                print(f"⚠️ Error en {clave_web}: {e}")

        await browser.close()
