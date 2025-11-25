#!/usr/bin/env python
import os,re,sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
BOTS_DIR = os.path.join(ROOT, 'core', 'bots')
pattern = re.compile(r"(?:async\s+def|def)\s+(?:consultar_|ejecutar_|main\b)", re.I)
count=0
files=[f for f in os.listdir(BOTS_DIR) if f.endswith('.py') and not f.startswith('__')]
for fname in files:
    try:
        with open(os.path.join(BOTS_DIR,fname),'r',encoding='utf-8',errors='ignore') as fh:
            if pattern.search(fh.read()):
                count+=1
    except:
        pass
print(count)
