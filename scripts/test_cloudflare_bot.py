#!/usr/bin/env python
"""
Script para testear bots con Cloudflare Challenge.
Ejecuta el bot en modo visual para verificar que Cloudflare se resuelve.

Uso:
  python scripts/test_cloudflare_bot.py --bot embajada_alemania_funcionarios --nombre Joan --apellido Sotaquira
"""
import os
import sys
import pathlib
import django
import argparse

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
django.setup()

from django.contrib.auth import get_user_model
from core.models import Candidato, Consulta
import asyncio


def main():
    parser = argparse.ArgumentParser(description='Test bot con Cloudflare')
    parser.add_argument('--bot', required=True, help='Nombre del bot (ej: embajada_alemania_funcionarios)')
    parser.add_argument('--nombre', default='Test', help='Nombre a buscar')
    parser.add_argument('--apellido', default='Usuario', help='Apellido a buscar')
    parser.add_argument('--cedula', default='9999999999', help='Cédula (para bots que lo requieren)')
    parser.add_argument('--tipo', default='CC', help='Tipo de documento')
    parser.add_argument('--headless', default='false', help='Ejecutar headless (true/false)')
    
    args = parser.parse_args()
    
    # Variables de entorno
    os.environ['EMBAJADA_ALEMANIA_HEADLESS'] = args.headless
    os.environ['EMBAJADA_ALEMANIA_SLOW_MO'] = '50'  # Más lento para ver la acción
    
    print(f"\n🔍 Test Cloudflare Bot")
    print(f"   Bot: {args.bot}")
    print(f"   Headless: {args.headless}")
    print(f"   Nombre: {args.nombre} {args.apellido}")
    print(f"   Cédula: {args.cedula} ({args.tipo})\n")
    
    # Crear usuario y candidato
    User = get_user_model()
    user = User.objects.first() or User.objects.create_user(username='testbot', password='test123')
    
    candidato, created = Candidato.objects.get_or_create(
        cedula=args.cedula,
        defaults={
            'tipo_doc': args.tipo,
            'nombre': args.nombre,
            'apellido': args.apellido,
            'email': 'test@example.com',
            'tipo_persona': 'NATURAL'
        }
    )
    if created:
        print(f"✅ Candidato creado: {candidato.nombre} {candidato.apellido}")
    
    consulta = Consulta.objects.create(
        candidato=candidato,
        usuario=user,
        estado='en_prueba'
    )
    print(f"✅ Consulta creada: ID={consulta.id}")
    
    # Importar y ejecutar bot
    try:
        if args.bot == 'embajada_alemania_funcionarios':
            from core.bots.embajada_alemania_funcionarios import consultar_embajada_alemania_funcionarios
            
            print(f"\n▶️  Ejecutando bot...")
            asyncio.run(consultar_embajada_alemania_funcionarios(
                consulta_id=consulta.id,
                nombre=args.nombre,
                apellido=args.apellido
            ))
        else:
            print(f"❌ Bot no reconocido: {args.bot}")
            return
        
        # Mostrar resultado
        from core.models import Resultado
        resultado = Resultado.objects.filter(consulta=consulta).first()
        if resultado:
            print(f"\n✅ Resultado guardado:")
            print(f"   Estado: {resultado.estado}")
            print(f"   Score: {resultado.score}")
            print(f"   Mensaje: {resultado.mensaje[:100]}")
            if resultado.archivo:
                print(f"   Archivo: {resultado.archivo}")
        else:
            print(f"\n⚠️  No se encontró resultado")
            
        # Limpiar
        consulta.delete()
        print(f"\n✅ Consulta limpiada")
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        consulta.delete()


if __name__ == '__main__':
    main()
