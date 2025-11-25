#!/usr/bin/env python
"""Runner genérico para ejecutar un solo bot para pruebas.

Uso:
  python scripts/run_bot_single.py --bot adres --cedula 1070386336 --tipo CC
  python scripts/run_bot_single.py --bot core.bots.adres.consultar_adres --cedula 1070386336 --tipo CC

Soporta variables de entorno por bot: <BOTNAME>_HEADLESS y <BOTNAME>_SLOW_MO
o banderas `--headless` / `--slow-mo`.
"""
import os
import sys
import pathlib
import django
import argparse
import asyncio
import importlib
import inspect

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


async def call_bot(func, consulta_id, cedula, tipo_doc):
    sig = inspect.signature(func)
    kwargs = {}
    for name, param in sig.parameters.items():
        ln = name.lower()
        if ln in ('consulta_id', 'consulta'):
            kwargs[name] = consulta_id
        elif ln in ('cedula', 'numero', 'numero_doc', 'documento', 'numero_documento', 'numerodocumento'):
            kwargs[name] = cedula
        elif ln in ('tipo_doc', 'tipo', 'tipodoc', 'tipo_documento', 'tipodocumento'):
            kwargs[name] = tipo_doc
        else:
            # no tenemos valor para ese parámetro, omitir
            pass

    # Si la firma no acepta kwargs (p.ej. solo parámetros posicionales), intentar llamada posicional simple
    try:
        if kwargs:
            return await func(**kwargs)
        else:
            # intento posicional con los 3 valores comunes
            return await func(consulta_id, cedula, tipo_doc)
    except TypeError:
        # último recurso: llamada posicional con 1-3 args según aridad
        params = list(sig.parameters)
        arity = len(params)
        args = []
        if arity >= 1:
            args.append(consulta_id)
        if arity >= 2:
            args.append(cedula)
        if arity >= 3:
            args.append(tipo_doc)
        return await func(*args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bot', required=True, help="Nombre del bot o ruta a función (ej: 'adres' o 'core.bots.adres.consultar_adres')")
    parser.add_argument('--cedula', required=True)
    parser.add_argument('--tipo', default='CC')
    parser.add_argument('--headless', choices=['true','false'], help='Override headless')
    parser.add_argument('--slow-mo', type=int, help='Override slow_mo (ms)')
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

    # Ejecutar
    try:
        asyncio.run(call_bot(func, consulta.id, args.cedula, args.tipo))
        print('Ejecución finalizada')
    except Exception as e:
        import traceback
        traceback.print_exc()
        print('Error durante la ejecución:', e)

    print('Hecho')


if __name__ == '__main__':
    main()
