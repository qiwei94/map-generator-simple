import osmium

import os

import json

import shapely.wkb as wkblib

from shapely.geometry import mapping



pbf_file = r'C:\Users\kiwi\OneDrive\Desktop\pbf\zhejiang.osm.pbf'

output_dir = r'C:\Users\kiwi\OneDrive\Desktop\pbf'



wkbfab = osmium.geom.WKBFactory()



# 西湖25km范围边界框

# West=119.89, South=30.03, East=120.41, North=30.48

BBOX_WEST = 119.89

BBOX_SOUTH = 30.03

BBOX_EAST = 120.41

BBOX_NORTH = 30.48



class WestLakeWaterExtractor(osmium.SimpleHandler):

    def __init__(self):

        super().__init__()

        self.water_features = []

        self.feature_count = 0

        self.west_lake_found = False

    

    def area(self, a):

        # 检查水体标签

        is_water = False

        water_type = None

        

        # 获取标签

        tags = {}

        try:

            for tag in a.tags:

                tags[tag.k] = tag.v

        except:

            pass

        

        if 'natural' in tags and tags['natural'] == 'water':

            is_water = True

            water_type = 'natural=water'

        elif 'water' in tags:

            is_water = True

            water_type = f"water={tags['water']}"

        elif 'waterway' in tags:

            is_water = True

            water_type = f"waterway={tags['waterway']}"

        elif 'landuse' in tags and tags['landuse'] == 'reservoir':

            is_water = True

            water_type = 'landuse=reservoir'

        

        if not is_water:

            return

        

        # 转换几何体

        try:

            wkb = wkbfab.create_multipolygon(a)

            geom = wkblib.loads(wkb, hex=True)

            

            # 检查是否在边界框内

            bounds = geom.bounds

            if bounds[0] <= BBOX_EAST and bounds[2] >= BBOX_WEST and bounds[1] <= BBOX_NORTH and bounds[3] >= BBOX_SOUTH:

                self.feature_count += 1

                

                name = tags.get('name', '(unnamed)')

                

                # 检查是否是西湖

                if '西湖' in name or 'West Lake' in name.lower() or 'xihu' in name.lower():

                    self.west_lake_found = True

                    print(f"*** Found West Lake: ID={a.orig_id()}, name={name} ***")

                

                if self.feature_count <= 10 or '西湖' in name or 'West Lake' in name.lower():

                    print(f"Feature {self.feature_count}: {name}, {water_type}, bounds=[{bounds[0]:.2f},{bounds[1]:.2f},{bounds[2]:.2f},{bounds[3]:.2f}]")

                

                feature = {

                    "type": "Feature",

                    "properties": {

                        "osm_id": a.orig_id(),

                        "osm_type": "relation" if not a.from_way() else "way",

                        "name": name,

                        "water_type": water_type,

                        **tags

                    },

                    "geometry": mapping(geom)

                }

                self.water_features.append(feature)

        except Exception as e:

            pass



print("=" * 60)

print("Extracting water features in West Lake 25km range...")

print(f"BBOX: {BBOX_WEST},{BBOX_SOUTH} to {BBOX_EAST},{BBOX_NORTH}")

print("=" * 60)



handler = WestLakeWaterExtractor()

handler.apply_file(pbf_file)



print()

print("=" * 60)

print(f"Total water features found: {handler.feature_count}")

print(f"West Lake found: {handler.west_lake_found}")

print("=" * 60)



# Save GeoJSON

if handler.water_features:

    geojson = {

        "type": "FeatureCollection",

        "features": handler.water_features

    }

    

    output_file = os.path.join(output_dir, 'westlake_water_25km.geojson')

    with open(output_file, 'w', encoding='utf-8') as f:

        json.dump(geojson, f, ensure_ascii=False, indent=2)

    

    file_size = os.path.getsize(output_file)

    print(f"\nSaved to: {output_file}")

    print(f"File size: {file_size / 1024:.1f} KB ({file_size / 1024 / 1024:.2f} MB)")

else:

    print("No water features found")