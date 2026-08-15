"""Constrained, versioned map design specifications.

DesignSpec controls semantic layer selection and OSM tag filtering only. It is
deliberately unable to set mesh operations, global Z values, or boolean rules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple, Union


DESIGN_SPEC_VERSION = "1.0"
LAYER_NAMES = (
    "terrain",
    "buildings",
    "landmarks",
    "roads",
    "water",
    "vegetation",
    "block_base",
)

_LAYER_FIELDS = {"enabled", "include_tags", "exclude_tags"}
_SPEC_FIELDS = {"version", "name", "preset", "layers"}
TagValues = Tuple[str, ...]


def _normalise_tag_filters(raw: Optional[Mapping[str, Any]]) -> Dict[str, TagValues]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("tag filters must be an object mapping tag keys to values")

    result: Dict[str, TagValues] = {}
    for key, values in raw.items():
        if not isinstance(key, str) or not key:
            raise ValueError("tag filter keys must be non-empty strings")
        if values is True or values == "*":
            normalised = ("*",)
        elif isinstance(values, str):
            normalised = (values,)
        elif isinstance(values, (list, tuple, set)):
            normalised = tuple(sorted({str(value) for value in values}))
        else:
            raise ValueError(f"unsupported tag filter values for {key!r}")
        if not normalised:
            raise ValueError(f"tag filter values for {key!r} cannot be empty")
        result[key] = normalised
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class LayerSpec:
    """Semantic controls for one supported map layer."""

    enabled: bool = False
    include_tags: Mapping[str, TagValues] = field(default_factory=dict)
    exclude_tags: Mapping[str, TagValues] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]]) -> "LayerSpec":
        raw = raw or {}
        unknown = set(raw) - _LAYER_FIELDS
        if unknown:
            raise ValueError(f"unsupported layer fields: {sorted(unknown)}")
        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("layer enabled must be a boolean")
        return cls(
            enabled=enabled,
            include_tags=_normalise_tag_filters(raw.get("include_tags")),
            exclude_tags=_normalise_tag_filters(raw.get("exclude_tags")),
        )

    def to_dict(self) -> Dict[str, Any]:
        def serialise(filters: Mapping[str, TagValues]) -> Dict[str, list]:
            return {key: list(values) for key, values in sorted(filters.items())}

        return {
            "enabled": self.enabled,
            "include_tags": serialise(self.include_tags),
            "exclude_tags": serialise(self.exclude_tags),
        }


_PRESET_LAYERS: Dict[str, Tuple[str, ...]] = {
    "city_texture": LAYER_NAMES,
    "terrain_only": ("terrain",),
    "road_network": ("terrain", "roads"),
    "water_focus": ("terrain", "water"),
}
DESIGN_PRESETS = tuple(sorted(_PRESET_LAYERS))


@dataclass(frozen=True)
class DesignSpec:
    """Validated design input for deterministic pipeline compilation."""

    version: str = DESIGN_SPEC_VERSION
    name: str = "city_texture"
    preset: Optional[str] = None
    layers: Mapping[str, LayerSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.version != DESIGN_SPEC_VERSION:
            raise ValueError(
                f"unsupported DesignSpec version {self.version!r}; "
                f"expected {DESIGN_SPEC_VERSION!r}"
            )
        unknown = set(self.layers) - set(LAYER_NAMES)
        if unknown:
            raise ValueError(f"unsupported layers: {sorted(unknown)}")
        if not self.layer("terrain").enabled:
            raise ValueError("printable DesignSpec must keep the terrain layer enabled")

    def layer(self, name: str) -> LayerSpec:
        if name not in LAYER_NAMES:
            raise KeyError(f"unsupported layer: {name}")
        return self.layers.get(name, LayerSpec())

    def enabled(self, name: str) -> bool:
        return self.layer(name).enabled

    def with_layer(self, name: str, *, enabled: bool) -> "DesignSpec":
        layers = dict(self.layers)
        layers[name] = replace(self.layer(name), enabled=enabled)
        return replace(self, layers=layers)

    def required_sources(self) -> Tuple[str, ...]:
        """Return source datasets needed by enabled output layers."""
        block_base = self.enabled("block_base")
        sources = []
        if self.enabled("buildings") or self.enabled("landmarks") or block_base:
            sources.append("buildings")
        if self.enabled("roads") or block_base:
            sources.append("roads")
        if self.enabled("water") or block_base:
            sources.append("water")
        if self.enabled("vegetation") or block_base:
            sources.append("vegetation")
        if block_base:
            sources.append("landuse")
        return tuple(sources)

    @property
    def landmarks_only(self) -> bool:
        return self.enabled("landmarks") and not self.enabled("buildings")

    def to_dict(self, *, include_fingerprint: bool = True) -> Dict[str, Any]:
        result = {
            "version": self.version,
            "name": self.name,
            "preset": self.preset,
            "layers": {name: self.layer(name).to_dict() for name in LAYER_NAMES},
        }
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_dict(include_fingerprint=False),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def save(self, path: Union[str, Path]) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return destination


def _preset_dict(name: str) -> Dict[str, Any]:
    try:
        enabled_layers = set(_PRESET_LAYERS[name])
    except KeyError as exc:
        raise ValueError(
            f"unknown design preset {name!r}; choose from {', '.join(DESIGN_PRESETS)}"
        ) from exc
    return {
        "version": DESIGN_SPEC_VERSION,
        "name": name,
        "preset": name,
        "layers": {
            layer: {"enabled": layer in enabled_layers}
            for layer in LAYER_NAMES
        },
    }


def _merge_preset(raw: Mapping[str, Any]) -> Dict[str, Any]:
    preset = raw.get("preset")
    if not preset:
        return dict(raw)
    merged = _preset_dict(str(preset))
    merged.update({key: value for key, value in raw.items() if key != "layers"})
    layer_overrides = raw.get("layers", {})
    if not isinstance(layer_overrides, Mapping):
        raise ValueError("layers must be an object")
    for name, override in layer_overrides.items():
        base_layer = dict(merged["layers"].get(name, {}))
        if not isinstance(override, Mapping):
            raise ValueError(f"layer {name!r} must be an object")
        base_layer.update(override)
        merged["layers"][name] = base_layer
    return merged


def _design_spec_from_dict(raw: Mapping[str, Any]) -> DesignSpec:
    unknown = set(raw) - _SPEC_FIELDS - {"fingerprint"}
    if unknown:
        raise ValueError(f"unsupported DesignSpec fields: {sorted(unknown)}")
    merged = _merge_preset(raw)
    layers_raw = merged.get("layers", {})
    if not isinstance(layers_raw, Mapping):
        raise ValueError("layers must be an object")
    layers = {name: LayerSpec.from_dict(layer) for name, layer in layers_raw.items()}
    spec = DesignSpec(
        version=str(merged.get("version", DESIGN_SPEC_VERSION)),
        name=str(merged.get("name", merged.get("preset") or "custom")),
        preset=merged.get("preset"),
        layers=layers,
    )
    expected = raw.get("fingerprint")
    if expected is not None and expected != spec.fingerprint:
        raise ValueError("DesignSpec fingerprint does not match its contents")
    return spec


DesignInput = Optional[Union[DesignSpec, Mapping[str, Any], str, Path]]


def resolve_design_spec(
    design_spec: DesignInput = None,
    *,
    preset: Optional[str] = None,
) -> DesignSpec:
    """Resolve a preset, JSON object, JSON path, or existing DesignSpec."""
    if design_spec is not None and preset is not None:
        raise ValueError("provide either design_spec or preset, not both")
    if isinstance(design_spec, DesignSpec):
        return design_spec
    if design_spec is None:
        return _design_spec_from_dict(_preset_dict(preset or "city_texture"))
    if isinstance(design_spec, (str, Path)):
        raw = json.loads(Path(design_spec).read_text(encoding="utf-8"))
    elif isinstance(design_spec, Mapping):
        raw = dict(design_spec)
    else:
        raise TypeError("design_spec must be a DesignSpec, mapping, or JSON path")
    if not isinstance(raw, Mapping):
        raise ValueError("DesignSpec JSON root must be an object")
    return _design_spec_from_dict(raw)


def _tag_mask(series, values: Iterable[str]):
    accepted = set(values)
    if "*" in accepted:
        return series.notna()

    def matches(value: Any) -> bool:
        if isinstance(value, (list, tuple, set)):
            return any(str(item) in accepted for item in value)
        return value is not None and str(value) in accepted

    return series.apply(matches)


def filter_features(gdf, layer: LayerSpec):
    """Apply declarative tag filters without mutating the input GeoDataFrame.

    Include filters use OR semantics across keys. Exclude filters also use OR
    semantics and are applied after includes.
    """
    if gdf is None or getattr(gdf, "empty", True):
        return gdf
    result = gdf.copy()
    if layer.include_tags:
        include = None
        for key, values in layer.include_tags.items():
            mask = (
                _tag_mask(result[key], values)
                if key in result
                else result.geometry.notna() & False
            )
            include = mask if include is None else include | mask
        result = result[include].copy() if include is not None else result.iloc[0:0].copy()
    if layer.exclude_tags and not result.empty:
        exclude = None
        for key, values in layer.exclude_tags.items():
            mask = (
                _tag_mask(result[key], values)
                if key in result
                else result.geometry.notna() & False
            )
            exclude = mask if exclude is None else exclude | mask
        if exclude is not None:
            result = result[~exclude].copy()
    return result
