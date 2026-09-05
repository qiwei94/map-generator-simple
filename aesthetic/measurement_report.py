"""Complete measurement-to-policy report for one generation run.

The report is deliberately observational.  It preserves every leaf value
produced by the measurement stage, records the resolved policy separately,
and states which consumers actually used each measurement family.  It never
feeds report values back into geometry generation.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


SCHEMA_VERSION = "pipeline-measurement-report-v2"
HTML_VERSION = "pipeline-measurement-report-html-v2"

STATUS_LABELS_ZH = {
    "ready": "就绪", "partial": "部分就绪", "pending": "等待中",
    "unavailable": "不可用", "not_applicable": "不适用", "error": "错误",
    "measured": "已完成测量", "generated_pending_validation": "已生成，待验收",
    "validated": "已验收", "measurement_error": "测量失败",
    "applied_hard_or_preprocess": "已在硬约束或预处理中生效",
    "applied_bounded_geometry": "已在有边界几何阶段生效",
    "applied_indirect_policy": "已通过下游策略间接生效",
    "policy_only": "仅形成策略建议", "diagnostic_only": "仅作诊断",
    "pending_acceptance": "等待验收", "not_wired": "尚未接入",
}

PHASE_LABELS_ZH = {
    "inputs": "输入与测量", "decisions": "策略决策",
    "preprocess": "预处理", "outcomes": "生成结果", "acceptance": "正式验收",
}

FAMILY_LABELS_ZH = {
    "run": "运行配置", "provenance": "数据来源证据",
    "source_inventory": "原始要素清单", "city_profile": "城市配置画像",
    "scene_character": "场景特征", "external_evidence": "交叉数据证据",
    "auto_parameters": "自动参数", "scene_policy": "场景生成策略",
    "realization_matrix": "策略落地矩阵", "road_roles": "道路角色",
    "water_roles": "水体角色", "layers": "可打印图层",
    "composition": "构图规格", "building_mass": "建筑中频形态",
    "height": "高度层级", "terrain": "地形",
    "water_relief": "水体高差", "block_base": "街区底座",
    "printability": "可打印性", "meshes": "语义网格",
    "validator": "项目验证器", "slicer": "切片器",
}


FAMILY_STATUSES = {
    "ready", "partial", "pending", "unavailable", "not_applicable", "error",
}

# This is the completeness contract.  A provider may be unavailable or not
# applicable for a particular crop, but a whole measurement family may never
# silently disappear from the report.
REQUIRED_FAMILIES = {
    "inputs": (
        "run", "provenance", "source_inventory", "city_profile",
        "scene_character", "external_evidence",
    ),
    "decisions": (
        "auto_parameters", "scene_policy", "realization_matrix",
    ),
    "preprocess": (
        "road_roles", "water_roles", "layers", "composition",
    ),
    "outcomes": (
        "building_mass", "height", "terrain", "water_relief",
        "block_base", "printability", "meshes",
    ),
    "acceptance": ("validator", "slicer"),
}


# Longest / most specific prefixes must come first.  The final broad rules
# guarantee that new fields are retained and visibly marked instead of being
# silently dropped when the measurement schema grows.
_IMPACT_RULES = (
    {
        "id": "building_block_grammar",
        "title": "建筑粒度与轮廓语法",
        "prefixes": ("scene_character.metrics.buildings.block_grammar",),
        "consumers": (
            "scene_policy.resolve_block_grammar_strategy",
            "building_mass_strategy.resolve_building_mass_policy",
        ),
        "decision_outputs": (
            "建筑聚合压力", "轮廓规整压力", "方向保护压力",
            "纹理补强压力", "独立建筑预算",
        ),
        "effect": (
            "在打印硬约束和道路/水体 topology 内调整匿名建筑的选择、"
            "聚合和轮廓规整；不复制参考 Demo 建筑。"
        ),
        "forbidden": ("道路/水体替换", "地形顶点", "全局 Z", "网格布尔"),
    },
    {
        "id": "building_distribution_quality",
        "title": "建筑分布完整性",
        "prefixes": ("scene_character.metrics.building_data_quality",),
        "consumers": (
            "scene_policy.resolve_scene_policy",
            "building_mass_strategy.apply_building_mass_to_layers",
        ),
        "decision_outputs": (
            "逐格 literal / neighbourhood mass / block-base support / open-space",
            "建筑中频策略是否激活",
        ),
        "effect": (
            "区分真正空地、建筑缺失区和数据完整街区；只有得到道路、邻域或"
            "交叉数据佐证的区域才允许承担补足密度的建筑中频。"
        ),
        "forbidden": ("凭空填满街区", "按城市名写死策略"),
    },
    {
        "id": "building_morphology",
        "title": "建筑可打印形态",
        "prefixes": ("scene_character.metrics.buildings",),
        "consumers": (
            "scene_policy.resolve_scene_policy",
            "building_height_hierarchy.apply_building_height_hierarchy",
            "building_mass_strategy.apply_building_mass_to_layers",
        ),
        "decision_outputs": (
            "喷嘴以下占比", "细长体压力", "建筑聚合方式",
            "高度所有者与视觉中心强度",
        ),
        "effect": (
            "决定匿名小楼是保留、聚合还是降为安静纹理，并限制独立细针建筑；"
            "可信高度只参与有界的相对层级。"
        ),
        "forbidden": ("直接照搬真实高度", "无边界合并"),
    },
    {
        "id": "terrain_landform",
        "title": "地貌身份",
        "prefixes": ("scene_character.metrics.terrain.landform",),
        "consumers": ("scene_policy.resolve_scene_policy",),
        "decision_outputs": (
            "山峰 / 山脊 / 火山口 / 谷地 / 平原得分", "城市或自然场景分类",
        ),
        "effect": (
            "决定地貌是否应成为主叙事，并为自然景观策略提供证据；当前城市"
            "pipeline 不允许这些分数直接修改 DEM 顶点。"
        ),
        "forbidden": ("LLM 控制 DEM", "报告值直接写入全局 Z"),
    },
    {
        "id": "terrain_relief",
        "title": "高程、坡度与起伏",
        "prefixes": ("scene_character.metrics.terrain",),
        "consumers": (
            "scene_policy.resolve_scene_policy",
            "building_height_hierarchy.apply_building_height_hierarchy",
            "formal terrain evidence gate",
        ),
        "decision_outputs": (
            "terrain score", "地形/建筑高度所有权", "无可信 DEM 时阻断正式输出",
        ),
        "effect": (
            "约束建筑相对高度并保护山体轮廓；真正的地形网格仍只来自 DEM 和"
            "打印机物理配置。"
        ),
        "forbidden": ("审美分数重写地形",),
    },
    {
        "id": "water_topology",
        "title": "水体拓扑与构图",
        "prefixes": ("scene_character.metrics.water_topology",),
        "consumers": (
            "scene_policy scene/archetype scoring",
            "building block-grammar strategy",
        ),
        "decision_outputs": (
            "海岸 / 河轴 / 水网 / 岛屿 / 汇流得分", "主体结构",
            "道路可见预算建议", "建筑中频目标粒度",
        ),
        "effect": (
            "影响场景身份、负空间优先级和建筑中频；水体几何仍由 OSM/现有"
            "水体管线决定，报告不会生成替代河岸线。"
        ),
        "forbidden": ("替换水体矢量", "直接控制布尔"),
    },
    {
        "id": "road_structure",
        "title": "道路骨架与城市结构",
        "prefixes": ("scene_character.metrics.road_structure",),
        "consumers": (
            "scene_policy scene/archetype scoring",
            "building block-grammar strategy",
        ),
        "decision_outputs": (
            "网格 / 环线 / 放射得分", "主体结构", "建筑方向保护",
            "道路显示预算建议",
        ),
        "effect": (
            "当前正式管线中会间接影响建筑中频和构图策略；道路预处理发生在"
            "ScenePolicy 之前，因此道路预算目前仍是审计建议，不会回改本次道路。"
        ),
        "forbidden": ("事后重写道路几何",),
    },
    {
        "id": "cross_source_urban",
        "title": "高德交叉城市证据",
        "prefixes": ("scene_character.metrics.external_urban",),
        "consumers": (
            "auto parameter resolver (preprocess 前的同源证据)",
            "scene_policy.resolve_scene_policy",
            "building_data_quality.resolve_local_building_quality",
        ),
        "decision_outputs": (
            "城市/野外纠偏", "花园城市识别", "建筑缺失区置信度",
        ),
        "effect": (
            "只做结构存在性和角色佐证；高德栅格不会进入网格、Z 或布尔运算。"
        ),
        "forbidden": ("把地图截图当作替代矢量",),
    },
    {
        "id": "data_confidence",
        "title": "数据一致性与置信度",
        "prefixes": ("scene_character.metrics.data_confidence",),
        "consumers": ("scene_policy warnings", "administrator review"),
        "decision_outputs": ("数据缺口警告", "是否需要人工/交叉验证"),
        "effect": (
            "当前只产生警告和审核证据，不会因为置信度标签单独改变几何。"
        ),
        "forbidden": ("把无报错等同于数据完整",),
    },
    {
        "id": "spatial_cells",
        "title": "8×8 空间分区测量",
        "prefixes": ("scene_character.cells",),
        "consumers": (
            "building_data_quality.resolve_local_building_quality",
            "height_emphasis_zones (显式实验时)",
        ),
        "decision_outputs": (
            "水岸/核心/网格/稀疏/疑似缺口角色", "逐格建筑表达策略",
            "可选高度强调区",
        ),
        "effect": (
            "把全城平均值拆成局部策略；只有激活的有界 consumer 才会改变建筑"
            "多边形或相对高度。"
        ),
        "forbidden": ("直接控制底层 mesh",),
    },
    {
        "id": "scene_summary",
        "title": "场景摘要与相对阈值",
        "prefixes": ("scene_character.summary",),
        "consumers": ("scene_policy classification and scoring",),
        "decision_outputs": ("场景类别", "原型", "主次结构", "诊断 traits"),
        "effect": (
            "部分摘要进入场景分类；traits 本身主要用于报告，不是独立几何开关。"
        ),
        "forbidden": ("把审美标签当作物理参数",),
    },
    {
        "id": "feature_inventory",
        "title": "原始要素与非零检查",
        "prefixes": (
            "source_features", "scene_character.feature_counts",
        ),
        "consumers": ("input validation", "scene classification", "reporting"),
        "decision_outputs": ("缺层警告/阻断", "建筑存在性", "回归基线"),
        "effect": (
            "验证道路、水体、建筑等关键层确实存在，避免把空输出当成功。"
        ),
        "forbidden": ("只检查异常退出",),
    },
    {
        "id": "printer_scale",
        "title": "模型比例与打印机硬约束",
        "prefixes": ("run",),
        "consumers": (
            "preprocess width/area gates", "mesh builders", "validator",
        ),
        "decision_outputs": (
            "真实米到模型毫米比例", "最小线宽/高度/间隙", "采样和聚合尺度",
        ),
        "effect": "硬约束，优先级高于所有审美策略。",
        "forbidden": ("被 ScenePolicy 或 LLM 覆盖",),
    },
    {
        "id": "preprocess_road_water",
        "title": "预处理道路与水体证据",
        "prefixes": ("preprocess_evidence",),
        "consumers": ("road/water builders", "composition report"),
        "decision_outputs": ("道路角色/线宽", "水体主次", "topology blocks"),
        "effect": (
            "这些是在 SceneCharacter 之前已经实际作用于图层的证据，报告按原样"
            "保存，以便区分前置参数与后置审美策略。"
        ),
        "forbidden": ("报告阶段回写几何",),
    },
    {
        "id": "other_scene_measurement",
        "title": "其他 SceneCharacter 测量",
        "prefixes": ("scene_character",),
        "consumers": ("diagnostic report",),
        "decision_outputs": ("完整审计上下文",),
        "effect": "完整保存；没有显式 consumer 的字段标记为诊断项。",
        "forbidden": ("静默丢弃新字段",),
    },
)


def _safe_value(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if any(token in lowered for token in (
            "password", "secret", "authorization", "token")):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {
            str(child_key): _safe_value(child, key=str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value
    if hasattr(value, "item"):
        try:
            return _safe_value(value.item())
        except (TypeError, ValueError):
            pass
    if hasattr(value, "to_dict"):
        try:
            return _safe_value(value.to_dict())
        except (TypeError, ValueError):
            pass
    return str(value)


def _flatten(value: Any, prefix: str, result: list[dict]) -> None:
    if isinstance(value, Mapping):
        if not value:
            result.append({"path": prefix, "value": {}})
            return
        for key in sorted(value, key=lambda item: str(item)):
            child = f"{prefix}.{key}" if prefix else str(key)
            _flatten(value[key], child, result)
        return
    if isinstance(value, list):
        if not value:
            result.append({"path": prefix, "value": []})
            return
        for index, item in enumerate(value):
            _flatten(item, f"{prefix}[{index}]", result)
        return
    result.append({"path": prefix, "value": value})


def _rule_for_path(path: str) -> dict:
    normalized = path.replace("[", ".[")
    for rule in _IMPACT_RULES:
        if any(normalized == prefix or normalized.startswith(prefix + ".")
               for prefix in rule["prefixes"]):
            return rule
    return {
        "id": "unclassified",
        "title": "未分类但已保存",
        "consumers": ("diagnostic report",),
        "decision_outputs": ("无",),
        "effect": "报告保留该字段；当前没有登记的生成 consumer。",
        "forbidden": ("静默丢弃",),
    }


def _realized_status(impact_id: str, decisions: Mapping,
                     outcomes: Mapping) -> str:
    policy = decisions.get("scene_policy", {}) or {}
    active = policy.get("activation") == "active"
    mass_active = (outcomes.get("building_mass", {}) or {}).get(
        "status") == "active"
    height_outcome = outcomes.get("height_hierarchy", {}) or {}
    height_active = height_outcome.get("status") == "active"
    height_geometry_changed = bool(
        height_outcome.get("geometry_changed")
        or int(height_outcome.get(
            "sub_nozzle_heroes_demoted_to_mass") or 0) > 0
    )
    emphasis_active = (outcomes.get("height_emphasis", {}) or {}).get(
        "status") == "active"
    if impact_id in {"printer_scale", "preprocess_road_water"}:
        return "applied_hard_or_preprocess"
    if impact_id == "feature_inventory":
        return "validation_and_classification"
    if impact_id in {
            "building_block_grammar", "building_distribution_quality",
            "building_morphology", "spatial_cells"}:
        if (mass_active or height_active or height_geometry_changed
                or emphasis_active):
            return "applied_bounded_geometry"
        return "policy_only" if active else "audit_only"
    if impact_id in {"terrain_relief", "terrain_landform"}:
        return "applied_height_or_gate" if height_active else (
            "policy_only" if active else "audit_only")
    if impact_id in {"road_structure", "water_topology",
                     "cross_source_urban", "scene_summary"}:
        return "applied_indirect_policy" if active else "audit_only"
    if impact_id == "data_confidence":
        return "warning_only"
    return "diagnostic_only"


def _family(data: Any, *, status: str | None = None,
            version: str | None = None, reason: str | None = None) -> dict:
    safe = _safe_value(data if data is not None else {})
    if status is None:
        status = "ready" if safe not in ({}, [], None) else "unavailable"
    if status not in FAMILY_STATUSES:
        raise ValueError(f"unsupported measurement family status: {status}")
    return {
        "status": status,
        "version": version,
        "reason": reason,
        "data": safe,
    }


def _mapping(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _build_realization_matrix(auto: Mapping, scene_policy: Mapping,
                              preprocess: Mapping,
                              outcomes: Mapping) -> list[dict]:
    """Declare what actually consumed each measurement/decision family.

    This registry is intentionally explicit.  A value existing in
    ``ResolvedParams`` or ``ScenePolicy`` is not evidence that it changed the
    current run.  Every row names its consumer and the outcome used to decide
    whether the effect was applied, partial, pending, or unconsumed.
    """

    resolved = _mapping(
        auto.get("resolved_params") or auto.get("resolved") or {})
    road_roles = _mapping(preprocess.get("road_roles"))
    water_roles = _mapping(preprocess.get("water_roles"))
    layer_outcome = _mapping(outcomes.get("printable_features"))
    meshes = _mapping(outcomes.get("meshes"))
    mass = _mapping(outcomes.get("building_mass"))
    hierarchy = _mapping(outcomes.get("height_hierarchy"))
    emphasis = _mapping(outcomes.get("height_emphasis"))
    terrain = _mapping(outcomes.get("terrain"))
    block_base = _mapping(outcomes.get("block_base_clearance"))
    validation = _mapping(outcomes.get("validation"))
    active_policy = scene_policy.get("activation") == "active"
    mass_active = mass.get("status") == "active"
    demoted_count = int(
        hierarchy.get("sub_nozzle_heroes_demoted_to_mass") or 0)
    terrain_status = str(terrain.get("status") or "")
    terrain_ready = bool(terrain) and terrain_status not in {
        "pending", "unavailable", "error",
    }
    mesh_ready = bool(meshes)

    rows: list[dict] = []

    def add(effect_id: str, title: str, *, measurement_paths=(),
            decision_paths=(), consumer_stage: str,
            consumer_symbol: str, target_fields=(), effect_kind: str,
            status: str, applied: bool = False, consumer_called=None,
            outcome_paths=(), mismatch_reason: str | None = None,
            hard_bounds=(), resolved_value: Any = None) -> None:
        rows.append({
            "effect_id": effect_id,
            "title": title,
            "measurement_paths": list(measurement_paths),
            "decision_paths": list(decision_paths),
            "consumer_stage": consumer_stage,
            "consumer_symbol": consumer_symbol,
            "target_fields": list(target_fields),
            "effect_kind": effect_kind,
            "realization_status": status,
            "consumer_called": consumer_called,
            "applied_to_current_run": bool(applied),
            "actual_outcome_paths": list(outcome_paths),
            "mismatch_reason": mismatch_reason,
            "hard_bounds": list(hard_bounds),
            "resolved_value": _safe_value(resolved_value),
        })

    preprocess_called = bool(preprocess)
    add(
        "printer.scale_hard_limits", "比例与打印机硬约束",
        measurement_paths=("inputs.run.scale_mm_per_m",
                           "inputs.run.printer_profile"),
        consumer_stage="S3/S8/S9",
        consumer_symbol="preprocess_layers + mesh builders + clearance gate",
        target_fields=("minimum widths", "minimum heights", "minimum gaps"),
        effect_kind="hard_constraint",
        status="applied" if preprocess_called else "pending",
        applied=preprocess_called, consumer_called=preprocess_called,
        outcome_paths=("preprocess.layers", "outcomes.block_base",
                       "outcomes.printability"),
        hard_bounds=("printer profile remains authority",),
    )
    add(
        "auto.terrain_gamma_relief", "自动地形曲线与起伏厚度",
        measurement_paths=("inputs.city_profile.elevation_range_m",
                           "inputs.city_profile.relief_ratio"),
        decision_paths=("decisions.auto_parameters.resolved_params.z_gamma",
                        "decisions.auto_parameters.resolved_params.terrain_thickness_mm"),
        consumer_stage="S8", consumer_symbol="build_deepseek_terrain",
        target_fields=("terrain.vertices.z",), effect_kind="geometry_z",
        status=("applied" if terrain_ready else
                "pending" if resolved else "not_applicable"),
        applied=terrain_ready, consumer_called=terrain_ready or mesh_ready,
        outcome_paths=("outcomes.terrain", "outcomes.meshes.terrain"),
        hard_bounds=("DEM remains source of height", "no global Z control"),
        resolved_value={key: resolved.get(key) for key in (
            "z_gamma", "terrain_thickness_mm")},
    )
    add(
        "auto.elevation_smoothing", "自动 DEM 平滑参数",
        measurement_paths=("inputs.city_profile.elevation_range_m",),
        decision_paths=("decisions.auto_parameters.resolved_params.elevation_smoothing_sigma",),
        consumer_stage="S1 (currently ordered too late)",
        consumer_symbol="DEM fetch/smooth",
        target_fields=("elevation grid",), effect_kind="declared_only",
        status="declared_unconsumed", applied=False, consumer_called=False,
        mismatch_reason=(
            "the DEM is fetched and smoothed before ResolvedParams is applied; "
            "the resolved value does not reprocess this run"),
        resolved_value=resolved.get("elevation_smoothing_sigma"),
    )
    add(
        "auto.building_selection", "自动建筑选择与聚合阈值",
        measurement_paths=("inputs.city_profile.building_density",
                           "inputs.city_profile.avg_building_area",
                           "inputs.city_profile.height_tag_coverage",
                           "inputs.city_profile.osm_quality"),
        decision_paths=("decisions.auto_parameters.resolved_params.flat_mode",
                        "decisions.auto_parameters.resolved_params.building_density_threshold",
                        "decisions.auto_parameters.resolved_params.building_print_limit_m2"),
        consumer_stage="S3", consumer_symbol="preprocess_layers/_extract_BL/_compute_BO",
        target_fields=("BL selection", "BO selection", "footprint simplify"),
        effect_kind="selection_and_geometry_xy",
        status=("applied_partial" if resolved and preprocess_called
                else "pending" if resolved else "not_applicable"),
        applied=bool(resolved and preprocess_called),
        consumer_called=preprocess_called,
        outcome_paths=("preprocess.layers",),
        mismatch_reason=(
            "building_print_limit override is explicit for BO; parts of BL "
            "classification still use the module snapshot"),
        hard_bounds=("printer width and area floors",),
        resolved_value={key: resolved.get(key) for key in (
            "flat_mode", "building_density_threshold",
            "building_count_threshold", "building_print_limit_m2",
            "building_simplify_tol_m")},
    )
    add(
        "auto.building_z", "自动建筑相对高度范围",
        measurement_paths=("inputs.city_profile.height_tag_coverage",
                           "inputs.city_profile.building_density"),
        decision_paths=("decisions.auto_parameters.resolved_params.building_height_mm_min",
                        "decisions.auto_parameters.resolved_params.building_height_mm_max"),
        consumer_stage="S3/S8", consumer_symbol="_resolve_building_height_role + building builder",
        target_fields=("BL relative relief",), effect_kind="geometry_z",
        status=("applied" if resolved and preprocess_called
                else "pending" if resolved else "not_applicable"),
        applied=bool(resolved and preprocess_called),
        consumer_called=preprocess_called,
        outcome_paths=("outcomes.height", "outcomes.meshes.buildings"),
        hard_bounds=("printer layer height", "height hierarchy may only reduce"),
        resolved_value={key: resolved.get(key) for key in (
            "building_height_mm_min", "building_height_mm_max", "flat_mode")},
    )
    add(
        "auto.road_roles", "自动道路层级、预算与线宽",
        measurement_paths=("inputs.city_profile.road_density_km_per_km2",
                           "inputs.city_profile.area_km2",
                           "inputs.city_profile.osm_quality"),
        decision_paths=("decisions.auto_parameters.resolved_params.building_v2_road_tier",
                        "decisions.auto_parameters.resolved_params.road_width_multiplier"),
        consumer_stage="S3", consumer_symbol="select_road_roles + preprocess_layers",
        target_fields=("topology roads", "structural roads", "visible roads",
                       "road widths", "city blocks"),
        effect_kind="selection_and_geometry_xy",
        status="applied" if road_roles else "pending",
        applied=bool(road_roles), consumer_called=bool(road_roles),
        outcome_paths=("preprocess.road_roles", "preprocess.layers"),
        hard_bounds=("nozzle footprint", "ink budget", "continuity rules"),
        resolved_value={key: resolved.get(key) for key in (
            "building_v2_road_tier", "road_filter_tier",
            "road_width_multiplier")},
    )
    add(
        "auto.brick_xy", "建筑表面砖化参数",
        measurement_paths=("inputs.city_profile.building_density",),
        decision_paths=("decisions.auto_parameters.resolved_params.brick_perlin_amp",
                        "decisions.auto_parameters.resolved_params.brick_corner_r_m"),
        consumer_stage="S8", consumer_symbol="build_deepseek_buildings_v3",
        target_fields=("building mesh XY surface transform",),
        effect_kind="geometry_xy",
        status="applied" if mesh_ready else "pending",
        applied=mesh_ready, consumer_called=mesh_ready,
        outcome_paths=("outcomes.meshes.buildings",),
        hard_bounds=("source/topology footprint remains authority",),
        resolved_value={key: resolved.get(key) for key in (
            "brick_perlin_amp", "brick_corner_r_m")},
    )
    for effect_id, title, parameter, reason in (
        ("auto.aggregate_buffer_declared", "建筑聚合缓冲参数",
         "building_aggregate_buffer_m", "no formal v3 consumer reads this value"),
        ("auto.water_declared", "自动水体细节参数",
         "water_high_detail", "formal v3 water geometry uses role and printer gates instead"),
        ("auto.vegetation_declared", "自动植被参数",
         "vegetation_enabled", "植被覆盖层默认关闭；正式启用由 CLI --vegetation 控制"),
    ):
        add(
            effect_id, title,
            measurement_paths=("inputs.city_profile",),
            decision_paths=(f"decisions.auto_parameters.resolved_params.{parameter}",),
            consumer_stage="none in current formal path",
            consumer_symbol="unwired ResolvedParams field",
            target_fields=(), effect_kind="declared_only",
            status="declared_unconsumed", applied=False,
            consumer_called=False, mismatch_reason=reason,
            resolved_value=resolved.get(parameter),
        )
    add(
        "preprocess.road_roles", "道路候选、连续性与可见集合",
        measurement_paths=("preprocess.road_roles",),
        consumer_stage="S3/S8", consumer_symbol="road role selector + road builder",
        target_fields=("layers.roads", "city_blocks", "block_base cuts"),
        effect_kind="selection_and_geometry_xy",
        status="applied" if road_roles else "unavailable",
        applied=bool(road_roles), consumer_called=bool(road_roles),
        outcome_paths=("preprocess.layers", "outcomes.meshes.roads"),
        hard_bounds=("candidate >= selected", "selected roads must be non-zero when expected"),
    )
    add(
        "preprocess.water_roles", "水体候选、补缝与主次集合",
        measurement_paths=("preprocess.water_roles",),
        consumer_stage="S3/S8", consumer_symbol="water role selector + water builder",
        target_fields=("WL", "WO", "water topology cuts"),
        effect_kind="selection_and_geometry_xy",
        status="applied" if water_roles else "unavailable",
        applied=bool(water_roles), consumer_called=bool(water_roles),
        outcome_paths=("preprocess.layers", "outcomes.water_relief",
                       "outcomes.meshes.water"),
        hard_bounds=("source/native width evidence", "printer gap floor"),
    )
    add(
        "scene.local_building_strategy", "逐格建筑表达策略",
        measurement_paths=("inputs.scene_character.metrics.building_data_quality",
                           "inputs.scene_character.cells"),
        decision_paths=("decisions.scene_policy.roles.buildings.local_strategy",),
        consumer_stage="S6", consumer_symbol="apply_building_mass_to_layers",
        target_fields=("BO quiet/urban/sparse role and outline",),
        effect_kind="selection_and_geometry_xy",
        status="applied" if mass_active else (
            "policy_only" if active_policy else "not_applicable"),
        applied=mass_active, consumer_called=active_policy,
        outcome_paths=("outcomes.building_mass",),
        mismatch_reason=(None if mass_active else
                         "a resolved cell label alone does not change geometry"),
        hard_bounds=("existing road/water topology blocks", "source support"),
    )
    add(
        "scene.block_grammar", "建筑粒度与轮廓语法",
        measurement_paths=("inputs.scene_character.metrics.buildings.block_grammar",
                           "inputs.scene_character.metrics.buildings.regularization_pressure"),
        decision_paths=("decisions.scene_policy.roles.buildings.block_grammar_strategy",),
        consumer_stage="S6", consumer_symbol="resolve_building_mass_policy + building mass candidate",
        target_fields=("BO merge", "outline", "orientation", "component size"),
        effect_kind="geometry_xy",
        status="applied" if mass_active else (
            "policy_only" if active_policy else "not_applicable"),
        applied=mass_active, consumer_called=active_policy,
        outcome_paths=("outcomes.building_mass",),
        hard_bounds=("printer floor", "area growth cap", "topology clip"),
    )
    landscape = _mapping(scene_policy.get("landscape_strategy"))
    add(
        "scene.mass_activation", "建筑中频激活门",
        measurement_paths=("inputs.scene_character.summary",
                           "inputs.scene_character.metrics.building_data_quality"),
        decision_paths=("decisions.scene_policy.scene_class",
                        "decisions.scene_policy.landscape_strategy.enabled",
                        "decisions.scene_policy.garden_city_strategy.enabled"),
        consumer_stage="S6", consumer_symbol="apply_building_mass_to_layers",
        target_fields=("whether BO replacement/enrichment is attempted",),
        effect_kind="gate",
        status=("applied" if mass_active else
                "gate_prevented_new_mass" if landscape.get("enabled")
                else "not_activated"),
        applied=mass_active, consumer_called=active_policy,
        outcome_paths=("outcomes.building_mass.status",),
        mismatch_reason=(
            "landscape mode prevents a new mass replacement; it does not "
            "delete baseline BO" if landscape.get("enabled") else None),
    )
    add(
        "scene.hero_width_demotion", "喷嘴以下独立建筑降级",
        measurement_paths=("inputs.run.printer_profile.extrusion_width_mm",
                           "inputs.scene_character.metrics.buildings"),
        decision_paths=("decisions.scene_policy.activation",),
        consumer_stage="S6", consumer_symbol="apply_building_height_hierarchy",
        target_fields=("BL -> BO semantic ownership",),
        effect_kind="selection_and_geometry_xy",
        status="applied" if demoted_count > 0 else (
            "checked_no_change" if active_policy else "not_applicable"),
        applied=demoted_count > 0, consumer_called=active_policy,
        outcome_paths=("outcomes.height.height_hierarchy.sub_nozzle_heroes_demoted_to_mass",),
        hard_bounds=("extrusion width",), resolved_value=demoted_count,
    )
    hierarchy_status = str(hierarchy.get("status") or "")
    add(
        "scene.terrain_height_owner", "地貌主导下的建筑高度层级",
        measurement_paths=("inputs.scene_character.metrics.terrain",
                           "inputs.scene_character.metrics.terrain.landform"),
        decision_paths=("decisions.scene_policy.archetype",),
        consumer_stage="S6", consumer_symbol="apply_building_height_hierarchy",
        target_fields=("BL relative height cap", "formal terrain evidence gate"),
        effect_kind="geometry_z_and_gate",
        status=("applied" if hierarchy_status == "active" else
                "gate_failed" if hierarchy_status == "invalid_terrain_evidence"
                else "policy_only" if active_policy else "not_applicable"),
        applied=hierarchy_status == "active", consumer_called=active_policy,
        outcome_paths=("outcomes.height.height_hierarchy",
                       "outcomes.terrain"),
        mismatch_reason=(
            "SceneCharacter scores choose ownership; exact cap values come "
            "from the clipped terrain mesh, not from report scores"),
        hard_bounds=("height may only decrease", "printer layer floor"),
    )
    add(
        "scene.experimental_height_zones", "实验性局部高度强调",
        measurement_paths=("inputs.scene_character.cells",),
        decision_paths=("decisions.scene_policy.roles.buildings.local_strategy",),
        consumer_stage="S6", consumer_symbol="apply_height_emphasis_zones",
        target_fields=("selected BL promotions",), effect_kind="geometry_z",
        status="applied" if emphasis.get("status") == "active" else (
            "not_requested" if not emphasis.get("activation") else "inactive"),
        applied=emphasis.get("status") == "active",
        consumer_called=emphasis.get("status") == "active",
        outcome_paths=("outcomes.height.height_emphasis",),
        hard_bounds=("explicit CLI experiment only",),
    )
    for effect_id, title, policy_path in (
        ("scene.roads_declared", "ScenePolicy 道路预算", "roles.roads"),
        ("scene.water_declared", "ScenePolicy 水体优先级", "roles.water"),
        ("scene.terrain_role_declared", "ScenePolicy 地形角色", "roles.terrain"),
        ("scene.density_declared", "ScenePolicy 安静纹理预算", "roles.density"),
        ("scene.block_base_declared", "ScenePolicy Block Base 建议", "roles.buildings.block_base_policy"),
    ):
        add(
            effect_id, title,
            measurement_paths=("inputs.scene_character",),
            decision_paths=(f"decisions.scene_policy.{policy_path}",),
            consumer_stage="after S3 preprocess",
            consumer_symbol="no current formal geometry consumer",
            target_fields=(), effect_kind="declared_only",
            status="declared_unconsumed", applied=False,
            consumer_called=False,
            mismatch_reason=(
                "the policy is resolved after road/water/block-base "
                "preprocessing and does not retroactively rewrite this run"),
        )
    add(
        "acceptance.validator", "项目 3MF 验证器",
        measurement_paths=("outcomes.meshes",),
        consumer_stage="S11", consumer_symbol="tools/validate_3mf.py",
        target_fields=("errors", "warnings", "rule results"),
        effect_kind="acceptance_gate",
        status=str(validation.get("status") or "pending"),
        applied=validation.get("status") == "validated",
        consumer_called=validation.get("status") not in {None, "pending"},
        outcome_paths=("acceptance.validator",),
        mismatch_reason=(None if validation.get("status") == "validated" else
                         "3MF export success is not formal acceptance"),
    )
    if block_base:
        add(
            "preprocess.block_base_clearance", "Block Base 最终间隙",
            measurement_paths=("inputs.run.printer_profile",),
            consumer_stage="S9", consumer_symbol="block-base clearance cut and gate",
            target_fields=("final block-base geometry",),
            effect_kind="geometry_xy_and_gate",
            status="applied", applied=True, consumer_called=True,
            outcome_paths=("outcomes.block_base",),
            hard_bounds=("verified gap >= configured printable gap",),
        )
    return rows


def _family_status_from_payload(payload: Mapping, *,
                                default: str = "ready") -> str:
    status = str(payload.get("status") or "").lower()
    if status in {"error", "failed", "invalid"}:
        return "error"
    if status in {"pending", "queued", "running"}:
        return "pending"
    if status in {"unavailable", "missing"}:
        return "unavailable"
    if status in {"not_applicable", "inactive", "not_requested"}:
        return "not_applicable"
    return default


def _build_families(*, run: Mapping, source_features: Mapping,
                    scene_character: Mapping, scene_policy: Mapping,
                    auto: Mapping, preprocess: Mapping, outcomes: Mapping,
                    realization_matrix: list[dict],
                    provenance: Mapping | None = None,
                    acceptance: Mapping | None = None) -> dict:
    profile = _mapping(auto.get("profile"))
    external = _mapping(preprocess.get("amap_salience"))
    external_native_status = str(external.get("status") or "").lower()
    if external_native_status == "ready":
        external_family_status = "ready"
    elif external_native_status in {
            "disabled", "not_applicable", "out_of_scope"} or not external:
        external_family_status = "not_applicable"
    elif external_native_status in {"unavailable", "missing"}:
        external_family_status = "unavailable"
    elif external_native_status in {"error", "failed"}:
        external_family_status = "error"
    else:
        external_family_status = "partial"
    composition = _mapping(preprocess.get("composition_spec"))
    layer_values = _mapping(outcomes.get("printable_features"))
    water_relief_data = _mapping(outcomes.get("water_relief"))
    water_relief_materialized = bool(
        water_relief_data.get("surface_levels_mm")
        or water_relief_data.get("surface_shells")
        or int(water_relief_data.get("carved_vertex_count") or 0) > 0
        or int(water_relief_data.get("carved_terrain_vertices") or 0) > 0
    )
    water_expected = bool(
        int(layer_values.get("water_polygons") or 0) > 0
        or int(layer_values.get("water_landmarks") or 0) > 0
    )
    height_data = {
        "height_hierarchy": _mapping(outcomes.get("height_hierarchy")),
        "height_emphasis": _mapping(outcomes.get("height_emphasis")),
        "height_sources": _mapping(outcomes.get("height_sources")),
        "height_store": _mapping(outcomes.get("height_store")),
        "height_mapping": _mapping(outcomes.get("height_mapping")),
    }
    acceptance = _mapping(acceptance or {})
    validator = _mapping(
        acceptance.get("validator") or outcomes.get("validation") or {})
    slicer = _mapping(
        acceptance.get("slicer") or outcomes.get("slicer") or {})
    if not validator:
        validator = {"status": "pending", "reason": "not run yet"}
    if not slicer:
        slicer = {"status": "pending", "reason": "not run yet"}
    return {
        "inputs": {
            "run": _family(run, version="run-context-v1"),
            "provenance": _family(
                provenance or {
                    "pbf": {"basename": run.get("pbf")},
                    "note": "strong source fingerprints were not supplied",
                }, status="partial", version="source-provenance-v1",
                reason="runtime currently supplies only bounded provenance"),
            "source_inventory": _family(
                source_features, version="source-inventory-v1"),
            "city_profile": _family(
                profile,
                status="ready" if profile else "not_applicable",
                version="city-profile-native"),
            "scene_character": _family(
                scene_character,
                status=_family_status_from_payload(scene_character),
                version=str(scene_character.get("version") or "unknown")),
            "external_evidence": _family(
                external,
                status=external_family_status,
                version=str(external.get("version") or "evidence-native"),
                reason=(None if external else
                        "no cross-source guide was available for this crop")),
        },
        "decisions": {
            "auto_parameters": _family(
                auto, status="ready" if auto else "not_applicable",
                version="resolved-params-native"),
            "scene_policy": _family(
                scene_policy,
                status="ready" if scene_policy else "unavailable",
                version=str(scene_policy.get("policy_version") or "unknown")),
            "realization_matrix": _family(
                realization_matrix, version="effect-registry-v1"),
        },
        "preprocess": {
            "road_roles": _family(
                _mapping(preprocess.get("road_roles")),
                version="road-role-evidence-native"),
            "water_roles": _family(
                _mapping(preprocess.get("water_roles")),
                version="water-role-evidence-native"),
            "layers": _family(
                layer_values,
                status="ready" if layer_values else "pending",
                version="layer-evidence-v1"),
            "composition": _family(
                composition,
                status="ready" if composition else "pending",
                version=str(composition.get("schema_version") or "unknown")),
        },
        "outcomes": {
            "building_mass": _family(
                _mapping(outcomes.get("building_mass")),
                status=_family_status_from_payload(
                    _mapping(outcomes.get("building_mass")),
                    default="ready"),
                version="building-mass-evidence-native"),
            "height": _family(
                height_data,
                status=("ready" if any(height_data.values()) else "pending"),
                version="height-outcomes-v1"),
            "terrain": _family(
                _mapping(outcomes.get("terrain")),
                status=("ready" if outcomes.get("terrain") else "pending"),
                version="terrain-evidence-native"),
            "water_relief": _family(
                water_relief_data,
                status=("ready" if water_relief_materialized else
                        "pending" if water_expected else "not_applicable"),
                version="water-relief-evidence-native"),
            "block_base": _family(
                _mapping(outcomes.get("block_base_clearance")),
                status=("ready" if outcomes.get("block_base_clearance")
                        else "pending"),
                version="block-base-clearance-native"),
            "printability": _family(
                _mapping(outcomes.get("printability")),
                status=("ready" if outcomes.get("printability") else "pending"),
                version="printability-report-native"),
            "meshes": _family(
                _mapping(outcomes.get("meshes")),
                status="ready" if outcomes.get("meshes") else "pending",
                version="mesh-summary-v1"),
        },
        "acceptance": {
            "validator": _family(
                validator,
                status=_family_status_from_payload(
                    validator, default=("ready" if validator else "pending")),
                version=str(validator.get("version") or "project-validator")),
            "slicer": _family(
                slicer,
                status=_family_status_from_payload(
                    slicer, default=("ready" if slicer else "pending")),
                version=str(slicer.get("tool_version") or "unknown")),
        },
    }


def _impact_chains(index: list[dict], decisions: Mapping,
                   outcomes: Mapping) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    rules: dict[str, dict] = {}
    for item in index:
        rule = _rule_for_path(item["path"])
        item["impact_id"] = rule["id"]
        item["impact_title"] = rule["title"]
        grouped.setdefault(rule["id"], []).append(item)
        rules[rule["id"]] = rule
    result = []
    for impact_id, items in grouped.items():
        rule = rules[impact_id]
        result.append({
            "id": impact_id,
            "title": rule["title"],
            "measurement_leaf_count": len(items),
            "measurement_prefixes": list(rule.get("prefixes", ())),
            "consumers": list(rule["consumers"]),
            "decision_outputs": list(rule["decision_outputs"]),
            "realized_status": _realized_status(
                impact_id, decisions, outcomes),
            "effect": rule["effect"],
            "forbidden_controls": list(rule["forbidden"]),
            "examples": items[:6],
        })
    order = {rule["id"]: index for index, rule in enumerate(_IMPACT_RULES)}
    return sorted(result, key=lambda item: (
        order.get(item["id"], len(order)), item["id"]))


def build_measurement_report(
    *,
    run: Mapping,
    source_features: Mapping,
    scene_character: Mapping,
    scene_policy: Mapping,
    auto_parameter_evidence: Mapping | None = None,
    preprocess_evidence: Mapping | None = None,
    generation_outcomes: Mapping | None = None,
    artifacts: Mapping | None = None,
    provenance: Mapping | None = None,
    acceptance_evidence: Mapping | None = None,
    status: str = "measured",
    generated_at: str | None = None,
) -> dict:
    """Build a lossless, searchable measurement and impact report."""

    if status not in {
            "measured", "generated_pending_validation", "validated",
            "measurement_error"}:
        raise ValueError("unsupported measurement report status")
    safe_run = _safe_value(run)
    raw_measurements = {
        "run": safe_run,
        "source_features": _safe_value(source_features),
        "scene_character": _safe_value(scene_character),
        "preprocess_evidence": _safe_value(preprocess_evidence or {}),
    }
    decisions = {
        "auto_parameters": _safe_value(auto_parameter_evidence or {}),
        "scene_policy": _safe_value(scene_policy),
    }
    outcomes = _safe_value(generation_outcomes or {})
    realization_matrix = _build_realization_matrix(
        _mapping(auto_parameter_evidence or {}),
        _mapping(scene_policy),
        _mapping(preprocess_evidence or {}),
        _mapping(generation_outcomes or {}),
    )
    decisions["realization_matrix"] = _safe_value(realization_matrix)
    measurement_index: list[dict] = []
    leaf_counts_by_tree = {}
    for tree, values in (
            ("inputs", raw_measurements),
            ("decisions", decisions),
            ("outcomes", outcomes)):
        before = len(measurement_index)
        for root, value in values.items():
            tree_items: list[dict] = []
            _flatten(value, root, tree_items)
            for item in tree_items:
                item["tree"] = tree
                measurement_index.append(item)
        leaf_counts_by_tree[tree] = len(measurement_index) - before
    impact_chains = _impact_chains(measurement_index, decisions, outcomes)
    counts = Counter(item["impact_id"] for item in measurement_index)
    unclassified = [
        item["path"] for item in measurement_index
        if item["impact_id"] == "unclassified"
    ]
    families = _build_families(
        run=safe_run,
        source_features=_safe_value(source_features),
        scene_character=_safe_value(scene_character),
        scene_policy=_safe_value(scene_policy),
        auto=_safe_value(auto_parameter_evidence or {}),
        preprocess=_safe_value(preprocess_evidence or {}),
        outcomes=outcomes,
        realization_matrix=realization_matrix,
        provenance=_safe_value(provenance or {}),
        acceptance=_safe_value(acceptance_evidence or {}),
    )
    family_statuses = {
        f"{phase}.{name}": family["status"]
        for phase, required in REQUIRED_FAMILIES.items()
        for name in required
        for family in (families[phase][name],)
    }
    missing_families = [
        path for path, family_status in family_statuses.items()
        if family_status == "unavailable"
    ]
    canonical_indexes: dict[str, list[dict]] = {}
    for phase, phase_families in families.items():
        phase_index: list[dict] = []
        for family_name, family in phase_families.items():
            leaves: list[dict] = []
            _flatten(
                family.get("data"),
                f"measurements.{phase}.{family_name}.data",
                leaves,
            )
            for leaf in leaves:
                leaf["phase"] = phase
                leaf["family"] = family_name
                data_prefix = f"measurements.{phase}.{family_name}.data"
                suffix = leaf["path"].removeprefix(data_prefix)
                rule = _rule_for_path(f"{family_name}{suffix}")
                leaf["impact_id"] = rule["id"]
                leaf["impact_title"] = rule["title"]
            phase_index.extend(leaves)
        canonical_indexes[phase] = phase_index
    canonical_leaf_count = sum(
        len(items) for items in canonical_indexes.values())
    return {
        "schema_version": SCHEMA_VERSION,
        "html_version": HTML_VERSION,
        "language": "zh-CN",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "status_label_zh": STATUS_LABELS_ZH.get(status, status),
        "purpose": (
            "complete measurement, decision and realized-impact audit; "
            "never a geometry control surface"
        ),
        "purpose_zh": (
            "完整记录测量、策略决策与实际作用结果；报告只读，"
            "不会反向控制几何生成"
        ),
        "labels_zh": {
            "phases": PHASE_LABELS_ZH,
            "families": FAMILY_LABELS_ZH,
            "statuses": STATUS_LABELS_ZH,
        },
        "city": safe_run.get("city") if isinstance(safe_run, Mapping) else None,
        "coverage": {
            "measurement_leaf_count": len(measurement_index),
            "classified_leaf_count": len(measurement_index) - len(unclassified),
            "unclassified_leaf_count": len(unclassified),
            "unclassified_paths": unclassified,
            "leaf_counts_by_impact": dict(sorted(counts.items())),
            "leaf_counts_by_tree": leaf_counts_by_tree,
            "required_families": {
                phase: list(names)
                for phase, names in REQUIRED_FAMILIES.items()
            },
            "family_statuses": family_statuses,
            "missing_or_unavailable_families": missing_families,
            "canonical_leaf_counts_by_phase": {
                phase: len(items)
                for phase, items in canonical_indexes.items()
            },
            "canonical_measurement_leaf_count": canonical_leaf_count,
            "contract": (
                "required families never disappear; legacy measurement_index "
                "covers inputs, decisions and outcomes; indexes covers every "
                "canonical family data leaf"
            ),
        },
        "measurements": families,
        "indexes": canonical_indexes,
        "raw_measurements": raw_measurements,
        "resolved_decisions": decisions,
        "generation_outcomes": outcomes,
        "realization_matrix": _safe_value(realization_matrix),
        "impact_chains": impact_chains,
        "measurement_index": measurement_index,
        "artifacts": _safe_value(artifacts or {}),
    }


def _atomic_write(path: Path, payload: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)
    return str(path)


def _safe_report_stem(stem: str) -> str:
    value = str(stem).strip()
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError("measurement report stem must be a plain filename")
    return value


def write_measurement_report_json(
    output_dir, report: Mapping, *, stem: str = "pipeline_measurement_report",
) -> str:
    path = Path(output_dir) / f"{_safe_report_stem(stem)}.json"
    payload = json.dumps(
        _safe_value(report), ensure_ascii=False, indent=2,
        sort_keys=True) + "\n"
    return _atomic_write(path, payload)


def _html_document(report: Mapping) -> str:
    payload = json.dumps(
        _safe_value(report), ensure_ascii=False, separators=(",", ":"))
    payload = (payload.replace("<", "\\u003c").replace(">", "\\u003e")
               .replace("&", "\\u0026"))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>地图模型生成完整测量报告</title>
<style>
:root{{--paper:#f4f1e9;--ink:#191815;--muted:#736f66;--line:#d6d0c3;
--accent:#ba4b2d;--good:#2f6e55;--warn:#9a6b19}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);
font:14px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:1500px;margin:auto;padding:32px}}h1{{font:700 34px/1.1 ui-serif,serif;
margin:0 0 10px}}h2{{font-size:19px;margin:30px 0 12px}}p{{margin:6px 0}}
.muted{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:20px 0}}
.card,.panel{{border:1px solid var(--line);background:#fbfaf6;padding:14px}}
.card b{{display:block;font-size:23px}}.status{{color:var(--good)}}
input{{width:100%;padding:12px;border:1px solid var(--line);background:white;font:inherit}}
.table-wrap{{overflow:auto;border:1px solid var(--line);background:#fff}}
table{{width:100%;border-collapse:collapse;min-width:900px}}th,td{{text-align:left;
vertical-align:top;padding:9px 10px;border-bottom:1px solid #e7e2d8}}th{{position:sticky;top:0;background:#eee9df;z-index:1}}
code{{font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}}
.badge{{display:inline-block;border:1px solid var(--line);padding:2px 7px;margin:1px 3px 1px 0;border-radius:999px;background:#f7f4ed}}
.impact{{border-left:4px solid var(--accent);margin:9px 0}}details{{border:1px solid var(--line);background:#fbfaf6;margin:8px 0}}
summary{{cursor:pointer;padding:12px;font-weight:650}}pre{{white-space:pre-wrap;word-break:break-word;padding:0 12px 12px;max-height:700px;overflow:auto}}
@media(max-width:800px){{main{{padding:18px}}.cards{{grid-template-columns:1fr 1fr}}}}
</style></head><body><main>
<h1>地图模型生成完整测量报告</h1><p id="subtitle" class="muted"></p>
<div class="cards"><div class="card"><span>城市</span><b id="city">—</b></div>
<div class="card"><span>测量叶子项</span><b id="leafCount">0</b></div>
<div class="card"><span>影响链</span><b id="impactCount">0</b></div>
<div class="card"><span>状态</span><b id="status" class="status">—</b></div></div>
<h2>测量字段族覆盖</h2><div id="families" class="panel"></div>
<h2>测量 → 决策 → 实际消费者</h2><p class="muted">“字段已算出”不等于“本次已生效”。这里按真实调用链区分已应用、部分应用、仅策略、未接线与待验收。</p>
<div class="table-wrap"><table><thead><tr><th>作用链</th><th>状态</th><th>实际执行模块</th><th>本次作用</th><th>结果证据 / 差异</th></tr></thead><tbody id="realizations"></tbody></table></div>
<h2>测量如何影响生成（类别说明）</h2><div id="impacts"></div>
<h2>全部测量项</h2><p class="muted">每个原始测量叶子都在这里；未登记 consumer 的项目会明确标为诊断项。</p>
<input id="search" type="search" placeholder="搜索路径、数值或影响类别…">
<div class="table-wrap"><table><thead><tr><th>路径</th><th>值</th><th>影响类别</th></tr></thead><tbody id="rows"></tbody></table></div>
<h2>完整原始证据</h2>
<details><summary>规范化测量族（技术字段）</summary><pre id="canonical"></pre></details>
<details><summary>原始测量证据（技术字段）</summary><pre id="raw"></pre></details>
<details><summary>策略决策证据（技术字段）</summary><pre id="decisions"></pre></details>
<details><summary>生成结果与验收证据（技术字段）</summary><pre id="outcomes"></pre></details>
</main><script>const report={payload};
const esc=s=>String(s??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));
const fmt=v=>typeof v==='string'?v:JSON.stringify(v);
const labels=report.labels_zh||{{}};const phaseLabel=v=>(labels.phases||{{}})[v]||v;const familyLabel=v=>(labels.families||{{}})[v]||v;const statusLabel=v=>(labels.statuses||{{}})[v]||v;
city.textContent=report.city||'—';leafCount.textContent=report.coverage.canonical_measurement_leaf_count||report.coverage.measurement_leaf_count;
impactCount.textContent=report.impact_chains.length;status.textContent=report.status_label_zh||statusLabel(report.status);
subtitle.textContent=`${{report.generated_at}} · 报告版本 ${{report.schema_version}} · ${{report.purpose_zh||'报告只读，不参与几何生成'}}`;
families.innerHTML=Object.entries(report.coverage.family_statuses||{{}}).map(([name,value])=>{{const parts=name.split('.');return `<span class="badge">${{esc(phaseLabel(parts[0]))}} · ${{esc(familyLabel(parts.slice(1).join('.')))}} · ${{esc(statusLabel(value))}}</span>`}}).join(' ');
realizations.innerHTML=(report.realization_matrix||[]).map(x=>`<tr><td><b>${{esc(x.title)}}</b><br><code>${{esc(x.effect_id)}}</code></td><td><span class="badge">${{esc(statusLabel(x.realization_status))}}</span></td><td><code>${{esc(x.consumer_symbol)}}</code><br><span class="muted">${{esc(x.consumer_stage)}}</span></td><td>${{x.applied_to_current_run?'是':'否'}}</td><td><code>${{esc((x.actual_outcome_paths||[]).join(' · '))}}</code>${{x.mismatch_reason?`<p class="muted">${{esc(x.mismatch_reason)}}</p>`:''}}</td></tr>`).join('');
impacts.innerHTML=report.impact_chains.map(x=>`<section class="panel impact"><b>${{esc(x.title)}}</b> <span class="badge">${{esc(statusLabel(x.realized_status))}}</span><p>${{esc(x.effect)}}</p><p class="muted">实际执行模块：${{x.consumers.map(esc).join(' · ')}}</p><p>策略输出：${{x.decision_outputs.map(y=>`<span class="badge">${{esc(y)}}</span>`).join('')}}</p><p class="muted">覆盖 ${{x.measurement_leaf_count}} 项；禁止控制：${{x.forbidden_controls.map(esc).join('、')}}</p></section>`).join('');
const searchIndex=Object.values(report.indexes||{{}}).flat();
const tbody=document.getElementById('rows');function render(q=''){{q=q.trim().toLowerCase();const items=searchIndex.filter(x=>!q||`${{phaseLabel(x.phase)}} ${{familyLabel(x.family)}} ${{x.path}} ${{fmt(x.value)}} ${{x.impact_title}}`.toLowerCase().includes(q));tbody.innerHTML=items.map(x=>`<tr><td><span class="badge">${{esc(phaseLabel(x.phase))}} · ${{esc(familyLabel(x.family))}}</span><code>${{esc(x.path)}}</code></td><td><code>${{esc(fmt(x.value))}}</code></td><td>${{esc(x.impact_title)}}</td></tr>`).join('')}}
search.addEventListener('input',e=>render(e.target.value));render();
canonical.textContent=JSON.stringify(report.measurements,null,2);raw.textContent=JSON.stringify(report.raw_measurements,null,2);decisions.textContent=JSON.stringify(report.resolved_decisions,null,2);outcomes.textContent=JSON.stringify(report.generation_outcomes,null,2);
</script></body></html>"""


def write_measurement_report_html(
    output_dir, report: Mapping, *, stem: str = "pipeline_measurement_report",
) -> str:
    path = Path(output_dir) / f"{_safe_report_stem(stem)}.html"
    return _atomic_write(path, _html_document(report))


def write_measurement_report(
    output_dir, report: Mapping, *, stem: str = "pipeline_measurement_report",
) -> dict:
    return {
        "json": write_measurement_report_json(output_dir, report, stem=stem),
        "html": write_measurement_report_html(output_dir, report, stem=stem),
    }
