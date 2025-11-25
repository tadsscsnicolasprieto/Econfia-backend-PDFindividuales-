#!/usr/bin/env python
import os, re, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
BOTS_DIR = os.path.join(ROOT, 'core', 'bots')
if not os.path.isdir(BOTS_DIR):
    print('No existe el directorio core/bots')
    sys.exit(1)

pattern = re.compile(r"(?:async\s+def|def)\s+(?:consultar_|ejecutar_|main\b)", re.I)
files = [f for f in os.listdir(BOTS_DIR) if f.endswith('.py') and not f.startswith('__')]
matched = []
for fname in files:
    path = os.path.join(BOTS_DIR, fname)
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
            txt = fh.read()
            if pattern.search(txt):
                matched.append(fname)
    except Exception as e:
        pass

print(len(matched))
for m in sorted(matched):
    print(m)
