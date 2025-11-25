#!/usr/bin/env python
"""
Analiza firmas de funciones en bots para detectar parámetros esperados.
Genera un reporte de cuántos parámetros espera cada bot.
"""
import os
import sys
import inspect
import importlib
import json

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
import django
django.setup()

from core.models import Fuente, Resultado

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

def analyze_bot_signature(bot_name):
    """Analiza la firma de la función principal del bot."""
    try:
        module = importlib.import_module(f'core.bots.{bot_name}')
        
        # Buscar función principal
        func = None
        func_name = None
        for fn in ['consultar_' + bot_name, bot_name, 'ejecutar_' + bot_name, 'main']:
            if hasattr(module, fn):
                func = getattr(module, fn)
                func_name = fn
                break
        
        if not func:
            return {'bot': bot_name, 'status': 'NO_FUNC', 'signature': '', 'params': 0}
        
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())
        
        return {
            'bot': bot_name,
            'status': 'OK',
            'function': func_name,
            'signature': str(sig),
            'params': params,
            'param_count': len(params)
        }
    except Exception as e:
        return {'bot': bot_name, 'status': 'ERROR', 'error': str(e), 'params': 0}

def main():
    bots = get_bots_without_results()
    print(f"Analizando {len(bots)} bots...")
    
    results = []
    param_groups = {}
    
    for bot in bots[:20]:  # primeros 20
        info = analyze_bot_signature(bot)
        results.append(info)
        
        param_count = info.get('param_count', 0)
        if param_count not in param_groups:
            param_groups[param_count] = []
        param_groups[param_count].append(bot)
        
        status = info.get('status')
        symbol = '✓' if status == 'OK' else '❌'
        params_str = ', '.join(info.get('params', [])) if info.get('params') else 'N/A'
        print(f"{symbol} {bot}: {info.get('function', 'N/A')}({params_str})")
    
    print("\n📊 AGRUPACIÓN POR NÚMERO DE PARÁMETROS:")
    for param_count in sorted(param_groups.keys()):
        print(f"  {param_count} parámetros: {len(param_groups[param_count])} bots")
        for bot in param_groups[param_count][:5]:
            print(f"    - {bot}")
    
    # Guardar JSON
    with open('bot_signatures.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\n✅ Análisis guardado en bot_signatures.json")

if __name__ == '__main__':
    main()
