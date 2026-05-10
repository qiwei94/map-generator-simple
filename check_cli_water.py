"""检查 CLI pipeline 生成的水体文件是否有钱塘江"""

import json

geojson_file = r'cache/geojson/water_30.03_119.89_30.48_120.41.geojson'

with open(geojson_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

print(f"Total features: {len(data['features'])}")

# 统计几何类型
from collections import Counter
geom_types = Counter(f['geometry']['type'] for f in data['features'])
print(f"Geometry types: {dict(geom_types)}")

# 查找钱塘江
qt_features = []
for feat in data['features']:
    props = feat.get('properties', {})
    name = props.get('name', '')
    waterway = props.get('waterway', '')
    
    if '钱塘' in str(name) or 'Qiantang' in str(name) or waterway == 'river':
        qt_features.append({
            'name': name,
            'waterway': waterway,
            'geom_type': feat['geometry']['type'],
        })

print(f"\nRiver features (waterway=river): {len([f for f in qt_features if f['waterway'] == 'river'])}")
print(f"Qiantang River: {len([f for f in qt_features if '钱塘' in f['name'] or 'Qiantang' in f['name']])}")

# 显示前 10 条河流
rivers = [f for f in qt_features if f['waterway'] == 'river'][:10]
print(f"\nTop 10 rivers:")
for f in rivers:
    print(f"  name={f['name']}, geom={f['geom_type']}")
