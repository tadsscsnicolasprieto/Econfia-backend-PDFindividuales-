from .cpae_certificado import consultar_cpae_certificado
from .cpae_verify_licensure import consultar_cpae_verify_licensure
from.cpae_verify_certification import consultar_cpae_verify_certification
from.rama_vigencias_pdf import consultar_rama_vigencias_pdf
from .cpqcol_verificar import consultar_cpqcol_verificar
from.sirna_inscritos_png import consultar_sirna_inscritos_png
from.sirna_sanciones_png import consultar_sirna_sanciones_png
from.cpiq_certificado_vigencia import consultar_cpiq_certificado_vigencia
from.cpiq_validacion_matricula import consultar_cpiq_validacion_matricula
from.cpiq_validacion_tarjeta import consultar_cpiq_validacion_tarjeta
from .cpiq_validacion_certificado_vigencia import consultar_cpiq_validacion_certificado_vigencia
from .copnia_certificado import consultar_copnia_certificado
from.cpqcol_antecedentes import consultar_cpqcol_antecedentes
from.conalpe_consulta_inscritos import consultar_conalpe_consulta_inscritos
from.conalpe_certificado import consultar_conalpe_certificado
from.colpsic_verificacion_tarjetas import consultar_colpsic_verificacion_tarjetas
from.colpsic_validar_documento import consultar_colpsic_validar_documento
from.cnb_carnet_afiliacion import consultar_cnb_carnet_afiliacion
from.cnb_consulta_matriculados import consultar_cnb_consulta_matriculados
from.colelectro_directorio import consultar_colelectro_directorio
from.conpucol_verificacion_colegiados import consultar_conpucol_verificacion_colegiados
from.conpucol_certificados import consultar_conpucol_certificados
from.cp_validar_matricula import consultar_cp_validar_matricula
from.cp_validar_certificado import consultar_cp_validar_certificado
from.cp_certificado_busqueda import consultar_cp_certificado_busqueda
from.cpip_verif_matricula import consultar_cpip_verif_matricula
from.conte_consulta_vigencia import consultar_conte_consulta_vigencia
from.conte_consulta_matricula import consultar_conte_consulta_matricula
from.cpnt_vigenciapdf import consultar_cpnt_vigenciapdf
from.cpnt_vigencia_externa_form import consultar_cpnt_vigencia_externa_form
from.cpnt_consulta_licencia import consultar_cpnt_consulta_licencia
from.cpnaa_matricula_arquitecto import consultar_cpnaa_matricula_arquitecto
from.cpnaa_certificado_vigencia import consultar_cpnaa_certificado_vigencia
from.conaltel_consulta_matriculados import consultar_conaltel_consulta_matriculados
from.cpaa_generar_certificado import consultar_cpaa_generar_certificado
from.ccap_validate_identity import consultar_ccap_validate_identity
from.biologia_consulta import consultar_biologia_consulta
from.biologia_validacion_certificados import consultar_biologia_validacion_certificados
from.secop_consulta_aacs import consultar_secop_consulta_aacs
from.colombiacompra_procesos import consultar_colombiacompra_procesos
from .ruaf import consultar_ruaf
from .adres import consultar_adres
from .procuraduria import consultar_procuraduria
from .personeria import consultar_personeria
from .policia_nacional import consultar_policia_nacional
from .rnmc import consultar_rnmc
from .inhabilidades import consultar_inhabilidades
from .libreta_militar import consultar_libreta_militar
from.porvenir_cert_afiliacion import consultar_porvenir_cert_afiliacion
from.colpensiones_rpm import consultar_colpensiones_rpm
from.bancoproveedores_quien_consulto import consultar_quien_consulto
from.procuraduria_generar_certificado import generar_certificado_procuraduria
from .contraloria import consultar_contraloria
from.rama_abogado_certificado import consultar_rama_abogado_certificado
from.sideap_comprobante import consultar_sideap_comprobante


nro_bien="123456"
empresa ="SCS SOLUCIONES GROUP"
nit = "830512262-1"

