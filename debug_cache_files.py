"""Check which cache files are loaded for Hangzhou"""
import sys, os
sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import json
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.city_cache_loader import (
    _get_cache_index,
    _find_nearby_cities,
    _resolve_cache_path,
    CACHE_FILE_ROOTS,
)

LAT1, LON1 = 30.1375, 120.020
LAT2, LON2 = 30.3625, 120.280
south, west, north, east = LAT1, LON1, LAT2, LON2

print('=== Cache Files for Hangzhou West Lake Area ===')
print('Query bbox: (%.4f, %.4f) to (%.4f, %.4f)' % (south, west, north, east))

# Find nearby cities
nearby = _find_nearby_cities(south, west, north, east)
print('\nNearby cities in cache index: %d' % len(nearby))

for key, data in nearby:
    print('\n--- %s ---' % key)
    lat = data.get('lat', '')
    lon = data.get('lon', '')
    print('Center: lat=%s, lon=%s' % (lat, lon))
    
    cache_files = data.get('cache_files', [])
    print('Cache files: %d' % len(cache_files))
    
    if len(cache_files) > 0:
        # Check water-related files
        water_files = []
        for cf in cache_files:
            resolved = _resolve_cache_path(cf)
            if resolved:
                # Check file content for water features
                try:
                    with open(resolved, 'r', encoding='utf-8') as f:
                        content = json.load(f)
                    elems = content.get('elements', [])
                    
                    # Count water-related elements
                    water_count = 0
                    for elem in elems:
                        tags = elem.get('tags', {})
                        if tags.get('natural') == 'water' or tags.get('water') or tags.get('waterway'):
                            water_count += 1
                    
                    if water_count > 0:
                        water_files.append({
                            'path': cf,
                            'resolved': resolved,
                            'elements': len(elems),
                            'water_elements': water_count,
                        })
                except Exception as e:
                    pass
        
        print('Water-related files: %d' % len(water_files))
        total_water_elements = sum(f['water_elements'] for f in water_files)
        print('Total water elements in cache: %d' % total_water_elements)
        
        for wf in water_files[:10]:
            print('  %s: %d elems, %d water' % (wf['path'][:50], wf['elements'], wf['water_elements']))

# Check if 西湖 entry has data
print('\n=== 西湖 (West Lake) Entry ===')
index = _get_cache_index()
xi_hu_key = None
for key in index.keys():
    if '西湖' in key:
        xi_hu_key = key
        break

if xi_hu_key:
    data = index[xi_hu_key]
    print('Key: %s' % xi_hu_key)
    print('Data: %s' % json.dumps(data, indent=2)[:500])
else:
    print('No 西湖 entry found')

# Also check Hangzhou entry
print('\n=== Hangzhou Entry ===')
for key, data in index.items():
    if 'Hangzhou' in key:
        print('Key: %s' % key)
        cache_files = data.get('cache_files', [])
        print('cache_files count: %d' % len(cache_files))
        if len(cache_files) > 0:
            print('First 5 cache_files:')
            for cf in cache_files[:5]:
                resolved = _resolve_cache_path(cf)
                print('  %s -> exists=%s' % (cf, resolved is not None))
        break