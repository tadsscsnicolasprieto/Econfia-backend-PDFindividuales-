#!/usr/bin/env python
"""
Runner universal para ejecutar cualquier bot sin importar su firma.

Permite ejecutar bots que reciben:
- (consulta_id)
- (consulta_id, cedula)
- (consulta_id, cedula, tipo_doc)
- (consulta_id, nombre, apellido)
- (consulta_id, pasaporte)
- etc.

Detecta automáticamente los parámetros.
"""

import os
import sys
import pathlib
import django
import argparse
import asyncio
import importlib
import inspect

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from django.contrib.auth import get_user_model
from core.models import Candidato, Consulta


# --------------------------------------------------------
# Resolver módulo / función
# --------------------------------------------------------
def resolve_callable(bot_str: str):
    """
    Permite:
      python run_bot_single.py --bot adres
      python run_bot_single.py --bot core.bots.adres.consultar_adres
    """
    if "." in bot_str and bot_str.count(".") >= 2:
        module_path, func_name = bot_str.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        func = getattr(mod, func_name)
        return func, module_path

    module_path = f"core.bots.{bot_str}"
    try:
        mod = importlib.import_module(module_path)
    except Exception:
        raise ImportError(f"No pude importar módulo '{module_path}'")

    # funciones típicas
    for name in [
        f"consultar_{bot_str}",
        "consultar",
        f"consultar_{bot_str.replace('-', '_')}",
    ]:
        if hasattr(mod, name):
            return getattr(mod, name), module_path

    # fallback: primer 'consultar_'
    for attr in dir(mod):
        if attr.startswith("consultar_"):
            return getattr(mod, attr), module_path

    raise ImportError(f"No encontré función de consulta en '{module_path}'")


# --------------------------------------------------------
# Llamador universal
# --------------------------------------------------------
async def call_bot_dynamic(func, consulta_id, cedula, tipo_doc, nombre, apellido, fecha_expedicion=None):
    """
    Detecta la firma del bot y llena automáticamente los parámetros.
    Compatibilidad total con bots nuevos y viejos.
    """
    sig = inspect.signature(func)
    params = sig.parameters
    args = []

    # Mapa de nombres que se pueden llenar automáticamente
    value_map = {
        "consulta_id": consulta_id,
        "consulta": consulta_id,
        "cedula": cedula,
        "documento": cedula,
        "numero": cedula,
        "numero_doc": cedula,
        "tipo": tipo_doc,
        "tipo_doc": tipo_doc,
        "tipo_documento": tipo_doc,
        "nombre": nombre,
        "apellido": apellido,
        "nombres": nombre,
        "apellidos": apellido,
        "fecha_expedicion": fecha_expedicion,
        "fecha": fecha_expedicion,
    }

    # Llenar según orden declarado
    for pname in params:
        key = pname.lower()
        if key in value_map:
            args.append(value_map[key])
        else:
            # parámetros no reconocidos → None
            args.append(None)

    return await func(*args)


# --------------------------------------------------------
# MAIN
# --------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True)
    parser.add_argument("--cedula", default="")
    parser.add_argument("--tipo", default="CC")
    parser.add_argument("--nombre", default="Prueba")
    parser.add_argument("--apellido", default="Runner")
    parser.add_argument("--fecha-expedicion", default=None)
    parser.add_argument("--headless", choices=["true", "false"])
    parser.add_argument("--slow-mo", type=int)
    args = parser.parse_args()

    # resolver función
    func, module_path = resolve_callable(args.bot)

    # usuario para ejecutar
    User = get_user_model()
    user = User.objects.first() or User.objects.create_user("testbot_runner", "test123")

    # crear candidato
    candidato, _ = Candidato.objects.get_or_create(
        cedula=args.cedula or "0",
        defaults={"tipo_doc": args.tipo, "nombre": args.nombre, "apellido": args.apellido},
    )

    # crear consulta
    consulta = Consulta.objects.create(candidato=candidato, usuario=user, estado="pendiente")
    print(f"Creada consulta id={consulta.id} (bot={args.bot})")

    # Config de headless y slowmo
    bot_env = os.path.basename(module_path).upper()
    if args.headless:
        os.environ[f"{bot_env}_HEADLESS"] = args.headless
    if args.slow_mo:
        os.environ[f"{bot_env}_SLOW_MO"] = str(args.slow_mo)

    try:
        asyncio.run(
            call_bot_dynamic(
                func=func,
                consulta_id=consulta.id,
                cedula=args.cedula,
                tipo_doc=args.tipo,
                nombre=args.nombre,
                apellido=args.apellido,
                fecha_expedicion=args.fecha_expedicion,
            )
        )
        print("Ejecución finalizada correctamente.")
    except Exception as e:
        import traceback

        traceback.print_exc()
        print("Error durante la ejecución:", e)

    print("Hecho.")


if __name__ == "__main__":
    main()
