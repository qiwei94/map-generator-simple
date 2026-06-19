import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.INFO)

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import fetch_from_cli
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.height_enrichment import load_overture_heights

pbf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbf_cache", "chongqing-latest.osm.pbf")
gdf = fetch_from_cli("building", 29.45, 106.45, 29.67, 106.7, pbf)
print(f"OSM buildings: {len(gdf)}")

if "height_source" in gdf.columns:
    for src, count in gdf["height_source"].value_counts().items():
        print(f"  {src}: {count} ({count/len(gdf)*100:.1f}%)")
else:
    print("  height_source column not found!")

n_tagged = (gdf["est_height"] != 10.0).sum()
print(f"Total tagged: {n_tagged}/{len(gdf)} ({n_tagged/len(gdf)*100:.1f}%)")
