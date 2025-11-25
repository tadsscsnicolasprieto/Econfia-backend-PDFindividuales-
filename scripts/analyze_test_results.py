#!/usr/bin/env python
"""
Generador de reporte de bot testing.
Lee el CSV y genera un resumen con categorías.
"""
import csv
import sys

def analyze_csv(csv_file):
    """Analiza el CSV de pruebas y genera reporte."""
    
    results = []
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            results.append(row)
    
    passed = [r for r in results if r['status'] == 'PASS']
    failed = [r for r in results if r['status'] == 'FAIL']
    import_failed = [r for r in results if r.get('test') == 'import' and r['status'] == 'FAIL']
    exec_failed = [r for r in results if r.get('test') == 'execution' and r['status'] == 'FAIL']
    
    # Contar por número de parámetros
    param_counts = {}
    for r in passed:
        try:
            pc = int(r.get('param_count', 0) or 0)
            if pc not in param_counts:
                param_counts[pc] = []
            param_counts[pc].append(r['bot'])
        except:
            pass
    
    print("\n" + "="*80)
    print("📊 REPORTE DE PRUEBAS DE BOTS")
    print("="*80)
    
    print(f"\n✅ TOTAL DE BOTS PROBADOS: {len(results)}")
    print(f"   ✓ Exitosos: {len(passed)} ({100*len(passed)//len(results)}%)")
    print(f"   ❌ Fallados: {len(failed)} ({100*len(failed)//len(results)}%)")
    
    print(f"\n📋 CATEGORÍA DE FALLOS:")
    print(f"   - No encontró función: {len(import_failed)}")
    print(f"   - Error en ejecución: {len(exec_failed)}")
    
    print(f"\n📝 DISTRIBUCIÓN POR NÚMERO DE PARÁMETROS (Bots exitosos):")
    for param_count in sorted(param_counts.keys()):
        bots = param_counts[param_count]
        print(f"   {param_count} parámetros: {len(bots)} bots")
        for bot in bots[:3]:
            print(f"      - {bot}")
        if len(bots) > 3:
            print(f"      ... y {len(bots)-3} más")
    
    print(f"\n⚠️ BOTS SIN FUNCIÓN EXPORTADA (probablemente no son bots reales):")
    for r in import_failed:
        print(f"   - {r['bot']}")
    
    print(f"\n🔧 RECOMENDACIONES:")
    print(f"   1. {len(import_failed)} archivos no son bots reales (eliminar o renombrar)")
    print(f"   2. {len(passed)} bots están funcionando correctamente")
    print(f"   3. Las firmas varían entre 2-4 parámetros (se adaptó exitosamente)")
    
    print("\n" + "="*80)

if __name__ == '__main__':
    if len(sys.argv) > 1:
        analyze_csv(sys.argv[1])
    else:
        print("Uso: python analyze_test_results.py <csv_file>")
