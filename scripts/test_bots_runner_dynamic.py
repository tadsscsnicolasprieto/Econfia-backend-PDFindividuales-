#!/usr/bin/env python
"""
Test runner mejorado que se adapta a diferentes firmas de funciones.
Detecta dinámicamente cuántos parámetros espera cada bot y ejecuta en consecuencia.
"""
import os
import sys
import inspect
import importlib
import asyncio
import csv
import traceback
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
import django
django.setup()

from core.models import Consulta, Fuente, Resultado, Candidato, Perfil
import argparse

# Datos de prueba
TEST_CEDULA = "9999999999"
TEST_TIPO_DOC = "CC"
TEST_NOMBRE = "TestBot"
TEST_APELLIDO = "Runner"
TEST_EMAIL = "test@example.com"
TEST_PHONE = "3001234567"
TEST_USER = None  # Se cargará al inicio

def get_bots_without_results(limit=None):
    """Retorna lista de bots sin Resultado registrado."""
    bots_dir = os.path.join(PROJECT_ROOT, 'core', 'bots')
    bot_files = sorted([f for f in os.listdir(bots_dir) if f.endswith('.py') and not f.startswith('__')])
    bot_names = [os.path.splitext(f)[0] for f in bot_files]
    
    zero_results = []
    for bot in bot_names:
        f = Fuente.objects.filter(nombre=bot).first()
        if f:
            cnt = Resultado.objects.filter(fuente=f).count()
            if cnt == 0:
                zero_results.append(bot)
    
    if limit:
        zero_results = zero_results[:limit]
    return zero_results

def find_main_function(module, bot_name):
    """Busca la función principal en el módulo."""
    func_candidates = [
        'consultar_' + bot_name,
        'ejecutar_' + bot_name,
        bot_name,
        'main'
    ]
    
    for fn_name in func_candidates:
        if hasattr(module, fn_name):
            obj = getattr(module, fn_name)
            if callable(obj):
                return obj, fn_name
    
    return None, None

def test_bot(bot_name, test_num=1):
    """Prueba un bot individual (versión sincrónica)."""
    try:
        # Importar módulo
        module = importlib.import_module(f'core.bots.{bot_name}')
        func, func_name = find_main_function(module, bot_name)
        
        if not func:
            return {
                'bot': bot_name,
                'test': 'import',
                'status': 'FAIL',
                'error': 'No se encontró función principal'
            }
        
        # Obtener firma
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())
        param_count = len(params)
        
        # Crear o recuperar candidato
        candidato, _ = Candidato.objects.get_or_create(
            cedula=TEST_CEDULA,
            defaults={
                'tipo_doc': TEST_TIPO_DOC,
                'nombre': TEST_NOMBRE,
                'apellido': TEST_APELLIDO,
                'email': TEST_EMAIL,
                'tipo_persona': 'NATURAL'
            }
        )
        
        # Crear consulta temporal
        consulta = Consulta.objects.create(
            candidato=candidato,
            usuario=TEST_USER,
            estado='en_prueba'
        )
        
        # Preparar argumentos según la firma detectada
        args = []
        for param in params:
            if param == 'cedula':
                args.append(TEST_CEDULA)
            elif param == 'tipo_doc' or param == 'tipo_documento':
                args.append(TEST_TIPO_DOC)
            elif param == 'nombre' or param == 'nombres':
                args.append(TEST_NOMBRE)
            elif param == 'apellido' or param == 'apellidos':
                args.append(TEST_APELLIDO)
            elif param == 'consulta_id':
                args.append(consulta.id)
            elif param == 'consulta':
                args.append(consulta)
            elif param == 'email':
                args.append(TEST_EMAIL)
            elif param == 'phone' or param == 'telefono':
                args.append(TEST_PHONE)
            elif param == 'codigo' or param == 'numero':
                args.append("1234")
            elif param == 'max_hits':
                args.append(10)
            elif param == 'primer_nombre' or param == 'primer_apellido':
                args.append("Test")
            elif param == 'tipo_persona':
                args.append("NATURAL")
            else:
                # Parámetro desconocido, pasar valor por defecto
                args.append(None)
        
        # Ejecutar función
        if asyncio.iscoroutinefunction(func):
            # Ejecutar función async
            loop = None
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Si hay un loop corriendo, no podemos usarlo
                    result = asyncio.run(func(*args))
                else:
                    result = loop.run_until_complete(func(*args))
            except RuntimeError:
                # Crear nuevo loop
                result = asyncio.run(func(*args))
        else:
            result = func(*args)
        
        consulta.delete()
        
        return {
            'bot': bot_name,
            'test': 'execution',
            'status': 'PASS',
            'function': func_name,
            'signature': str(sig),
            'param_count': param_count
        }
    
    except Exception as e:
        return {
            'bot': bot_name,
            'test': 'execution',
            'status': 'FAIL',
            'error': str(e)[:100],
            'error_type': type(e).__name__
        }

def main(args):
    global TEST_USER
    from django.contrib.auth import get_user_model
    
    # Obtener o crear un usuario para las pruebas
    User = get_user_model()
    try:
        TEST_USER = User.objects.first()  # Usar primer usuario disponible
        if not TEST_USER:
            TEST_USER = User.objects.create_user(username='testbot', password='test123')
    except:
        TEST_USER = None
    
    bots = get_bots_without_results(limit=args.limit)
    print(f"Probando {len(bots)} bots...\n")
    
    results = []
    passed = 0
    failed = 0
    import_failed = 0
    
    for i, bot in enumerate(bots, 1):
        result = test_bot(bot, i)
        results.append(result)
        
        status = result.get('status')
        test_type = result.get('test')
        symbol = '✓' if status == 'PASS' else '❌'
        sig = result.get('signature', 'N/A')[:60]
        error = result.get('error', '')[:50]
        
        if status == 'PASS':
            passed += 1
            print(f"{i:2}. {symbol} {bot:40} | {sig}")
        else:
            failed += 1
            if test_type == 'import':
                import_failed += 1
            print(f"{i:2}. {symbol} {bot:40} | {error}")
    
    # Escribir CSV
    with open(args.output, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['bot', 'test', 'status', 'function', 'signature', 'param_count', 'error', 'error_type', 'timestamp'])
        writer.writeheader()
        for r in results:
            r['timestamp'] = datetime.utcnow().isoformat()
            writer.writerow(r)
    
    print(f"\n📊 RESULTADOS:")
    print(f"  ✓ Pasaron: {passed}/{len(bots)}")
    print(f"  ❌ Fallaron: {failed}/{len(bots)}")
    print(f"     - Import failed: {import_failed}")
    print(f"     - Execution failed: {failed - import_failed}")
    print(f"  📄 Reporte: {args.output}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Test runner para bots con detección dinámica de firmas')
    parser.add_argument('--limit', type=int, default=10, help='Límite de bots a probar')
    parser.add_argument('--output', default='test_results_dynamic.csv', help='Archivo de salida CSV')
    args = parser.parse_args()
    
    main(args)
