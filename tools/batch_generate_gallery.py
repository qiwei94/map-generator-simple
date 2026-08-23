"""预渲风格画廊：为内置城市批量渲染多组风格变体，供用户"先选后渲"。

产品动机：把参数决策与昂贵渲染解耦——用户先在画廊里看到同一城市
不同风格的差异（秒级浏览），选定后才触发正式 3MF 管线（分钟级）。

复用 aesthetic.CityHarness：
    prepare 一次（gdfs/elevation/profile 走 PipelineCache，二跑秒级命中）
    每风格只跑热路径 preprocess_layers + 纯 PIL 评审渲染（无 matplotlib）。

输出（output/style_gallery/<city>/）：
    <style>_topdown.png / <style>_height.png   每风格双视图
    contact_sheet.png                          风格拼图（一屏看差异）
    gallery_metadata.json                      profile + 每风格参数/指标分

用法：
    python tools/batch_generate_gallery.py                      # 全部城市
    python tools/batch_generate_gallery.py --cities westlake
    python tools/batch_generate_gallery.py --styles baseline,block_fill
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows GBK 控制台防 UnicodeEncodeError（其余字符集环境无影响）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(
        errors="replace", line_buffering=True, write_through=True)

from PIL import Image, ImageDraw, ImageFont

from aesthetic.config import PARAM_SPACE
from aesthetic.city_signature import analyze_city_signature
from aesthetic.metrics import compute_metrics
from aesthetic.presets import get_preset, list_presets
from aesthetic.rerun_harness import CityHarness
from aesthetic.review_render import render_review_bundle


# ─── 风格变体定义 ─────────────────────────────────────────────────────
# 每风格 = 基线 seed 上的参数偏移（delta 是"意图"，最终 clamp 进 PARAM_SPACE）。
# 语义面向用户："饱满块面 / 精细刻画 / 极简留白"，而非内部参数名。
STYLE_VARIANTS = {
    "baseline": {
        "label": "默认（规则引擎）",
        "desc": "城市画像自动推导的基线参数",
        "delta": {},
    },
    "block_fill": {
        "label": "饱满街区",
        "desc": "密度达标街区整块填充，体量感强",
        "delta": {
            "bo_mode": "block_fill",
            "building_density_threshold": ("set", 0.003),
            "building_count_threshold": ("set", 1),
            "building_height_mm_max": ("scale", 1.3),
        },
    },
    "dense_detail": {
        "label": "精细刻画",
        "desc": "保留全部小建筑与次级路网，细节密集",
        "delta": {
            "building_print_limit_m2": ("set", 1000.0),
            "building_simplify_tol_m": ("set", 5.0),
            "building_v2_road_tier": ("set", 5),
            "road_width_multiplier": ("set", 2.0),
            "aggregate_simplify_m": ("set", 10.0),
            "building_height_mm_max": ("scale", 0.8),
        },
    },
    "minimal": {
        "label": "极简留白",
        "desc": "只留主干路与地标建筑，海报感",
        "delta": {
            "building_print_limit_m2": ("set", 8000.0),
            "building_simplify_tol_m": ("set", 40.0),
            "building_v2_road_tier": ("set", 2),
            "road_width_multiplier": ("set", 8.0),
            "aggregate_simplify_m": ("set", 80.0),
            "building_height_mm_max": ("scale", 1.5),
        },
    },
}

# 山水区域不能复用城市海报的道路权重。保留相同 style key 以兼容前端
# 和历史任务，但标签与参数意图按场景重解释。
LANDSCAPE_STYLE_VARIANTS = {
    "baseline": {
        "label": "山水均衡",
        "desc": "以湖面与地形为主体，道路退到背景",
        "delta": {
            "building_v2_road_tier": ("set", 3),
            "road_width_multiplier": ("set", 2.0),
        },
    },
    "block_fill": {
        "label": "聚落点缀",
        "desc": "保留少量村镇体量，不用街区块面填满山野",
        "delta": {
            "bo_mode": "density_fill",
            "building_density_threshold": ("set", 0.01),
            "building_count_threshold": ("set", 2),
            "building_v2_road_tier": ("set", 3),
            "road_width_multiplier": ("set", 2.0),
            "building_height_mm_max": ("scale", 0.8),
        },
    },
    "dense_detail": {
        "label": "岸线与聚落",
        "desc": "保留更多岸边聚落与次级连接，但不强化公路",
        "delta": {
            "bo_mode": "oriented_bbox",
            "building_print_limit_m2": ("set", 1000.0),
            "building_simplify_tol_m": ("set", 8.0),
            "building_v2_road_tier": ("set", 4),
            "road_width_multiplier": ("set", 2.0),
            "aggregate_simplify_m": ("set", 16.0),
            "building_height_mm_max": ("scale", 0.75),
        },
    },
    "minimal": {
        "label": "山水留白",
        "desc": "只留主要交通和少量聚落，让湖湾与群山主导构图",
        "delta": {
            "bo_mode": "oriented_bbox",
            "building_print_limit_m2": ("set", 8000.0),
            "building_simplify_tol_m": ("set", 40.0),
            "building_v2_road_tier": ("set", 2),
            "road_width_multiplier": ("set", 2.0),
            "aggregate_simplify_m": ("set", 80.0),
            "building_height_mm_max": ("scale", 0.7),
        },
    },
}


def classify_scene_type(profile, requested_prototype: str) -> str:
    """Classify the visual scene from measured density, relief and water.

    The requested prototype is only a hint.  This keeps a sparse lake/mountain
    bbox from receiving dense-city road and block styling even when its OSM
    building coverage is poor.
    """
    road_density = float(getattr(
        profile, "road_density_km_per_km2", 0.0) or 0.0)
    # Some cities have poor OSM building coverage but a dense metropolitan
    # road network.  Do not misclassify them as mountains merely because the
    # requested prototype mentions terrain (Mexico City regression).
    urban_network = (
        profile.building_density >= 80
        and road_density >= 12
        and profile.water_ratio < 0.30
    )
    if requested_prototype == "skyline" or urban_network:
        return "urban"
    sparse = profile.building_density < 200
    nature_evidence = (
        profile.water_ratio >= 0.05
        or profile.elevation_range_m >= 100
        or profile.vegetation_ratio >= 0.10
    )
    if sparse and profile.water_ratio >= 0.08:
        return "water_landscape"
    if (sparse and nature_evidence
            or requested_prototype in ("landscape", "terrain")
            and profile.building_density < 500):
        return "landscape"
    return "urban"


def variants_for_scene(scene_type: str) -> dict:
    return (LANDSCAPE_STYLE_VARIANTS
            if scene_type in ("landscape", "water_landscape")
            else STYLE_VARIANTS)


def _clamp_to_space(name: str, value):
    """按 PARAM_SPACE 边界收口；不在空间内的参数（bo_mode）原样返回。"""
    if name not in PARAM_SPACE:
        return value
    lo, hi, _, _, is_int = PARAM_SPACE[name][:5]
    v = max(lo, min(hi, float(value)))
    return int(round(v)) if is_int else v


def build_style_params(seed: dict, delta: dict) -> dict:
    """基线 seed + 偏移指令 → 完整参数 dict（全部 clamp 合法）。"""
    params = dict(seed)
    for key, op in delta.items():
        if isinstance(op, tuple):
            kind, arg = op
            base = float(params.get(key, 0.0))
            if kind == "scale":
                params[key] = base * arg
            elif kind == "add":
                params[key] = base + arg
            elif kind == "set":
                params[key] = arg
        else:                      # 直接赋值（如 bo_mode）
            params[key] = op
    return {k: _clamp_to_space(k, v) for k, v in params.items()}


# ─── 拼图对照表 ───────────────────────────────────────────────────────

_CONTACT_SHEET_FONT_CANDIDATES = (
    # WSL can reuse the licensed fonts from its Windows host without copying
    # them into the repository or provisioning another package.
    "/mnt/c/Windows/Fonts/msyh.ttc",
    "/mnt/c/Windows/Fonts/simhei.ttf",
    # Native macOS workers.
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    # Common Linux package location, followed by legacy name lookup.
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "msyh.ttc",
    "simhei.ttf",
    "arial.ttf",
)


def _load_font(size: int):
    for name in _CONTACT_SHEET_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def make_contact_sheet(entries: list, out_path: str, tile: int = 640,
                       caption_h: int = 56) -> str:
    """entries: [(style_key, label, score, png_path)] → 横向拼图。"""
    n = len(entries)
    cols = min(n, 2)
    rows = (n + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile, rows * (tile + caption_h)),
                      (250, 250, 248))
    draw = ImageDraw.Draw(sheet)
    font = _load_font(22)
    for i, (key, label, score, path) in enumerate(entries):
        r, c = divmod(i, cols)
        x, y = c * tile, r * (tile + caption_h)
        img = Image.open(path).convert("RGB").resize((tile, tile),
                                                     Image.LANCZOS)
        sheet.paste(img, (x, y))
        caption = f"{label} ({key})  score={score:.2f}"
        draw.text((x + 12, y + tile + 14), caption, fill=(40, 40, 40),
                  font=font)
    sheet.save(out_path)
    return out_path


# ─── 单城市画廊 ───────────────────────────────────────────────────────

def generate_city_gallery(city: str, styles: list, out_root: str,
                          use_cache: bool = True) -> dict:
    preset = get_preset(city)
    out_dir = os.path.join(out_root, city)
    os.makedirs(out_dir, exist_ok=True)

    harness = CityHarness(preset, use_cache=use_cache)
    harness.prepare()
    seed = harness.seed_params()
    scene_type = classify_scene_type(harness.profile, preset.prototype)
    from aesthetic.framing import analyze_water_framing
    framing = analyze_water_framing(
        harness.ctx.get("water"), harness.ctx["bbox_local"],
        harness.profile.water_ratio)
    city_signature = analyze_city_signature(
        harness.ctx.get("roads"), harness.ctx.get("buildings"),
        harness.ctx["bbox_local"], harness.profile, framing, scene_type)
    topology = city_signature["road_topology"]
    if max(topology["ring_score"], topology["radial_score"],
           topology["grid_score"] * 0.85) >= 0.45:
        framing["recommended_size_km"] = 25
        framing["reason"] = (
            "检测到环状、放射状或网格化都市骨架，25 km 更能保留完整城市特征")
    framing["city_signature_score"] = city_signature["overall"]
    framing["ring_road_score"] = topology["ring_score"]
    framing["radial_road_score"] = topology["radial_score"]
    framing["grid_road_score"] = topology["grid_score"]
    variants = variants_for_scene(scene_type)
    print(f"  [gallery] scene_type={scene_type} "
          f"(prototype={preset.prototype}, "
          f"buildings={harness.profile.building_density:.1f}/km2, "
          f"water={harness.profile.water_ratio:.1%})")

    meta = {
        "city": city,
        "prototype": preset.prototype,
        "scene_type": scene_type,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "profile": harness.profile.to_dict(),
        "framing": framing,
        "city_signature": city_signature,
        "seed_params": seed,
        "styles": {},
    }
    sheet_entries = []

    for style in styles:
        spec = variants[style]
        params = build_style_params(seed, spec["delta"])
        t0 = time.time()
        try:
            layers = harness.run_round(params)
            bundle = render_review_bundle(
                layers, harness.ctx,
                road_width_multiplier=float(params["road_width_multiplier"]),
                out_dir=out_dir, tag=style, scene_type=scene_type)
            result = compute_metrics(layers, bundle, preset.prototype)
            score = result["overall"]
        except Exception as e:
            print(f"  [{city}/{style}] FAILED: {e}")
            meta["styles"][style] = {"label": spec["label"],
                                     "params": params, "error": str(e)}
            continue

        wall = time.time() - t0
        print(f"  [{city}/{style}] score={score:.2f} ({wall:.1f}s) "
              f"-> {os.path.basename(bundle['topdown'])}")
        meta["styles"][style] = {
            "label": spec["label"], "desc": spec["desc"],
            "params": params, "score": score,
            "metrics": result["metrics"],
            "details": result["details"],
            "renders": {"topdown": os.path.basename(bundle["topdown"]),
                        "height": os.path.basename(bundle["height"])},
            "wall_s": round(wall, 1),
        }
        sheet_entries.append((style, spec["label"], score, bundle["topdown"]))

    if sheet_entries:
        sheet_path = make_contact_sheet(
            sheet_entries, os.path.join(out_dir, "contact_sheet.png"))
        meta["contact_sheet"] = os.path.basename(sheet_path)
        print(f"  [{city}] contact sheet: {sheet_path}")

    meta_path = os.path.join(out_dir, "gallery_metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"  [{city}] metadata: {meta_path}")
    return meta


# ─── CLI ──────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="预渲风格画廊批量生成")
    ap.add_argument("--cities", default=None,
                    help=f"逗号分隔（默认全部: {','.join(list_presets())}）")
    ap.add_argument("--styles", default=None,
                    help=f"逗号分隔（默认全部: {','.join(STYLE_VARIANTS)}）")
    ap.add_argument("--out-dir",
                    default=os.path.join(_PROJECT_ROOT, "output",
                                         "style_gallery"))
    ap.add_argument("--no-cache", action="store_true",
                    help="禁用 PipelineCache（全量重算）")
    args = ap.parse_args()

    cities = ([c.strip() for c in args.cities.split(",") if c.strip()]
              if args.cities else list_presets())
    styles = ([s.strip() for s in args.styles.split(",") if s.strip()]
              if args.styles else list(STYLE_VARIANTS))
    for s in styles:
        if s not in STYLE_VARIANTS:
            ap.error(f"未知风格 '{s}'，可选: {list(STYLE_VARIANTS)}")

    print(f"[gallery] cities={cities} styles={styles}")
    t0 = time.time()
    for city in cities:
        print(f"\n{'=' * 60}\n  Gallery: {city}\n{'=' * 60}")
        generate_city_gallery(city, styles, args.out_dir,
                              use_cache=not args.no_cache)
    print(f"\n[gallery] all done in {time.time() - t0:.1f}s "
          f"-> {args.out_dir}")


if __name__ == "__main__":
    main()
