#!/usr/bin/env python
import os
import sys
import pathlib
import django
import asyncio

# Ensure project root is on sys.path so Django can import settings
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
django.setup()

from django.contrib.auth import get_user_model
from core.models import Candidato, Consulta
from core.bots import adres

USER_USERNAME = 'testbot_run_adres'
TEST_CEDULA = '1070386336'
TEST_TIPO = 'CC'

# Obtener o crear usuario
User = get_user_model()
user = User.objects.first()
if not user:
    user = User.objects.create_user(username=USER_USERNAME, password='test123')

# Crear candidato
candidato, _ = Candidato.objects.get_or_create(
    cedula=TEST_CEDULA,
    defaults={
        'tipo_doc': TEST_TIPO,
        'nombre': 'Prueba',
        'apellido': 'Adres'
    }
)

# Crear consulta
consulta = Consulta.objects.create(candidato=candidato, usuario=user, estado='pendiente')
print(f"Creada consulta id={consulta.id} para cedula={TEST_CEDULA}")

# Ejecutar bot
try:
    asyncio.run(adres.consultar_adres(consulta.id, TEST_CEDULA, TEST_TIPO))
    print('Ejecución finalizada')
except Exception as e:
    import traceback
    traceback.print_exc()
    print('Error durante la ejecución:', e)

print('Hecho')
