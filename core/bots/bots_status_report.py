
import os
import sys
from datetime import datetime

# Añadir la raíz del proyecto al sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.models import Fuente
from core.scripts.fuentes_bots import FUENTES_BOTS

LOG_PATH = os.path.join(os.path.dirname(__file__), 'bots_status.log')

def log_bots_status():
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG_PATH, 'a', encoding='utf-8') as log:
        log.write(f'--- Bot Status Report: {now} ---\n')
        fuentes_db = set(Fuente.objects.values_list('nombre', flat=True))
        for nombre in FUENTES_BOTS:
            status = 'Registrado' if nombre in fuentes_db else 'No registrado'
            log.write(f'Bot: {nombre} | Estado: {status}\n')
        # Bots en DB pero no en FUENTES_BOTS
        extra_db = fuentes_db - set(FUENTES_BOTS)
        for nombre in extra_db:
            log.write(f'Bot en DB pero no en FUENTES_BOTS: {nombre}\n')
        log.write('\n')

# Para ejecutar el reporte manualmente:
if __name__ == '__main__':
    log_bots_status()
