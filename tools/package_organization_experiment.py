#!/usr/bin/env python3
"""Create an allow-listed experimental code snapshot, with no credentials/data."""
from pathlib import Path
import argparse
import hashlib
import io
import json
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'tmp/organization_code_20260905.tar.gz')
    args=parser.parse_args()
    selected = list(ROOT.glob('*.py'))
    for name in ('aesthetic', '_TEXTURE_STYLE_OF_DEEPSEEK', 'tools', 'tests', 'webapp'):
        selected.extend((ROOT / name).rglob('*.py'))
    selected.extend([ROOT / 'tools/mesh_depth_raster.c', ROOT / 'pytest.ini'])
    selected = sorted(set(selected))
    manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in selected}
    target = args.output
    if target.exists():
        raise ValueError('Refusing to overwrite an existing experiment archive')
    with tarfile.open(target, 'w:gz') as archive:
        for p in selected:
            archive.add(p, arcname=str(p.relative_to(ROOT)), recursive=False)
        data = json.dumps(manifest, sort_keys=True, indent=2).encode()
        entry = tarfile.TarInfo('experiment_code_manifest.json')
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    print(json.dumps({'archive':str(target),'files':len(manifest),
                      'bytes':target.stat().st_size,
                      'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}))


if __name__ == '__main__': main()
