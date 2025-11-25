#!/usr/bin/env python
"""
Detecta archivos en core/bots que NO contienen una función principal
(consultar_/ejecutar_/main) y los mueve a core/bots/_archivos_no_bot/.
Genera `moved_non_bots.log` con el listado.
"""
import os,re,shutil

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
BOTS_DIR = os.path.join(ROOT, 'core', 'bots')
DEST_DIR = os.path.join(BOTS_DIR, '_archivos_no_bot')
LOG_FILE = os.path.join(ROOT, 'scripts', 'moved_non_bots.log')

if not os.path.isdir(BOTS_DIR):
    print('No existe el directorio core/bots')
    raise SystemExit(1)

pattern = re.compile(r"(?:async\s+def|def)\s+(?:consultar_|ejecutar_|main\b)", re.I)

os.makedirs(DEST_DIR, exist_ok=True)
moved = []

files = sorted([f for f in os.listdir(BOTS_DIR) if f.endswith('.py') and not f.startswith('__')])
locked = []
copied = []
for fname in files:
    src = os.path.join(BOTS_DIR, fname)
    try:
        with open(src, 'r', encoding='utf-8', errors='ignore') as fh:
            txt = fh.read()
            if not pattern.search(txt):
                dest = os.path.join(DEST_DIR, fname)
                print(f"Procesando {fname} -> destino: _archivos_no_bot/")
                try:
                    shutil.move(src, dest)
                    moved.append(fname)
                except PermissionError as pe:
                    # Archivo en uso; intentar copiar como fallback
                    try:
                        shutil.copy2(src, dest)
                        copied.append(fname)
                        print(f"  Copiado (fallback) {fname} -> _archivos_no_bot/")
                    except Exception as e:
                        locked.append((fname, str(e)))
                except Exception as e:
                    # Otros errores, registrar
                    locked.append((fname, str(e)))
    except Exception as e:
        locked.append((fname, str(e)))

with open(LOG_FILE, 'w', encoding='utf-8') as log:
    log.write('moved:\n')
    for m in moved:
        log.write(m + '\n')
    log.write('\ncopied_fallback:\n')
    for c in copied:
        log.write(c + '\n')
    log.write('\nlocked_errors:\n')
    for name, err in locked:
        log.write(f"{name}: {err}\n")

print('\nHecho. Archivos movidos: %d' % len(moved))
print('Copias por fallback: %d' % len(copied))
print('Bloqueados/no procesados: %d' % len(locked))
print('Log: ' + LOG_FILE)
