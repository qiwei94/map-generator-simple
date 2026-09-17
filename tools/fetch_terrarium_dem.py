"""Fetch a compact georeferenced DEM from the AWS Terrain Tiles dataset."""
from __future__ import annotations

import argparse
import io
import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_origin

ORIGIN = 20037508.342789244
SIZE = 256


def tile_xy(lon, lat, zoom):
    n = 2**zoom
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(max(-85.05112878, min(85.05112878, lat)))
    y = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    return x, y


def fetch(item):
    zoom, x, y = item
    url = f"https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{zoom}/{x}/{y}.png"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=60) as response:
        rgb = np.asarray(Image.open(io.BytesIO(response.read())).convert("RGB"), dtype=np.float32)
    return x, y, rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bbox", required=True, help="south,west,north,east")
    parser.add_argument("--zoom", type=int, default=9)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    south, west, north, east = map(float, args.bbox.split(","))
    x0f, y1f = tile_xy(west, south, args.zoom)
    x1f, y0f = tile_xy(east, north, args.zoom)
    x0, x1 = math.floor(x0f), math.floor(x1f)
    y0, y1 = math.floor(y0f), math.floor(y1f)
    jobs = [(args.zoom, x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    with ThreadPoolExecutor(max_workers=min(12, len(jobs))) as pool:
        tiles = list(pool.map(fetch, jobs))
    mosaic = np.empty(((y1 - y0 + 1) * SIZE, (x1 - x0 + 1) * SIZE), dtype=np.float32)
    for x, y, values in tiles:
        mosaic[(y-y0)*SIZE:(y-y0+1)*SIZE, (x-x0)*SIZE:(x-x0+1)*SIZE] = values
    world_px = SIZE * 2**args.zoom
    resolution = 2 * ORIGIN / world_px
    transform = from_origin(-ORIGIN + x0*SIZE*resolution,
                            ORIGIN - y0*SIZE*resolution, resolution, resolution)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(args.output, "w", driver="GTiff", height=mosaic.shape[0],
                       width=mosaic.shape[1], count=1, dtype="float32", crs="EPSG:3857",
                       transform=transform, compress="deflate", predictor=3) as dst:
        dst.write(mosaic, 1)
    print(f"{args.output}: tiles={len(jobs)}, shape={mosaic.shape}, "
          f"resolution={resolution:.1f}m, elevation={mosaic.min():.1f}..{mosaic.max():.1f}m")


if __name__ == "__main__":
    main()