def get_bot_configs_contratista(consulta_id, datos):
    return [
        {
            'name': 'sideap_comprobante',
            'func': consultar_sideap_comprobante,
            'kwargs': {
                'consulta_id': consulta_id,
                'numero': datos['cedula'],
                'tipo_doc': datos['tipo_doc'],
                'fecha_nacimiento': datos['fecha_nacimiento'],
                'correo': datos['email'],
            }
        },
        {
            'name': 'rama_abogado_certificado',
            'func': consultar_rama_abogado_certificado,
            'kwargs': {
                'consulta_id': consulta_id,
                'cedula': datos['cedula'],
                'tipo_doc': datos['tipo_doc'],
            }
        },
            {
            "name":"ccap_validate_identity",
            "func": consultar_ccap_validate_identity,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],
            }
        },
        {
            "name":"biologia_consulta",
            "func": consultar_biologia_consulta,
            "kwargs": {
                "consulta_id": consulta_id,
                "cedula": datos["cedula"],
            }
        },
        {
            "name":"biologia_validacion_certificados",
            "func": consultar_biologia_validacion_certificados,
            "kwargs": {
                "consulta_id": consulta_id,
                "codigo": datos["cedula"],
            }
        },

        {
            "name":"cpiq_certificado_vigencia",
            "func": consultar_cpiq_certificado_vigencia,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"], 
            }
        },
        {
            "name":"cpiq_validacion_matricula",
            "func": consultar_cpiq_validacion_matricula,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],  
            }
        },
        {
            "name":"cpiq_validacion_tarjeta",
            "func": consultar_cpiq_validacion_tarjeta,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],
            }
        },
        {
            "name":"cpiq_validacion_certificado_vigencia",
            "func": consultar_cpiq_validacion_certificado_vigencia,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],  # la cÃ©dula que se debe verificar
            }
        },
        {
            'name':"cpqcol_verificar,",
            "func": consultar_cpqcol_verificar,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],  # usa la cÃ©dula mientras tanto
            }
        },
        {
            "name":"cpqcol_antecedentes",
            "func": consultar_cpqcol_antecedentes,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],  
            }
        },
        {
            "name":"conalpe_consulta_inscritos",
            "func": consultar_conalpe_consulta_inscritos,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],
            }
        },
        {
            "name":"conalpe_certificado",
            "func": consultar_conalpe_certificado,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],
            }
        },
        {
            "name":"colpsic_verificacion_tarjeta",
            "func": consultar_colpsic_verificacion_tarjetas,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],
                "primer_nombre": datos["nombre"],
                "primer_apellido": datos["apellido"],
            }
        },
        {
            "name":"colpsic_validar_documento",
            "func": consultar_colpsic_validar_documento,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],         
                "codigo": datos["cedula"],   
            }
        },
        {
            "name":"cnb_carnet_afiliacion",
            "func": consultar_cnb_carnet_afiliacion,
            "kwargs": {
                "consulta_id": consulta_id,
                "tipo_doc": datos["tipo_doc"],  
                "numero": datos["cedula"],    
            }
        },
        {
            "name":"cnb_consulta_matriculados",
            "func": consultar_cnb_consulta_matriculados,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],   # usamos cÃ©dula como â€œNÃºmero de Tarjetaâ€ temporalmente
            }
        },
        {
            "name":"colelectro_directorio",
            "func": consultar_colelectro_directorio,
            "kwargs": {
                "consulta_id": consulta_id,
                "nombre": datos["nombre"],      
                "apellido": datos["apellido"],   
            }
        },
        {
            "name":"conpucol_verificacion_colegiados",
            "func": consultar_conpucol_verificacion_colegiados,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],      
            }
        },
        {
            "name":"conpucol_certificados",
            "func": consultar_conpucol_certificados,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],    
            }
        }, 
        {
            "name":"cp_validar_matricula",
            "func": consultar_cp_validar_matricula,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],
            }
        },
        {
            "name":"cp_validar_certificado",
            "func": consultar_cp_validar_certificado,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],   # usamos cÃ©dula como 'clave' temporalmente
            }
        },
        {
            "name":"cp_certificado_busqueda",
            "func": consultar_cp_certificado_busqueda,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],
            }
        },
        {
            "name":"cpip_verif_matricula",
            "func": consultar_cpip_verif_matricula,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],
            }
        },
        {
            "name":"conte_consulta_vigencia",
            "func": consultar_conte_consulta_vigencia,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],
            }
        },
        {
            "name":"conte_consulta_matricula",
            "func": consultar_conte_consulta_matricula,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],
            }
        },
        {
            "name":"cpnt_vigenciapdf",
            "func": consultar_cpnt_vigenciapdf,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],   # usamos la cÃ©dula como radicado
            }
        },
        {
            "name":"cpnt_vigencia_externa_form",
            "func": consultar_cpnt_vigencia_externa_form,
            "kwargs": {
                "consulta_id": consulta_id,
                "tipo_doc": datos["tipo_doc"],    # "CC" | "CE" | "Pasaporte" | "TI" (o 1/2/3/4)
                "numero": datos["cedula"],
            }
        },
        {
            "name":"cpnt_consulta_licencia",
            "func": consultar_cpnt_consulta_licencia,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],
            }
        },
        {
            "name":"cpnaa_matricula_arquitecto",
            "func": consultar_cpnaa_matricula_arquitecto,
            "kwargs": {
                "consulta_id": consulta_id,
                "tipo_doc": datos["tipo_doc"],   # "CC" | "CE" | "PASAPORTE" | "PPT"  (o 1/2/5/25)
                "numero": datos["cedula"],
            }
        },
        {
            "name":"cpnaa_certificado_vigencia",
            "func": consultar_cpnaa_certificado_vigencia,
            "kwargs": {
                "consulta_id": consulta_id,
                "tipo_doc": datos["tipo_doc"],   # "CC" | "CE" | "PASAPORTE" | "PPT" o 1/2/5/25
                "numero": datos["cedula"],
            }
        },
        {
            "name":"conaltel_consulta_matriculados",
            "func": consultar_conaltel_consulta_matriculados,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],
            }
        },
        {
            'name':'copnia_certificado',
            'func': consultar_copnia_certificado,
            'kwargs': {
               'consulta_id': consulta_id,
                'cedula': datos['cedula'],
                'tipo_doc':datos['tipo_doc'],
            }
        },
        {
            'name':'cpae_certificado',
            'func': consultar_cpae_certificado,
            'kwargs': {
               'consulta_id': consulta_id,
                'tipo_doc': datos['tipo_doc'],     
                'cedula': datos['cedula'],
            }
        },
        {
            'name':'cpae_verify_licensure',
            'func': consultar_cpae_verify_licensure,
            'kwargs': {
               'consulta_id': consulta_id,
                'tipo_doc': datos['tipo_doc'],   # 'CC' o 'CE'
                'cedula': datos['cedula'],
                'nombre': f"{datos.get('nombre', '')} {datos.get('apellido', '')}".strip(),
            }
        },
        {
            'name':'cpae_verify_certification',
            'func': consultar_cpae_verify_certification,
            'kwargs': {
               'consulta_id': consulta_id,
                'cedula': datos['cedula'],
                'nombre': f"{datos.get('nombre', '')} {datos.get('apellido', '')}".strip(),
            }
        },
        {
            'name':'cpaa_generar_certificado',
            "func": consultar_cpaa_generar_certificado,
            "kwargs": {
                "consulta_id": consulta_id,
                "tipo_doc": datos["tipo_doc"],      
                "numero": datos["cedula"],         
            }
         },
        {
            'name':'sirna_sanciones_png',
            "func": consultar_sirna_sanciones_png,
            "kwargs": {
                "consulta_id": consulta_id,
                "tipo_doc": datos["tipo_doc"],      
                "numero": datos["cedula"],         
            }
         },
         {
            'name':'sirna_sanciones_png',
            "func": consultar_sirna_inscritos_png,
            "kwargs": {
                "consulta_id": consulta_id,
                "tipo_doc": datos["tipo_doc"],      
                "numero": datos["cedula"],         
            }
         },
        {
            'name':'policia_nacional',
            'func': consultar_policia_nacional,
            'kwargs': {
                'consulta_id':consulta_id,
                'cedula': datos['cedula'],
                'tipo_doc': datos['tipo_doc'],
            }
        },
        {
             'name':'rnmc',
            'func': consultar_rnmc,
            'kwargs': {
                'cedula': datos['cedula'],
                'tipo_doc': datos['tipo_doc'],
                'fecha_expedicion': datos['fecha_expedicion'],
                'consulta_id': consulta_id
            }
        },
       {
             'func': consultar_inhabilidades,
                'name':'inhabilidades',
                'kwargs': {
                   'consulta_id': consulta_id,
                    'cedula': datos['cedula'],
                    'tipo_doc': datos['tipo_doc'],
                    'fecha_exp': datos['fecha_expedicion'],
                    'empresa': empresa,
                    'nit':nit,
                }
         },
        {
            'name':'libreta_militar',
             'func': consultar_libreta_militar,
                'kwargs': {
                   'consulta_id': consulta_id, 
                    'cedula': datos['cedula'],
                    'tipo_doc': datos['tipo_doc'],
                }
        },
        {
            'name':'personeria',
            'func': consultar_personeria,
            'kwargs': {
       'consulta_id': consulta_id,
                'cedula': datos['cedula'],
                'tipo_doc': datos['tipo_doc'],
                'fecha_expedicion': datos['fecha_expedicion']
            }
        },
        {
            'name':"ruaf",
            'func': consultar_ruaf,
            'kwargs': {
       'consulta_id': consulta_id,
                'cedula': datos['cedula'],
                'tipo_doc': datos['tipo_doc'],
                'fecha_expedicion': datos['fecha_expedicion']
            }
        },
        #  {
        #      'name':"adres",
        #      "func": consultar_adres,
        #      "kwargs": {
        #          "consulta_id": consulta_id,   # ID numÃ©rico
        #          "cedula": datos["cedula"],
        #          "tipo_doc": datos["tipo_doc"],
        #      }
        #  },

        {
            "name": "porvenir_cert_afiliacion",
            "func": consultar_porvenir_cert_afiliacion,
            "kwargs": {
                "consulta_id": consulta_id,
                "cedula": datos["cedula"],
                "tipo_doc": datos["tipo_doc"],  # "CC" | "CE" | "TI"
            },
        },
        {
            'name':'rama_vigencias',
            'func': consultar_rama_vigencias_pdf,
            'kwargs': {
                'consulta_id': consulta_id,
                'numero': datos['cedula'],
                'tipo_doc': datos['tipo_doc'],
            }
        },
        {
            "name": "colpensiones_rpm",
            "func": consultar_colpensiones_rpm,
            "kwargs": {
                "consulta_id": consulta_id,
                "cedula": datos["cedula"],
                "tipo_doc": datos["tipo_doc"],
            }
        },
        {
            'name': 'banco_proveedores_consulta_estados',
            'func': consultar_quien_consulto,
            'kwargs': {
                'consulta_id': consulta_id,
                'numero': datos['cedula'],      # o datos['numero_doc']
                'tipo_doc': datos['tipo_doc'],  # 'CC','CE','PEP','PPT'
            }
        },
        {
            'name':'procuraduria_certificado',
            'func': generar_certificado_procuraduria,
            'kwargs': {
            'consulta_id': consulta_id,
                'cedula': datos['cedula'],
                'tipo_doc': datos['tipo_doc']
            }
        },
        {
            'name':'secop_consulta_aacs',
            "func": consultar_secop_consulta_aacs,
            "kwargs": {
                "consulta_id": consulta_id,
                "nombre": f"{datos.get('nombre', '')} {datos.get('apellido', '')}".strip(),
                "tipo_doc": datos["tipo_doc"],                       
                "numero": datos["cedula"],                            
            }
        },
        {
             'name':'contraloria',
             'func': consultar_contraloria,   
             "kwargs": {
                 "consulta_id": consulta_id,
                 "cedula": datos["cedula"],
                 "tipo_doc": datos["tipo_doc"],
             }
          },
        {
            'name': 'colombiacompra_procesos',
            "func": consultar_colombiacompra_procesos,
            "kwargs": {
                "consulta_id": consulta_id,
                "numero": datos["cedula"],
            }
        },
]
