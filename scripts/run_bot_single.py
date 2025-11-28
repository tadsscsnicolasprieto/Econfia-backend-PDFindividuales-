import os
import sys
import pathlib
import django
import argparse
import asyncio
import importlib
import inspect
from asgiref.sync import sync_to_async


# Asegurar root en sys.path
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
django.setup()

from django.contrib.auth import get_user_model
from core.models import Candidato, Consulta


def resolve_callable(bot_str: str):
    """Resuelve una cadena a una función.
    Soporta:
      - 'core.bots.adres.consultar_adres'
      - 'adres' -> intenta importar 'core.bots.adres' y buscar 'consultar_adres'
    """
    if '.' in bot_str and bot_str.count('.') >= 2:
        # assume full path to function
        module_path, func_name = bot_str.rsplit('.', 1)
        mod = importlib.import_module(module_path)
        func = getattr(mod, func_name)
        return func, module_path

    # try module under core.bots
    module_path = f"core.bots.{bot_str}"
    try:
        mod = importlib.import_module(module_path)
    except Exception:
        raise ImportError(f"No pude importar módulo '{module_path}'")

    # buscar función con nombre esperado
    cand_names = [f"consultar_{bot_str}", 'consultar', f"consultar_{bot_str.replace('-','_')}"]
    for name in cand_names:
        if hasattr(mod, name):
            return getattr(mod, name), module_path

    # fallback: buscar primera callable que empiece por 'consultar_'
    for attr in dir(mod):
        if attr.startswith('consultar_'):
            return getattr(mod, attr), module_path

    raise ImportError(f"No encontré función de consulta en módulo '{module_path}'")


async def call_bot(func, consulta_id, cedula, tipo_doc, fecha_expedicion=None, codigo_verificacion=None, nombre=None, apellido=None):
    """
    Llama al bot intentando mapear parámetros por nombre (legacy) o por posición.
    Ahora acepta y propaga fecha_expedicion, codigo_verificacion, nombre y apellido.
    """
    # Obtener la consulta REAL desde la BD, junto con candidato
    consulta = await sync_to_async(
        Consulta.objects.select_related("candidato").get
    )(id=consulta_id)

    candidato = consulta.candidato

    sig = inspect.signature(func)
    params = list(sig.parameters.keys())

    # Caso 1: Bot moderno → (consulta, candidato)
    if params == ["consulta", "candidato"] or params[:2] == ["consulta", "candidato"]:
        return await func(consulta, candidato)

    # Caso legacy → autodetectar parámetros por nombre
    kwargs = {}
    for name, param in sig.parameters.items():
        ln = name.lower()
        if ln in ('consulta_id', 'consulta'):
            kwargs[name] = consulta_id
        elif ln in ('cedula', 'numero', 'numero_doc', 'documento', 'numero_documento', 'numerodocumento'):
            kwargs[name] = cedula
        elif ln in ('tipo_doc', 'tipo', 'tipodoc', 'tipo_documento', 'tipodocumento'):
            kwargs[name] = tipo_doc
        elif ln in ('fecha_expedicion', 'fecha', 'fecha_expedicion_raw', 'fecha_expedicion_str'):
            kwargs[name] = fecha_expedicion
        elif ln in ('codigo_verificacion', 'codigo', 'codigo_v'):
            kwargs[name] = codigo_verificacion
        elif ln in ('nombre', 'first_name', 'nombre_persona'):
            kwargs[name] = nombre
        elif ln in ('apellido', 'last_name', 'apellido_persona', 'apellidos'):
            kwargs[name] = apellido

    try:
        if kwargs:
            return await func(**kwargs)
        else:
            return await func(consulta_id, cedula, tipo_doc)
    except TypeError:
        # fallback por posición, intentando añadir fecha y código si la función acepta más args
        params = list(sig.parameters)
        arity = len(params)
        args = []
        if arity >= 1: args.append(consulta_id)
        if arity >= 2: args.append(cedula)
        if arity >= 3: args.append(tipo_doc)
        if arity >= 4 and fecha_expedicion is not None:
            args.append(fecha_expedicion)
        if arity >= 5 and codigo_verificacion is not None:
            args.append(codigo_verificacion)
        # si la función acepta más posiciones, intentar añadir nombre/apellido
        if arity >= 6 and nombre is not None:
            args.append(nombre)
        if arity >= 7 and apellido is not None:
            args.append(apellido)
        return await func(*args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bot', required=True, help="Nombre del bot o ruta a función (ej: 'adres' o 'core.bots.adres.consultar_adres')")
    parser.add_argument('--cedula', required=True)
    parser.add_argument('--tipo', default='CC')
    parser.add_argument('--consulta_id', type=int, help='ID de la consulta (opcional)')
    parser.add_argument('--nombre', help="Nombre del candidato")
    parser.add_argument('--apellido', help="Apellido del candidato")
    parser.add_argument("--fecha_expedicion", required=False, help="Fecha de expedición (YYYY-MM-DD o DD/MM/YYYY o DD-MM-YYYY)")
    parser.add_argument('--headless', choices=['true','false'], help='Override headless')
    parser.add_argument('--slow-mo', type=int, help='Override slow_mo (ms)')
    parser.add_argument('--codigo_verificacion', required=False, help='Código de verificación (si aplica)')
    args = parser.parse_args()

    # resolver función
    func, module_path = resolve_callable(args.bot)

    # preparar user/candidato/consulta
    User = get_user_model()
    user = User.objects.first()
    if not user:
        user = User.objects.create_user(username='testbot_runner', password='test123')

    candidato, _ = Candidato.objects.get_or_create(
        cedula=args.cedula,
        defaults={'tipo_doc': args.tipo, 'nombre': 'Prueba', 'apellido': 'Runner'}
    )

    consulta = Consulta.objects.create(candidato=candidato, usuario=user, estado='pendiente')
    print(f"Creada consulta id={consulta.id} para cedula={args.cedula} (bot={args.bot})")

    # set env overrides: prefer explicit flags, else BOTNAME_HEADLESS
    botname_env = os.path.basename(module_path)
    if args.headless is not None:
        os.environ[f"{botname_env.upper()}_HEADLESS"] = args.headless
    if args.slow_mo is not None:
        os.environ[f"{botname_env.upper()}_SLOW_MO"] = str(args.slow_mo)

    # Ejecutar: pasar fecha_expedicion, codigo_verificacion, nombre y apellido a call_bot
    try:
        asyncio.run(call_bot(
            func,
            consulta.id,
            args.cedula,
            args.tipo,
            fecha_expedicion=args.fecha_expedicion,
            codigo_verificacion=args.codigo_verificacion,
            nombre=args.nombre,
            apellido=args.apellido
        ))
        print('Ejecución finalizada')
    except Exception as e:
        import traceback
        traceback.print_exc()
        print('Error durante la ejecución:', e)

    print('Hecho')


if __name__ == '__main__':
    main()