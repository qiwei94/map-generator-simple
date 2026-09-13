"""Content identities for derived geometry caches (never paths/counts alone)."""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
import pandas as pd


def _signature(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def file_content_identity(path):
    absolute = os.path.realpath(path)
    signature = _signature(os.stat(absolute))
    result = dict(_file_content_identity(absolute, signature))
    if _signature(os.stat(absolute)) != signature:
        raise ValueError('source file changed while hashing')
    return result


@lru_cache(maxsize=16)
def _file_content_identity(path, signature):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        before = os.fstat(stream.fileno())
        while True:
            chunk = stream.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if signature != _signature(before) or signature != _signature(after) or signature != _signature(os.stat(path)):
        raise ValueError('source file changed while hashing')
    return {'sha256': digest.hexdigest(), 'size_bytes': after.st_size}


def projected_sources_identity(sources):
    result = {}
    for name, frame in sorted(sources.items()):
        digest = hashlib.sha256()
        if frame is not None:
            digest.update(str(frame.crs).encode())
            for geometry in frame.geometry:
                value = geometry.wkb if geometry is not None else b''
                digest.update(len(value).to_bytes(8, 'little'))
                digest.update(value)
            # Geometry, height, road class, bridge flags, names etc. all affect
            # generation. Pandas' stable hash also handles missing values.
            for column in sorted(c for c in frame.columns if c != frame.geometry.name):
                digest.update(str(column).encode())
                values = frame[column]
                try:
                    hashed = pd.util.hash_pandas_object(values, index=False)
                except TypeError:
                    values = values.map(lambda v: json.dumps(v, sort_keys=True, default=str))
                    hashed = pd.util.hash_pandas_object(values, index=False)
                digest.update(hashed.to_numpy().astype('<u8').tobytes())
        result[name] = {'features': 0 if frame is None else len(frame),
                        'sha256': digest.hexdigest()}
    return {'schema_version': 'projected-source-content-v1', 'layers': result}
