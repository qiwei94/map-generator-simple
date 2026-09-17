#!/usr/bin/env python3
"""Fetch a small, georeferenced public satellite mosaic for visual masking."""
from __future__ import annotations

import argparse
import io
import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image

TILE = 256


def _tile_xy(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    size = 2 ** zoom
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * size
    return (lon + 180) / 360 * size, y


def _lonlat(x: float, y: float, zoom: int) -> tuple[float, float]:
    size = 2 ** zoom
    lon = x / size * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / size))))
    return lon, lat


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bbox", required=True, help="south,west,north,east")
    parser.add_argument("--zoom", type=int, default=12)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    south, west, north, east = map(float, args.bbox.split(","))
    x0f, y1f = _tile_xy(west, south, args.zoom)
    x1f, y0f = _tile_xy(east, north, args.zoom)
    x0, x1 = math.floor(x0f), math.floor(x1f)
    y0, y1 = math.floor(y0f), math.floor(y1f)
    jobs = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]

    def fetch(job):
        x, y = job
        url = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
               f"World_Imagery/MapServer/tile/{args.zoom}/{y}/{x}")
        response = requests.get(url, timeout=45)
        response.raise_for_status()
        return x, y, Image.open(io.BytesIO(response.content)).convert("RGB")

    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        tiles = list(pool.map(fetch, jobs))
    mosaic = Image.new("RGB", ((x1 - x0 + 1) * TILE, (y1 - y0 + 1) * TILE))
    for x, y, image in tiles:
        mosaic.paste(image, ((x - x0) * TILE, (y - y0) * TILE))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    mosaic.save(args.output)
    west_actual, north_actual = _lonlat(x0, y0, args.zoom)
    east_actual, south_actual = _lonlat(x1 + 1, y1 + 1, args.zoom)
    metadata = {
        "provider": "Esri World Imagery", "zoom": args.zoom,
        "requested_bbox_wgs84": [south, west, north, east],
        "mosaic_bbox_wgs84": [south_actual, west_actual, north_actual, east_actual],
        "tile_range": [x0, y0, x1, y1], "size_px": list(mosaic.size),
    }
    args.output.with_suffix(".json").write_text(json.dumps(metadata, indent=2))
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
