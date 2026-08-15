# Conversational map DesignSpec

`DesignSpec` is the stable boundary between a conversational design proposal and
the deterministic map-to-3MF compiler:

```text
natural-language intent -> validated DesignSpec -> deterministic pipeline
```

The schema can select semantic layers and filter OSM tags. It cannot control
global Z values, arbitrary meshes, boolean operations, or low-level geometry
algorithms. Printable specifications must keep terrain enabled.

## Built-in presets

| Preset | Enabled output layers |
| --- | --- |
| `city_texture` | terrain and all supported semantic layers |
| `terrain_only` | terrain |
| `road_network` | terrain, roads |
| `water_focus` | terrain, water |

The pipeline derives required source datasets from the enabled output layers.
For example, `terrain_only` performs no OSM fetch, while `road_network` fetches
only roads. A custom specification with landmarks enabled and ambient buildings
disabled uses the narrower `building_landmarks` source filter.

## JSON format

```json
{
  "version": "1.0",
  "name": "primary-road-print",
  "preset": "road_network",
  "layers": {
    "roads": {
      "enabled": true,
      "include_tags": {
        "highway": ["primary", "secondary"]
      },
      "exclude_tags": {
        "access": ["private"]
      }
    }
  }
}
```

Layer overrides are merged onto the selected preset. Include filters use OR
semantics across tag keys; exclude filters are applied afterwards. `"*"` means
the tag must be present.

Supported layers are:

```text
terrain, buildings, landmarks, roads, water, vegetation, block_base
```

Unknown top-level fields, unknown layers, and unknown layer controls are
rejected. This fail-closed behavior prevents conversational input from reaching
geometry controls that are outside the DesignSpec contract.

## CLI

The reusable pipeline exposes the required options directly:

```bash
.venv/bin/python -m _TEXTURE_STYLE_OF_DEEPSEEK.pipeline \
  --lat1 30.20 --lon1 120.10 --lat2 30.22 --lon2 120.12 \
  --preset road_network

.venv/bin/python -m _TEXTURE_STYLE_OF_DEEPSEEK.pipeline \
  --lat1 30.20 --lon1 120.10 --lat2 30.22 --lon2 120.12 \
  --design-spec examples/road-design.json
```

`generate_city.py` retains its historical city presets (`westlake`, `chicago`,
and `chongqing`) and also accepts the DesignSpec presets through the same
`--preset` option when `--bbox`, `--pbf`, and `--city` are supplied:

```bash
.venv/bin/python generate_city.py \
  --bbox 30.20,120.10,30.22,120.12 \
  --pbf pbf_cache/zhejiang-latest.osm.pbf \
  --city westlake-small \
  --preset road_network
```

Use `--design-spec PATH` for custom JSON. Existing `--no-vegetation` and
`--no-block-base` flags remain supported as explicit layer-disable overrides.

## Reproducibility

Every resolved specification has a SHA-256 fingerprint over canonical JSON.
Every pipeline run writes the complete resolved document to
`design_spec.json` beside the generated artifacts. A saved fingerprint is
validated when the file is read again, so accidental edits are detected.

The fingerprint describes the design contract, not the external data snapshot.
Fully reproducible manufacturing builds should record the input PBF and elevation
data checksums separately.
