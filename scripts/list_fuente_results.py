import os
import sys
import json

# Ajusta la variable del settings de Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
# Ensure project root is on sys.path so Python can import the 'backend' package
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
import django
django.setup()

from core.models import Resultado, Fuente


def list_results(fuente_nombre, limit=20):
    f = Fuente.objects.filter(nombre=fuente_nombre).first()
    if not f:
        print(f"Fuente '{fuente_nombre}' no encontrada")
        return 1
    qs = Resultado.objects.filter(fuente=f).order_by('-id').values('id','consulta_id','mensaje','archivo','estado')[:limit]
    out = list(qs)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Uso: python scripts/list_fuente_results.py <fuente_nombre> [limit]')
        sys.exit(2)
    fuente = sys.argv[1]
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    sys.exit(list_results(fuente, lim))
