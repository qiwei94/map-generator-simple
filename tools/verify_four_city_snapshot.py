"""Verify transferred allow-list and attach existing caches in NEW experiment root."""
import argparse
import hashlib
import json
import os
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--base',type=Path,required=True)
a=p.parse_args()
root=Path(__file__).resolve().parents[1]
if root.name != 'map-four-cities-20260905':
    raise ValueError('This bounded setup only supports the requested isolated directory')
manifest=json.loads((root/'experiment_code_manifest.json').read_text())
for relative, expected in manifest.items():
    path=root/relative
    if not path.resolve().is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
        raise ValueError(f'Code verification failed: {relative}')
for name in ('.venv','pbf_cache','cache','data'):
    target=a.base/name
    if not target.exists():
        raise ValueError(f'Missing existing dependency {target}')
    os.symlink(target,root/name)
for name in ('tmp','output'):
    (root/name).mkdir()
if (a.base/'dem_cache').exists():
    os.symlink(a.base/'dem_cache',root/'dem_cache')
print(json.dumps({'verified_files':len(manifest),'base':str(a.base),'root':str(root)}))
