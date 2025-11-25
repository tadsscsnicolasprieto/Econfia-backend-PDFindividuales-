#!/usr/bin/env python
"""
Test runner rápido que solo verifica las firmas sin ejecutar el navegador.
Útil para diagnosticar qué bots pueden importarse y ejecutarse.
"""
import os
import sys
import inspect
import importlib
import csv
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
import django
django.setup()

from core.models import Fuente, Resultado
import argparse

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

def test_bot_signature_only(bot_name):
    """Prueba solo la firma de un bot, sin ejecutar."""
    try:
        # Importar módulo
        module = importlib.import_module(f'core.bots.{bot_name}')
        func, func_name = find_main_function(module, bot_name)
        
        if not func:
            return {
                'bot': bot_name,
                'status': 'NO_FUNC',
                'error': 'No se encontró función principal'
            }
        
        # Obtener firma
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())
        param_count = len(params)
        
        return {
            'bot': bot_name,
            'status': 'OK',
            'function': func_name,
            'signature': str(sig),
            'param_count': param_count,
            'is_async': 'async' if inspect.iscoroutinefunction(func) else 'sync'
        }
    
    except ImportError as e:
        return {
            'bot': bot_name,
            'status': 'IMPORT_ERROR',
            'error': str(e)[:80]
        }
    except Exception as e:
        return {
            'bot': bot_name,
            'status': 'ERROR',
            'error': str(e)[:80],
            'error_type': type(e).__name__
        }

def main(args):
    bots = get_bots_without_results(limit=args.limit)
    print(f"Analizando firmas de {len(bots)} bots...\n")
    
    results = []
    ok_count = 0
    no_func_count = 0
    error_count = 0
    
    for i, bot in enumerate(bots, 1):
        result = test_bot_signature_only(bot)
        results.append(result)
        
        status = result.get('status')
        if status == 'OK':
            ok_count += 1
            symbol = '✓'
            info = f"{result.get('function')} | {result['param_count']} params"
        elif status == 'NO_FUNC':
            no_func_count += 1
            symbol = '⚠'
            info = "Sin función principal"
        else:
            error_count += 1
            symbol = '❌'
            info = result.get('error', 'Desconocido')
        
        print(f"{i:3}. {symbol} {bot:45} | {info}")
    
    # Escribir CSV
    with open(args.output, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['bot', 'status', 'function', 'signature', 'param_count', 'is_async', 'error', 'error_type'])
        writer.writeheader()
        for r in results:
            writer.writerow(r)
    
    print(f"\n{'='*80}")
    print(f"📊 RESUMEN:")
    print(f"  ✓ Con función válida: {ok_count}/{len(bots)}")
    print(f"  ⚠ Sin función principal: {no_func_count}/{len(bots)}")
    print(f"  ❌ Errores: {error_count}/{len(bots)}")
    print(f"  📄 Reporte: {args.output}")
    print(f"{'='*80}\n")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Test rápido de firmas de bots')
    parser.add_argument('--limit', type=int, default=None, help='Límite de bots a probar (None = todos)')
    parser.add_argument('--output', default='bot_signatures_report.csv', help='Archivo de salida CSV')
    args = parser.parse_args()
    
    main(args)
