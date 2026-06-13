"""Pipeline stage cache — pickle-based, key by input hash.

Saves expensive computation results (terrain mesh, preprocess layers,
GeoDataFrame loads) to disk.  On subsequent runs with identical inputs,
loads from cache instead of recomputing.

Usage:
    from _TEXTURE_STYLE_OF_DEEPSEEK._pipeline_cache import PipelineCache

    cache = PipelineCache("westlake_cli")
    terrain = cache.get_or_compute("terrain_v1", input_keys={"scale": scale, "elev_hash": elev_hash}, compute_fn=build_terrain)
"""

from __future__ import annotations

import hashlib
import os
import pickle
import time
from typing import Any, Callable, Dict, Optional


class PipelineCache:
    """Simple pickle-based stage cache for the build pipeline."""

    def __init__(self, city_name: str, enabled: bool = True,
                 cache_dir: Optional[str] = None):
        if cache_dir is None:
            cache_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'cache', 'pipeline')
        self.cache_dir = os.path.join(cache_dir, city_name)
        self.enabled = enabled
        if enabled:
            os.makedirs(self.cache_dir, exist_ok=True)

    def _make_key(self, stage: str, input_keys: Dict[str, Any]) -> str:
        """Generate a deterministic cache key from stage name + inputs."""
        h = hashlib.md5()
        h.update(stage.encode())
        for k in sorted(input_keys.keys()):
            v = input_keys[k]
            h.update(f"|{k}={v}".encode())
        return f"{stage}_{h.hexdigest()[:12]}"

    def _path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}.pkl")

    def get_or_compute(
        self,
        stage: str,
        input_keys: Dict[str, Any],
        compute_fn: Callable[[], Any],
        *,
        label: str = "",
    ) -> Any:
        """Load from cache if hit, otherwise compute and save.

        Args:
            stage: stage identifier (e.g. "terrain", "preprocess")
            input_keys: dict of parameters that affect the output
            compute_fn: zero-arg callable that produces the result
            label: human-readable label for logging
        """
        if not self.enabled:
            return compute_fn()

        key = self._make_key(stage, input_keys)
        path = self._path(key)

        if os.path.exists(path):
            t0 = time.time()
            with open(path, 'rb') as f:
                result = pickle.load(f)
            elapsed = time.time() - t0
            tag = label or stage
            print(f"  [cache HIT] {tag}: loaded in {elapsed:.1f}s "
                  f"(key={key})")
            return result

        tag = label or stage
        print(f"  [cache MISS] {tag}: computing... (key={key})")
        t0 = time.time()
        result = compute_fn()
        elapsed = time.time() - t0

        try:
            with open(path, 'wb') as f:
                pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
            fsize = os.path.getsize(path) / 1024
            print(f"  [cache SAVE] {tag}: saved in {elapsed:.1f}s "
                  f"({fsize:.0f} KB, key={key})")
        except Exception as e:
            print(f"  [cache WARN] {tag}: compute took {elapsed:.1f}s, "
                  f"save failed: {e}")

        return result

    def invalidate(self, stage: str):
        """Remove all cached files for a given stage prefix."""
        if not os.path.exists(self.cache_dir):
            return
        for f in os.listdir(self.cache_dir):
            if f.startswith(stage + "_") and f.endswith(".pkl"):
                os.remove(os.path.join(self.cache_dir, f))
                print(f"  [cache DEL] {f}")

    def clear(self):
        """Remove all cached files for this city."""
        if not os.path.exists(self.cache_dir):
            return
        for f in os.listdir(self.cache_dir):
            if f.endswith(".pkl"):
                os.remove(os.path.join(self.cache_dir, f))
        print(f"  [cache CLEAR] {self.cache_dir}")
