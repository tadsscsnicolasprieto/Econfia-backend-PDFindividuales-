#!/usr/bin/env python
"""
Test runner para bots: ejecuta cada bot sin resultados e identifica errores.
Uso: python test_bots_runner.py [--limit 10] [--output report.csv]
"""
import os
import sys
import json
import csv
import importlib
import asyncio
import traceback
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
import django
django.setup()

from core.models import Fuente, Resultado, Candidato, Consulta
from django.contrib.auth.models import User

# Datos de prueba
TEST_CEDULA = "1070386336"
TEST_TIPO_DOC = "CC"
TEST_NOMBRE = "TEST"
TEST_APELLIDO = "USER"
TEST_FECHA_EXPEDICION = "09-08-2018"

def get_bots_without_results():
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
    return zero_results

def test_bot_import(bot_name):
    """Intenta importar el módulo del bot y verifica que tenga función principal."""
    try:
        module = importlib.import_module(f'core.bots.{bot_name}')
        # Buscar función principal (puede ser consultar_<bot>, <bot>, etc.)
        func_names = ['consultar_' + bot_name, bot_name, 'ejecutar_' + bot_name]
        for fn in func_names:
            if hasattr(module, fn):
                return True, None
        return False, "No se encontró función principal"
    except Exception as e:
        return False, str(e)

def test_bot_execution(bot_name, consulta_id=999):
    """Intenta ejecutar el bot con datos de prueba mínimos."""
    try:
        module = importlib.import_module(f'core.bots.{bot_name}')
        # Buscar función principal
        func = None
        for fn in ['consultar_' + bot_name, bot_name, 'ejecutar_' + bot_name]:
            if hasattr(module, fn):
                func = getattr(module, fn)
                break
        if not func:
            return False, "No se encontró función principal"
        
        # Crear/obtener objeto Candidato para la prueba
        candidato, _ = Candidato.objects.get_or_create(
            cedula=TEST_CEDULA,
            defaults={
                'tipo_doc': TEST_TIPO_DOC,
                'nombre': TEST_NOMBRE,
                'apellido': TEST_APELLIDO,
            }
        )
        
        # Crear usuario/consulta si hace falta
        user, _ = User.objects.get_or_create(username='test_bot_runner')
        consulta, _ = Consulta.objects.get_or_create(
            id=consulta_id,
            defaults={
                'candidato': candidato,
                'usuario': user,
                'estado': 'test'
            }
        )
        
        # Ejecutar el bot (sincrónico o asincrónico)
        if asyncio.iscoroutinefunction(func):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(func(TEST_CEDULA, TEST_TIPO_DOC, TEST_NOMBRE, TEST_APELLIDO, consulta_id))
            finally:
                loop.close()
        else:
            result = func(TEST_CEDULA, TEST_TIPO_DOC, TEST_NOMBRE, TEST_APELLIDO, consulta_id)
        
        return True, "Ejecución completada"
    except Exception as e:
        tb = traceback.format_exc()
        return False, f"{str(e)[:200]}\n{tb[:300]}"

def run_tests(limit=None, output_file='bot_test_report.csv'):
    """Ejecuta pruebas en bots sin resultados y genera reporte."""
    bots = get_bots_without_results()
    if limit:
        bots = bots[:limit]
    
    print(f"🔍 Probando {len(bots)} bots sin resultados...")
    results = []
    
    for i, bot in enumerate(bots, 1):
        print(f"\n[{i}/{len(bots)}] {bot}... ", end='', flush=True)
        
        # Test 1: Import
        can_import, import_err = test_bot_import(bot)
        if not can_import:
            print(f"❌ IMPORT FAIL")
            results.append({
                'bot': bot,
                'test': 'import',
                'status': 'FAIL',
                'error': import_err,
                'timestamp': datetime.now().isoformat()
            })
            continue
        print(f"✓ import ", end='', flush=True)
        
        # Test 2: Execution (con timeout corto)
        can_exec, exec_err = test_bot_execution(bot, consulta_id=9000+i)
        if not can_exec:
            print(f"❌ EXEC FAIL")
            results.append({
                'bot': bot,
                'test': 'execution',
                'status': 'FAIL',
                'error': exec_err,
                'timestamp': datetime.now().isoformat()
            })
        else:
            print(f"✓ exec")
            results.append({
                'bot': bot,
                'test': 'execution',
                'status': 'PASS',
                'error': '',
                'timestamp': datetime.now().isoformat()
            })
    
    # Guardar reporte CSV
    if results:
        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['bot', 'test', 'status', 'error', 'timestamp'])
            writer.writeheader()
            writer.writerows(results)
        print(f"\n📊 Reporte guardado en: {output_file}")
    
    # Resumen
    passed = sum(1 for r in results if r['status'] == 'PASS')
    failed = sum(1 for r in results if r['status'] == 'FAIL')
    print(f"\n📈 RESUMEN: {passed} PASS, {failed} FAIL")
    return results

if __name__ == '__main__':
    limit = None
    output_file = 'bot_test_report.csv'
    
    if '--limit' in sys.argv:
        idx = sys.argv.index('--limit')
        limit = int(sys.argv[idx + 1])
    
    if '--output' in sys.argv:
        idx = sys.argv.index('--output')
        output_file = sys.argv[idx + 1]
    
    results = run_tests(limit=limit, output_file=output_file)
    sys.exit(0 if all(r['status'] == 'PASS' for r in results) else 1)
