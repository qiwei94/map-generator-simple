import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import logging
logging.basicConfig(level=logging.WARNING)

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import fetch_from_cli

# Load OSM buildings
pbf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbf_cache", "zhejiang-latest.osm.pbf")
gdf = fetch_from_cli("building", 30.13, 120.01, 30.36, 120.29, pbf)

print(f"Total buildings: {len(gdf)}")
print(f"\nHeight source distribution:")
if "height_source" in gdf.columns:
    for src, count in gdf["height_source"].value_counts().items():
        pct = count / len(gdf) * 100
        # Get height stats for this source
        h = gdf.loc[gdf["height_source"] == src, "est_height"]
        print(f"  {src:15s}: {count:>6,} ({pct:5.1f}%)  height range: {h.min():.0f}-{h.max():.0f}m, median={h.median():.0f}m")
else:
    print("  height_source column not found!")

# Show some examples of each source
print(f"\nExamples by source:")
for src in ["osm_height", "osm_levels", "overture", "default"]:
    subset = gdf[gdf.get("height_source", pd.Series()) == src]
    if len(subset) > 0:
        named = subset[subset["name"].notna()]
        print(f"\n  [{src}] ({len(subset)} buildings)")
        if len(named) > 0:
            for _, row in named.head(5).iterrows():
                print(f"    {row.get('name', '?'):30s}  h={row['est_height']:.0f}m  area={row.geometry.area:.0f}m2")
        else:
            for _, row in subset.head(3).iterrows():
                print(f"    (unnamed)  h={row['est_height']:.0f}m  area={row.geometry.area:.0f}m2")
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.INFO)

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import fetch_from_cli
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.height_enrichment import load_overture_heights
import os

# Load OSM buildings (from GeoJSON cache)
print("Loading OSM buildings...")
pbf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbf_cache", "zhejiang-latest.osm.pbf")
gdf = fetch_from_cli("building", 30.13, 120.01, 30.36, 120.29, pbf)
print(f"OSM buildings: {len(gdf)}")
n_tagged = (gdf["est_height"] != 10.0).sum()
print(f"est_height tagged: {n_tagged}/{len(gdf)} ({n_tagged/len(gdf)*100:.1f}%)")

# Try Overture enrichment
print()
print("Loading Overture heights...")
cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "height_cache")
print(f"Cache dir: {cache_dir}")
print(f"Exists: {os.path.isdir(cache_dir)}")

ov_h, ov_n = load_overture_heights(
    gdf, bbox_wgs84=(30.13, 120.01, 30.36, 120.29),
    cache_dir=cache_dir)

if ov_h is not None:
    n_matched = ov_h.notna().sum()
    print(f"\nOverture matched: {n_matched}/{len(gdf)} ({n_matched/len(gdf)*100:.1f}%)")
    # How many NEW heights (OSM didn't have but Overture does)
    osm_default = gdf["est_height"] == 10.0
    ov_valid = ov_h.notna() & (ov_h > 0)
    new_heights = (osm_default & ov_valid).sum()
    print(f"NEW heights (OSM default + Overture valid): {new_heights}")
else:
    print("Overture returned None!")
