"""可计算美学指标（B 内核，无 AI 依赖）.

五个几何/统计维度，全部从 layers + 渲染 masks/DSM 直接计算：
- coverage:    建筑覆盖率是否在原型目标带（太空 vs 太糊）
- regularity:  密度规整度（网格单元覆盖率 CV，越均匀越高分）
- height_var:  高度错落（DSM p90-median 落差 / 原型目标）
- water:       水体实心度（最大水体面积占比，碎片越少越高分）
- landmark:    地标占比（n_BL/(n_BL+n_BO)）是否在目标带（核心建筑比重）
- road:        道路可见度（道路像素占比目标带，细灰线 vs 黑团）
- edge:        边缘几何精度（网格城市近直角拐角占比，惩罚简化锯齿）

每个指标归一化到 [0,1]，overall = 10 × 加权求和（权重按原型）。
"""

import numpy as np

from .config import PROTOTYPE_TARGETS


def _band_score(v: float, lo: float, hi: float, falloff: float) -> float:
    """三角隶属度：带内 1.0，带外线性衰减到 0。"""
    if lo <= v <= hi:
        return 1.0
    if v < lo:
        return max(0.0, 1.0 - (lo - v) / max(falloff, 1e-9))
    return max(0.0, 1.0 - (v - hi) / max(falloff, 1e-9))


def _coverage_metric(building_mask: np.ndarray, water_mask: np.ndarray) -> float:
    land = building_mask.size - int(water_mask.sum())
    if land <= 0:
        return 0.0
    return float(building_mask.sum() / land)


def _regularity_metric(building_mask: np.ndarray, water_mask: np.ndarray,
                       n_cells: int = 8) -> float:
    """8x8 网格单元覆盖率的变异系数（CV）→ 1 - CV（截断）。"""
    G = building_mask.shape[0]
    cell = G // n_cells
    covs = []
    for i in range(n_cells):
        for j in range(n_cells):
            b = building_mask[i * cell:(i + 1) * cell, j * cell:(j + 1) * cell]
            w = water_mask[i * cell:(i + 1) * cell, j * cell:(j + 1) * cell]
            if w.mean() > 0.5:      # 水域主导单元不参与密度规整评价
                continue
            covs.append(float(b.mean()))
    if len(covs) < 4:
        return 0.5                # 样本不足给中性分
    covs = np.asarray(covs)
    mean = covs.mean()
    if mean < 1e-6:
        return 0.0
    cv = float(covs.std() / mean)
    return float(np.clip(1.0 - cv / 1.5, 0.0, 1.0))


def _height_var_metric(dsm: np.ndarray, target_spread_mm: float) -> float:
    vals = dsm[dsm > 0]
    if vals.size < 100:
        return 0.0
    p90 = float(np.percentile(vals, 90))
    med = float(np.median(vals))
    spread = p90 - med
    return float(np.clip(spread / max(target_spread_mm, 1e-6), 0.0, 1.0))


def _water_metric(layers, water_mask: np.ndarray) -> float:
    if water_mask.mean() < 0.005:
        return 0.7                # 几乎无水 → 中性
    polys = [p for p in list(layers.WL) + list(layers.WO)
             if p is not None and not p.is_empty]
    if not polys:
        return 0.0                # mask 有水但图层没水 → 异常
    areas = np.array([p.area for p in polys])
    dominance = float(areas.max() / max(areas.sum(), 1e-9))
    return float(np.clip(dominance / 0.7, 0.0, 1.0))


def _edge_metric(layers, max_polys: int = 3000) -> float:
    """近直角拐角占比（网格城市轮廓应以 ~90° 为主；粗简化产生斜切尖角）。

    对 BL+BO 抽样，计算每个外环拐点的内角，统计落在 [75°,105°] 的比例。
    """
    polys = [p for p, _ in layers.BL] + list(layers.BO)
    if not polys:
        return 0.0
    if len(polys) > max_polys:
        idx = np.linspace(0, len(polys) - 1, max_polys).astype(int)
        polys = [polys[i] for i in idx]

    n_right = 0
    n_total = 0
    lo, hi = np.radians(75.0), np.radians(105.0)
    for p in polys:
        if p is None or p.is_empty or p.geom_type != "Polygon":
            continue
        try:
            coords = np.asarray(p.exterior.coords)
        except Exception:
            continue
        if len(coords) < 4:
            continue
        v1 = coords[1:-1] - coords[:-2]
        v2 = coords[2:] - coords[1:-1]
        n1 = np.hypot(v1[:, 0], v1[:, 1])
        n2 = np.hypot(v2[:, 0], v2[:, 1])
        ok = (n1 > 1e-6) & (n2 > 1e-6)
        if not ok.any():
            continue
        cosang = (v1[ok, 0] * v2[ok, 0] + v1[ok, 1] * v2[ok, 1]) / (n1[ok] * n2[ok])
        ang = np.arccos(np.clip(cosang, -1.0, 1.0))  # 转角（外角）
        internal = np.pi - ang                        # 内角
        n_right += int(((internal >= lo) & (internal <= hi)).sum())
        n_total += int(ok.sum())
    if n_total == 0:
        return 0.0
    return float(n_right / n_total)


def compute_metrics(layers, bundle: dict, prototype: str) -> dict:
    """返回 {"metrics": {name: [0,1]}, "overall": 0-10, "details": {...}}"""
    targets = PROTOTYPE_TARGETS.get(prototype, PROTOTYPE_TARGETS["classic"])
    bm, wm, dsm = bundle["building_mask"], bundle["water_mask"], bundle["dsm"]

    cov_raw = _coverage_metric(bm, wm)
    n_bl = len(layers.BL)
    n_total = n_bl + len(layers.BO)
    bl_ratio = n_bl / n_total if n_total > 0 else 0.0
    road_ratio = float(bundle["road_mask"].mean()) if "road_mask" in bundle else 0.0

    metrics = {
        # coverage 用单调 ramp（raw/hi）：当前管线覆盖率远低于带下限，
        # 带评分会永久 0 分、梯度消失；ramp 保证任何改善都有梯度。
        "coverage": float(np.clip(cov_raw / max(targets["coverage"][1], 1e-9), 0.0, 1.0)),
        "regularity": _regularity_metric(bm, wm),
        "height_var": _height_var_metric(dsm, targets["height_spread_mm"]),
        "water": _water_metric(layers, wm),
        "landmark": _band_score(bl_ratio, *targets["landmark_ratio"]),
        "road": _band_score(road_ratio, *targets["road_ratio"]),
        "edge": _edge_metric(layers),
    }
    weights = targets["weights"]
    overall = 10.0 * sum(weights[k] * metrics[k] for k in weights)

    return {
        "metrics": metrics,
        "overall": round(float(overall), 3),
        "details": {
            "coverage_raw": round(cov_raw, 4),
            "n_BL": n_bl,
            "n_BO": len(layers.BO),
            "bl_ratio": round(bl_ratio, 4),
            "road_px_ratio": round(road_ratio, 4),
            "water_px_ratio": round(float(wm.mean()), 4),
            "dsm_max_mm": round(float(dsm.max()), 3) if dsm.size else 0.0,
        },
    }
