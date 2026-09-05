"""Canonical, read-only contract for the map generation pipeline.

The contract is deliberately free of generator imports.  Geometry code, the
durable run ledger and the administrator console can therefore share one stage
registry without creating a second source of truth or allowing observation
code to steer geometry.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from collections.abc import Mapping
import hashlib
import json
import math
from typing import Iterator


CONTRACT_VERSION = "generation-pipeline-contract-v3"
SUPPORTED_MODES = ("full", "draft", "styles", "fetch", "review")
S10_REQUIRED_ARTIFACT_BUNDLE = (
    "3mf",
    "design_spec",
    "measurement_report_json",
    "measurement_report_html",
    "pipeline_observation_json",
    "pipeline_observation_html",
)
S11_REQUIRED_ARTIFACT_BUNDLE = (
    "validator_report",
    "acceptance_report",
)
S11_ACCEPTED_SLICER_STATUSES = ("passed", "accepted")
SEMANTIC_MESH_GATE_VERSION = "semantic-mesh-gate-v1"
SEMANTIC_MESH_SUMMARY_VERSION = "semantic-mesh-summary-v2"
SEMANTIC_MESH_ROLES = (
    "terrain", "landmarks", "buildings", "roads", "water",
    "vegetation", "block_base",
)
FEATURE_SURVIVAL_POLICY_VERSION = "feature-survival-v1"
FEATURE_SURVIVAL_SCOPE = "binary_nonzero_survival"
FEATURE_SURVIVAL_FAMILIES = ("roads", "water", "buildings", "vegetation")
FEATURE_SURVIVAL_STATUSES = (
    "survived", "lost", "not_present_in_source", "intentionally_omitted",
)
CARRIED_CONTEXT_KEYS = {
    "S3": (
        "preprocess_parameters_fingerprint",
        "source_feature_counts_fingerprint",
        "terrain_surface_fingerprint",
    ),
    "S4": (
        "preprocess_parameters_fingerprint",
        "source_feature_counts_fingerprint",
        "terrain_surface_fingerprint",
    ),
    "S5": (
        "preprocess_parameters_fingerprint",
        "source_feature_counts_fingerprint",
        "terrain_surface_fingerprint",
        "scene_character_fingerprint",
    ),
    "S6": (
        "preprocess_parameters_fingerprint",
        "source_feature_counts_fingerprint",
        "terrain_surface_fingerprint",
        "scene_character_fingerprint",
        "scene_policy_fingerprint",
    ),
    "S7": (
        "preprocess_parameters_fingerprint",
        "source_feature_counts_fingerprint",
        "terrain_surface_fingerprint",
        "scene_character_fingerprint",
        "scene_policy_fingerprint",
    ),
    "S8": (
        "preprocess_parameters_fingerprint",
        "source_feature_counts_fingerprint",
        "terrain_surface_fingerprint",
        "scene_character_fingerprint",
        "scene_policy_fingerprint",
    ),
    "S9": (
        "preprocess_parameters_fingerprint",
        "source_feature_counts_fingerprint",
        "terrain_surface_fingerprint",
        "scene_character_fingerprint",
        "scene_policy_fingerprint",
    ),
    "S10": (
        "preprocess_parameters_fingerprint",
        "source_feature_counts_fingerprint",
        "terrain_surface_fingerprint",
        "scene_character_fingerprint",
        "scene_policy_fingerprint",
    ),
}


@dataclass(frozen=True)
class StageSpec:
    """One immutable stage boundary in the generation pipeline."""

    id: str
    order: int
    name: str
    progress_threshold: int
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    context_in: str
    context_out: str
    applicable_modes: tuple[str, ...]
    summary: str
    required_context_keys: tuple[str, ...] = ()

    def applies_to(self, mode: str) -> bool:
        _validate_mode(mode)
        return mode in self.applicable_modes


_ALL = SUPPORTED_MODES
_GENERATING = ("full", "draft", "styles", "review")


PIPELINE_STAGES = (
    StageSpec(
        "S0", 0, "解析运行配置", 0,
        ("任务请求", "打印机配置", "数据源注册表"),
        ("ResolvedRunSpec", "SourceRegistry"),
        "RunRequest", "PipelineContextV0", _ALL,
        "固定 bbox、模型比例、运行模式、打印硬约束及只读数据源身份。",
        required_context_keys=(
            "city", "mode", "bbox_wgs84", "scale_mm_per_m",
            "printer_profile_id"),
    ),
    StageSpec(
        "S1", 1, "获取原始数据", 7,
        ("ResolvedRunSpec", "SourceRegistry"),
        ("建筑 / 道路 / 水体 / 地表原始要素", "DEM"),
        "PipelineContextV0", "PipelineContextV1", _ALL,
        "通过已注册数据源获取原始要素，并保留来源与缓存证据。",
        required_context_keys=(
            "raw_feature_counts", "dem_evidence", "osmium_backend"),
    ),
    StageSpec(
        "S2", 2, "投影、源测量与输入检查", 15,
        ("原始地理要素", "DEM", "模型比例"),
        ("ProjectedData", "SourceEvidence", "非零要素检查",
         "TerrainSurfacePlan", "PreprocessParameters"),
        "PipelineContextV1", "PipelineContextV2", _GENERATING,
        "裁切、投影、修复与去重，验证投影来源和非零要素；基于原始来源、"
        "比例与打印档案解析一次尺度级预处理参数和不可变地形表面计划。"
        "这些参数只供紧邻的 S3 使用，本阶段不生成网格。",
        required_context_keys=(
            "projected_feature_counts", "bbox_local_m",
            "terrain_surface_plan", "preprocess_parameters",
            "preprocess_parameters_fingerprint",
            "source_feature_counts_fingerprint",
            "terrain_surface_fingerprint"),
    ),
    StageSpec(
        "S3", 3, "预处理与拓扑构图", 20,
        ("ProjectedData", "SourceEvidence", "PreprocessParameters"),
        ("BaseLayers", "Topology", "CompositionEvidence"),
        "PipelineContextV2", "PipelineContextV3", _GENERATING,
        "消费 S2 已解析的尺度参数，并以其 fingerprint 绑定缓存与摘要交接，"
        "建立道路和水体拓扑、角色化图层与 Block Base 基线；当前 legacy 执行"
        "仍使用少量同次运行局部变量，不得从之后的观测报告反向改写本 Stage。",
        required_context_keys=(
            "layer_counts", "preprocess_policy_version",
            "preprocess_parameters_fingerprint",
            "source_feature_counts_fingerprint",
            "terrain_surface_fingerprint"),
    ),
    StageSpec(
        "S4", 4, "城市与地貌观测", 35,
        ("BaseLayers", "TerrainSurfacePlan", "模型比例", "SourceEvidence"),
        ("SceneCharacter", "BlockGrammar", "数据质量测量"),
        "PipelineContextV3", "PipelineContextV4", _GENERATING,
        "只读测量城市结构、地貌、数据质量与可打印尺度特征。",
        required_context_keys=(
            "scene_character_version", "scene_status", "source_quality",
            "preprocess_parameters_fingerprint",
            "source_feature_counts_fingerprint",
            "terrain_surface_fingerprint", "scene_character_fingerprint"),
    ),
    StageSpec(
        "S5", 5, "下游生成策略解析", 40,
        ("SceneCharacter", "打印硬约束", "版本化视觉语法"),
        ("ScenePolicy", "角色优先级", "有界生成策略"),
        "PipelineContextV4", "PipelineContextV5", _GENERATING,
        "把 S4 测量解析为供 S6–S8 使用的有界策略；不反向改写 S3 已完成的"
        "道路/水体拓扑，也不直接控制网格、全局 Z 或布尔运算。",
        required_context_keys=(
            "scene_policy_version", "activation", "scene_class",
            "archetype", "scene_character_fingerprint",
            "scene_policy_fingerprint", "preprocess_parameters_fingerprint",
            "source_feature_counts_fingerprint",
            "terrain_surface_fingerprint"),
    ),
    StageSpec(
        "S6", 6, "建筑中频与高度层级", 62,
        ("ScenePolicy", "来源 footprint", "Topology", "打印硬边界",
         "TerrainSurfacePlan"),
        ("FinalLayerPolygons", "建筑中频证据", "高度角色"),
        "PipelineContextV5", "PipelineContextV6", _GENERATING,
        "在道路和水体硬边界内完成建筑聚合、轮廓规整与高度意图。",
        required_context_keys=(
            "final_layer_counts", "building_mass_status",
            "height_hierarchy_status", "terrain_surface_fingerprint",
            "preprocess_parameters_fingerprint",
            "source_feature_counts_fingerprint",
            "scene_character_fingerprint", "scene_policy_fingerprint"),
    ),
    StageSpec(
        "S7", 7, "诊断证据与构图", 70,
        ("FinalLayerPolygons", "SceneCharacter", "ScenePolicy",
         "TerrainSurfacePlan"),
        ("ReviewArtifacts", "CompositionSpec", "测量报告"),
        "PipelineContextV6", "PipelineContextV7", _GENERATING,
        "由最终图层生成 PNG、可选 Draft GLB 和只读诊断证据。",
        required_context_keys=(
            "review_artifacts", "terrain_surface_fingerprint",
            "preprocess_parameters_fingerprint",
            "source_feature_counts_fingerprint",
            "scene_character_fingerprint", "scene_policy_fingerprint"),
    ),
    StageSpec(
        "S8", 8, "生成语义网格", 78,
        ("FinalLayerPolygons", "TerrainSurfacePlan", "语义高度"),
        ("SemanticMeshBundle", "FeatureCounts"),
        "PipelineContextV7", "PipelineContextV8", ("full",),
        "从 S2 的同一不可变表面计划物化地形，并为建筑、道路、水体、植被"
        "和 Block Base 生成语义网格；不得重新解析另一套全局 Z。",
        required_context_keys=(
            "terrain_surface_fingerprint", "mesh_summary",
            "water_relief_status", "preprocess_parameters_fingerprint",
            "source_feature_counts_fingerprint",
            "scene_character_fingerprint", "scene_policy_fingerprint"),
    ),
    StageSpec(
        "S9", 9, "间隙与几何检查", 91,
        ("SemanticMeshBundle", "打印间隙配置"),
        ("VerifiedSemanticMeshBundle", "GeometryEvidence"),
        "PipelineContextV8", "PipelineContextV9", ("full",),
        "只读检查必需语义网格的有限数、三角面、水密、绕向、边界，"
        "验证 Block Base 道路间隙，并阻止来源语义族静默归零；"
        "本阶段不执行隐式网格修复。",
        required_context_keys=(
            "gate_version", "passed", "required_roles", "errors",
            "warnings", "mesh_metrics", "feature_survival",
            "preprocess_parameters_fingerprint",
            "source_feature_counts_fingerprint",
            "terrain_surface_fingerprint", "scene_character_fingerprint",
            "scene_policy_fingerprint"),
    ),
    StageSpec(
        "S10", 10, "导出交付产物", 95,
        ("VerifiedSemanticMeshBundle", "DesignSpec 证据"),
        ("ArtifactBundle", "artifact 哈希"),
        "PipelineContextV9", "PipelineContextV10", ("full",),
        "导出多对象 3MF、design_spec.json、报告和可审计产物哈希。",
        required_context_keys=(
            "status", "artifact_bundle", "s9_errors", "s9_warnings",
            "terrain_surface_fingerprint", "preprocess_parameters_fingerprint",
            "source_feature_counts_fingerprint",
            "scene_character_fingerprint", "scene_policy_fingerprint"),
    ),
    StageSpec(
        "S11", 11, "正式验收", 98,
        ("ArtifactBundle", "项目验证器", "受信切片证据声明"),
        ("AcceptanceReport",),
        "PipelineContextV10", "AcceptanceReport", ("full",),
        "系统重跑项目验证器，并校验与 3MF hash 绑定的受信切片证据声明；"
        "声明本身不能密码学证明切片器确实运行。",
        required_context_keys=(
            "schema_version", "accepted", "errors", "warnings",
            "artifact_sha256", "validator_strict_passed", "slicer_status"),
    ),
)


_STAGES_BY_ID = {stage.id: stage for stage in PIPELINE_STAGES}


def _validate_mode(mode: str) -> None:
    if mode not in SUPPORTED_MODES:
        expected = ", ".join(SUPPORTED_MODES)
        raise ValueError(f"unsupported pipeline mode {mode!r}; expected {expected}")


def stage_by_id(stage_id: str) -> StageSpec:
    try:
        return _STAGES_BY_ID[stage_id]
    except KeyError as exc:
        raise KeyError(f"unknown pipeline stage {stage_id!r}") from exc


def iter_stages(mode: str | None = None) -> Iterator[StageSpec]:
    if mode is None:
        yield from PIPELINE_STAGES
        return
    _validate_mode(mode)
    yield from (stage for stage in PIPELINE_STAGES if mode in stage.applicable_modes)


def stages_for_mode(mode: str) -> tuple[StageSpec, ...]:
    return tuple(iter_stages(mode))


def is_stage_applicable(stage_id: str, mode: str) -> bool:
    return stage_by_id(stage_id).applies_to(mode)


def terminal_stage(mode: str) -> StageSpec:
    stages = stages_for_mode(mode)
    if not stages:  # guarded by the contract validation below
        raise ValueError(f"pipeline mode {mode!r} has no applicable stages")
    return stages[-1]


def contract_as_dict() -> dict:
    """Return a JSON-safe snapshot for reports and administrator UIs."""

    return {
        "contract_version": CONTRACT_VERSION,
        "supported_modes": list(SUPPORTED_MODES),
        "terminal_stages": {
            mode: terminal_stage(mode).id for mode in SUPPORTED_MODES
        },
        "stages": [
            {
                **asdict(stage),
                "inputs": list(stage.inputs),
                "outputs": list(stage.outputs),
                "applicable_modes": list(stage.applicable_modes),
                "required_context_keys": list(stage.required_context_keys),
            }
            for stage in PIPELINE_STAGES
        ],
    }


def _issue_list(stage_id: str, key: str, value) -> list:
    issues = value.get(key)
    if not isinstance(issues, (list, tuple)):
        raise ValueError(f"{stage_id} {key} must be a list")
    return list(issues)


def _zero_issue_count(stage_id: str, key: str, value) -> None:
    count = value.get(key)
    if isinstance(count, bool) or not isinstance(count, int) or count != 0:
        raise ValueError(f"{stage_id} {key} must be integer zero")


def _nonempty_string(stage_id: str, key: str, value: Mapping) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{stage_id} {key} must be a non-empty string")
    return item


def _sha256_string(stage_id: str, key: str, value: Mapping) -> str:
    item = _nonempty_string(stage_id, key, value)
    if len(item) != 64 or any(char not in "0123456789abcdef" for char in item):
        raise ValueError(f"{stage_id} {key} must be a lowercase SHA-256")
    return item


def _finite_bbox(stage_id: str, key: str, value: Mapping) -> list[float]:
    bbox = value.get(key)
    if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
            or any(isinstance(item, bool)
                   or not isinstance(item, (int, float))
                   or not math.isfinite(float(item)) for item in bbox)):
        raise ValueError(f"{stage_id} {key} must contain four finite numbers")
    normalized = [float(item) for item in bbox]
    if normalized[0] >= normalized[2] or normalized[1] >= normalized[3]:
        raise ValueError(f"{stage_id} {key} bounds are inverted or empty")
    return normalized


def _nonnegative_counts(
    stage_id: str,
    key: str,
    value: Mapping,
    *,
    required: tuple[str, ...] = (),
    require_any: bool = False,
) -> dict[str, int]:
    counts = value.get(key)
    if not isinstance(counts, Mapping):
        raise ValueError(f"{stage_id} {key} must be a mapping")
    missing = set(required) - set(counts)
    if missing:
        raise ValueError(
            f"{stage_id} {key} is missing: {', '.join(sorted(missing))}")
    normalized = {}
    for name, count in counts.items():
        if (not isinstance(name, str) or not name.strip()
                or isinstance(count, bool) or not isinstance(count, int)
                or count < 0):
            raise ValueError(
                f"{stage_id} {key} must contain non-negative integer counts")
        normalized[name] = count
    if require_any and not any(normalized.values()):
        raise ValueError(f"{stage_id} {key} cannot be entirely zero")
    return normalized


def _validate_feature_survival(value: Mapping) -> None:
    """Validate the complete, internally consistent binary-survival proof."""

    if value.get("policy_version") != FEATURE_SURVIVAL_POLICY_VERSION:
        raise ValueError("S9 feature_survival policy_version is invalid")
    if value.get("scope") != FEATURE_SURVIVAL_SCOPE:
        raise ValueError("S9 feature_survival scope is invalid")
    errors = _issue_list("S9 feature_survival", "errors", value)
    roles = value.get("roles")
    if not isinstance(roles, Mapping) or set(roles) != set(
            FEATURE_SURVIVAL_FAMILIES):
        raise ValueError(
            "S9 feature_survival must describe all semantic families")
    omissions = value.get("intentional_omissions")
    if (not isinstance(omissions, (list, tuple))
            or any(str(item) != "vegetation" for item in omissions)):
        raise ValueError(
            "S9 feature_survival only permits an explicit vegetation omission")
    omitted = {str(item) for item in omissions}
    derived_errors = []
    for family in FEATURE_SURVIVAL_FAMILIES:
        evidence = roles.get(family)
        if not isinstance(evidence, Mapping):
            raise ValueError(
                f"S9 feature_survival {family} evidence is invalid")
        source_count = evidence.get("source_count")
        final_count = evidence.get("final_count")
        expected = evidence.get("expected")
        status = evidence.get("status")
        if (isinstance(source_count, bool) or not isinstance(source_count, int)
                or source_count < 0 or isinstance(final_count, bool)
                or not isinstance(final_count, int) or final_count < 0
                or not isinstance(expected, bool)
                or status not in FEATURE_SURVIVAL_STATUSES):
            raise ValueError(
                f"S9 feature_survival {family} evidence has invalid fields")
        derived_expected = source_count > 0 and family not in omitted
        if expected is not derived_expected:
            raise ValueError(
                f"S9 feature_survival {family} expected flag is inconsistent")
        if family in omitted:
            derived_status = "intentionally_omitted"
        elif not derived_expected:
            derived_status = "not_present_in_source"
        elif final_count > 0:
            derived_status = "survived"
        else:
            derived_status = "lost"
            derived_errors.append(
                f"{family}: {source_count} projected source features "
                "collapsed to zero final semantic layers")
        if status != derived_status:
            raise ValueError(
                f"S9 feature_survival {family} status is inconsistent")
    source_counts = {
        family: int(roles[family]["source_count"])
        for family in FEATURE_SURVIVAL_FAMILIES
    }
    if value.get("source_counts_fingerprint") != (
            feature_source_counts_fingerprint(source_counts)):
        raise ValueError(
            "S9 feature_survival source fingerprint is inconsistent")
    if errors != derived_errors:
        raise ValueError("S9 feature_survival errors are inconsistent")
    if value.get("passed") is not (not derived_errors):
        raise ValueError("S9 feature_survival passed flag is inconsistent")


def _validate_mesh_gate(value: Mapping) -> None:
    """Validate the complete successful in-memory mesh proof for S9."""

    if value.get("gate_version") != SEMANTIC_MESH_GATE_VERSION:
        raise ValueError("S9 gate_version is invalid")
    required_roles = value.get("required_roles")
    if not isinstance(required_roles, (list, tuple)) or not required_roles:
        raise ValueError("S9 required_roles must be a non-empty list")
    if (any(not isinstance(role, str) or not role.strip()
            for role in required_roles)
            or len(set(required_roles)) != len(required_roles)):
        raise ValueError(
            "S9 required_roles must contain unique non-empty role names")
    unknown_roles = set(required_roles) - set(SEMANTIC_MESH_ROLES)
    if unknown_roles:
        raise ValueError(
            "S9 required_roles contains unknown roles: "
            + ", ".join(sorted(unknown_roles)))
    if "terrain" not in required_roles:
        raise ValueError("S9 required_roles must include terrain")

    metrics = value.get("mesh_metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("S9 mesh_metrics must be a mapping")
    if set(metrics) != set(required_roles):
        raise ValueError(
            "S9 mesh_metrics roles must exactly match required_roles")

    for role in required_roles:
        metric = metrics.get(role)
        if not isinstance(metric, Mapping):
            raise ValueError(f"S9 mesh_metrics {role} must be a mapping")
        vertices = metric.get("vertices")
        faces = metric.get("faces")
        if metric.get("present") is not True:
            raise ValueError(f"S9 mesh_metrics {role} is not present")
        if (isinstance(vertices, bool) or not isinstance(vertices, int)
                or vertices <= 0):
            raise ValueError(
                f"S9 mesh_metrics {role} vertices must be a positive int")
        if (isinstance(faces, bool) or not isinstance(faces, int)
                or faces <= 0):
            raise ValueError(
                f"S9 mesh_metrics {role} faces must be a positive int")
        if metric.get("watertight") is not True:
            raise ValueError(f"S9 mesh_metrics {role} is not watertight")
        if metric.get("winding_consistent") is not True:
            raise ValueError(
                f"S9 mesh_metrics {role} winding is inconsistent")
        bounds = metric.get("bounds_mm")
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2
                or any(not isinstance(point, (list, tuple))
                       or len(point) != 3 for point in bounds)):
            raise ValueError(
                f"S9 mesh_metrics {role} bounds_mm must be two XYZ points")
        normalized_bounds = []
        for point in bounds:
            normalized_point = []
            for coordinate in point:
                if (isinstance(coordinate, bool)
                        or not isinstance(coordinate, (int, float))
                        or not math.isfinite(float(coordinate))):
                    raise ValueError(
                        f"S9 mesh_metrics {role} bounds_mm must be finite")
                normalized_point.append(float(coordinate))
            normalized_bounds.append(normalized_point)
        if any(minimum > maximum for minimum, maximum in zip(
                normalized_bounds[0], normalized_bounds[1])):
            raise ValueError(
                f"S9 mesh_metrics {role} bounds_mm are inverted")


def _validate_mesh_bounds(stage_id: str, role: str, bounds) -> None:
    if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2
            or any(not isinstance(point, (list, tuple))
                   or len(point) != 3 for point in bounds)):
        raise ValueError(
            f"{stage_id} mesh_summary {role} bounds_mm must be two XYZ points")
    normalized = []
    for point in bounds:
        normalized_point = []
        for coordinate in point:
            if (isinstance(coordinate, bool)
                    or not isinstance(coordinate, (int, float))
                    or not math.isfinite(float(coordinate))):
                raise ValueError(
                    f"{stage_id} mesh_summary {role} bounds_mm must be finite")
            normalized_point.append(float(coordinate))
        normalized.append(normalized_point)
    if any(minimum > maximum for minimum, maximum in zip(
            normalized[0], normalized[1])):
        raise ValueError(
            f"{stage_id} mesh_summary {role} bounds_mm are inverted")


def _validate_s8_mesh_summary(value: Mapping) -> None:
    """Validate generated-mesh evidence without turning S8 into S9.

    S8 may record a non-watertight mesh so that S9 can reject it, but every
    ready role must carry the same measurable geometry fields that S9 will
    later attest.  ``winding_consistent`` is required for newly written v2
    summaries and remains optional only for already persisted v3-contract
    ledgers written before that evidence field existed.
    """

    summary = value.get("mesh_summary")
    if not isinstance(summary, Mapping):
        raise ValueError("S8 mesh_summary must be a mapping")
    unknown = set(summary) - set(SEMANTIC_MESH_ROLES)
    if unknown:
        raise ValueError(
            "S8 mesh_summary contains unknown roles: "
            + ", ".join(sorted(unknown)))
    version = value.get("mesh_summary_version")
    if version is not None and version != SEMANTIC_MESH_SUMMARY_VERSION:
        raise ValueError("S8 mesh_summary_version is invalid")

    for role, evidence in summary.items():
        if not isinstance(evidence, Mapping):
            raise ValueError(f"S8 mesh_summary {role} must be a mapping")
        status = evidence.get("status")
        vertices = evidence.get("vertices")
        faces = evidence.get("faces")
        if status not in {"ready", "absent"}:
            raise ValueError(f"S8 mesh_summary {role} status is invalid")
        if (isinstance(vertices, bool) or not isinstance(vertices, int)
                or isinstance(faces, bool) or not isinstance(faces, int)):
            raise ValueError(
                f"S8 mesh_summary {role} counts must be integers")
        if status == "absent":
            if vertices != 0 or faces != 0:
                raise ValueError(
                    f"S8 mesh_summary {role} absent counts must be zero")
            continue
        if vertices <= 0 or faces <= 0:
            raise ValueError(
                f"S8 mesh_summary {role} ready mesh must be non-empty")
        if not isinstance(evidence.get("watertight"), bool):
            raise ValueError(
                f"S8 mesh_summary {role} watertight must be boolean")
        winding = evidence.get("winding_consistent")
        if (version == SEMANTIC_MESH_SUMMARY_VERSION
                and not isinstance(winding, bool)):
            raise ValueError(
                f"S8 mesh_summary {role} winding_consistent must be boolean")
        if winding is not None and not isinstance(winding, bool):
            raise ValueError(
                f"S8 mesh_summary {role} winding_consistent must be boolean")
        _validate_mesh_bounds("S8", role, evidence.get("bounds_mm"))

    terrain = summary.get("terrain")
    if not isinstance(terrain, Mapping) or terrain.get("status") != "ready":
        raise ValueError(
            "S8 mesh_summary must contain a non-empty terrain mesh")


def _json_fingerprint(value) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_feature_source_counts(value: Mapping) -> dict[str, int]:
    """Return the four canonical source-family counts or reject bad input."""

    if not isinstance(value, Mapping):
        raise ValueError("source feature counts must be a mapping")
    normalized = {}
    for family in FEATURE_SURVIVAL_FAMILIES:
        count = value.get(family, 0)
        if (isinstance(count, bool) or not isinstance(count, int)
                or count < 0):
            raise ValueError(
                f"source feature count for {family} must be a non-negative int")
        normalized[family] = count
    return normalized


def feature_source_counts_fingerprint(value: Mapping) -> str:
    """Content identity carried from projected source evidence through S10."""

    return _json_fingerprint(normalize_feature_source_counts(value))


def validate_stage_context(stage_id: str, value, *, outcome="complete") -> None:
    """Reject incomplete or semantically false Stage output.

    ``complete_stage`` always uses the default successful outcome.  S11 has a
    separate explicit rejection transition; it passes ``outcome='reject'`` so
    failed validation evidence can be recorded without ever becoming a
    successful completion.
    """

    spec = stage_by_id(stage_id)
    if not isinstance(value, Mapping):
        raise ValueError(f"{stage_id} output Context must be a mapping")
    missing = [key for key in spec.required_context_keys if key not in value]
    if missing:
        raise ValueError(
            f"{stage_id} output Context is missing required keys: "
            f"{', '.join(missing)}")
    if outcome not in {"complete", "reject"}:
        raise ValueError(f"unsupported stage outcome {outcome!r}")
    if outcome == "reject" and stage_id != "S11":
        raise ValueError("only S11 has an explicit rejected outcome")

    if stage_id == "S0":
        _nonempty_string("S0", "city", value)
        if value.get("mode") not in SUPPORTED_MODES:
            raise ValueError("S0 mode is invalid")
        _finite_bbox("S0", "bbox_wgs84", value)
        scale = value.get("scale_mm_per_m")
        if (isinstance(scale, bool) or not isinstance(scale, (int, float))
                or not math.isfinite(float(scale)) or float(scale) <= 0):
            raise ValueError("S0 scale_mm_per_m must be finite and positive")
        _nonempty_string("S0", "printer_profile_id", value)

    if stage_id == "S1":
        _nonnegative_counts(
            "S1", "raw_feature_counts", value,
            required=FEATURE_SURVIVAL_FAMILIES, require_any=True)
        dem = value.get("dem_evidence")
        if not isinstance(dem, Mapping):
            raise ValueError("S1 dem_evidence must be a mapping")
        dem_status = dem.get("status")
        flat = dem.get("flat_fallback")
        if (dem_status not in {"ready", "flat_fallback"}
                or not isinstance(flat, bool)
                or bool(dem_status == "flat_fallback") != flat):
            raise ValueError("S1 dem_evidence status is inconsistent")
        _nonempty_string("S1 dem_evidence", "source", dem)
        _nonempty_string("S1", "osmium_backend", value)

    if stage_id == "S2":
        parameters = value.get("preprocess_parameters")
        if not isinstance(parameters, Mapping):
            raise ValueError("S2 preprocess_parameters must be a mapping")
        if value.get("preprocess_parameters_fingerprint") != (
                _json_fingerprint(parameters)):
            raise ValueError(
                "S2 preprocess_parameters fingerprint does not match payload")
        projected_counts = _nonnegative_counts(
            "S2", "projected_feature_counts", value,
            required=FEATURE_SURVIVAL_FAMILIES, require_any=True)
        if value.get("source_feature_counts_fingerprint") != (
                feature_source_counts_fingerprint(projected_counts)):
            raise ValueError(
                "S2 source feature counts fingerprint does not match payload")
        _finite_bbox("S2", "bbox_local_m", value)
        terrain_plan = value.get("terrain_surface_plan")
        if not isinstance(terrain_plan, Mapping):
            raise ValueError("S2 terrain_surface_plan must be a mapping")
        terrain_fingerprint = terrain_plan.get("fingerprint")
        if (not isinstance(terrain_fingerprint, str)
                or len(terrain_fingerprint) != 64
                or any(char not in "0123456789abcdef"
                       for char in terrain_fingerprint)
                or value.get("terrain_surface_fingerprint") !=
                terrain_fingerprint):
            raise ValueError(
                "S2 terrain surface fingerprint does not match its plan")

    if stage_id == "S3":
        _nonnegative_counts(
            "S3", "layer_counts", value,
            required=("BL", "BO", "WL", "WO", "VL", "VO", "roads",
                      "block_base"))
        _nonempty_string("S3", "preprocess_policy_version", value)

    if stage_id == "S4":
        _nonempty_string("S4", "scene_character_version", value)
        _nonempty_string("S4", "scene_status", value)
        if not isinstance(value.get("source_quality"), Mapping):
            raise ValueError("S4 source_quality must be a mapping")

    if stage_id == "S5":
        _nonempty_string("S5", "scene_policy_version", value)
        if value.get("activation") not in {"active", "audit_only"}:
            raise ValueError("S5 activation is invalid")
        _nonempty_string("S5", "scene_class", value)
        _nonempty_string("S5", "archetype", value)

    if stage_id == "S6":
        _nonnegative_counts(
            "S6", "final_layer_counts", value,
            required=("BL", "BO", "WL", "WO", "VL", "VO", "roads",
                      "block_base"))
        _nonempty_string("S6", "building_mass_status", value)
        _nonempty_string("S6", "height_hierarchy_status", value)

    if stage_id == "S7":
        artifacts = value.get("review_artifacts")
        if (not isinstance(artifacts, (list, tuple))
                or any(not isinstance(item, str) or not item.strip()
                       for item in artifacts)
                or len(set(artifacts)) != len(artifacts)):
            raise ValueError(
                "S7 review_artifacts must be a unique list of names")

    if stage_id == "S8":
        _validate_s8_mesh_summary(value)
        if value.get("water_relief_status") not in {
                "materialized", "not_applicable"}:
            raise ValueError("S8 water_relief_status is invalid")

    carried_fingerprints = (
        "preprocess_parameters_fingerprint",
        "source_feature_counts_fingerprint",
        "terrain_surface_fingerprint",
        "scene_character_fingerprint",
        "scene_policy_fingerprint",
    )
    for key in carried_fingerprints:
        if key in value:
            _sha256_string(stage_id, key, value)

    if stage_id == "S9":
        _validate_mesh_gate(value)
        if value.get("passed") is not True:
            raise ValueError("S9 cannot complete unless passed is true")
        if _issue_list("S9", "errors", value):
            raise ValueError("S9 cannot complete with geometry errors")
        if _issue_list("S9", "warnings", value):
            raise ValueError("S9 cannot complete with geometry warnings")
        survival = value.get("feature_survival")
        if not isinstance(survival, Mapping):
            raise ValueError("S9 feature_survival must be a mapping")
        _validate_feature_survival(survival)
        if survival.get("source_counts_fingerprint") != value.get(
                "source_feature_counts_fingerprint"):
            raise ValueError(
                "S9 feature survival is not bound to the S2 source counts")
        if survival.get("passed") is not True:
            raise ValueError(
                "S9 cannot complete unless source features survived")
        if _issue_list("S9 feature_survival", "errors", survival):
            raise ValueError("S9 cannot complete with feature survival errors")

    if stage_id == "S10":
        if value.get("status") != "generated_pending_validation":
            raise ValueError(
                "S10 status must be generated_pending_validation")
        bundle = value.get("artifact_bundle")
        if not isinstance(bundle, (list, tuple)):
            raise ValueError("S10 artifact_bundle must be a collection")
        if (len(bundle) != len(S10_REQUIRED_ARTIFACT_BUNDLE)
                or set(bundle) != set(S10_REQUIRED_ARTIFACT_BUNDLE)):
            raise ValueError(
                "S10 artifact_bundle must exactly match required artifacts")
        _zero_issue_count("S10", "s9_errors", value)
        _zero_issue_count("S10", "s9_warnings", value)

    if stage_id == "S11":
        _sha256_string("S11", "artifact_sha256", value)
        errors = _issue_list("S11", "errors", value)
        warnings = _issue_list("S11", "warnings", value)
        if outcome == "reject":
            if value.get("accepted") is not False:
                raise ValueError("rejected S11 evidence must set accepted=false")
            if not errors:
                raise ValueError("rejected S11 evidence must explain the failure")
            return
        if value.get("accepted") is not True:
            raise ValueError("S11 cannot complete unless accepted is true")
        if value.get("validator_strict_passed") is not True:
            raise ValueError(
                "S11 cannot complete unless the strict validator passed")
        if value.get("slicer_status") not in S11_ACCEPTED_SLICER_STATUSES:
            raise ValueError("S11 cannot complete unless the slicer accepted")
        if errors:
            raise ValueError("S11 cannot complete with acceptance errors")
        if warnings:
            raise ValueError("S11 cannot complete with acceptance warnings")


def _validate_contract() -> None:
    expected_ids = tuple(f"S{index}" for index in range(12))
    actual_ids = tuple(stage.id for stage in PIPELINE_STAGES)
    if actual_ids != expected_ids:
        raise RuntimeError(
            f"pipeline stages must be S0..S11 in order, got {actual_ids!r}")
    if tuple(stage.order for stage in PIPELINE_STAGES) != tuple(range(12)):
        raise RuntimeError("pipeline stage order fields must be contiguous")
    for previous, current in zip(PIPELINE_STAGES, PIPELINE_STAGES[1:]):
        if previous.context_out != current.context_in:
            raise RuntimeError(
                f"context chain broken between {previous.id} and {current.id}: "
                f"{previous.context_out!r} != {current.context_in!r}")
    known = set(SUPPORTED_MODES)
    for stage in PIPELINE_STAGES:
        unknown = set(stage.applicable_modes) - known
        if unknown:
            raise RuntimeError(
                f"{stage.id} declares unsupported modes: {sorted(unknown)!r}")
    # Every mode is a prefix of the canonical chain.  This preserves the
    # single-direction Context contract even for early-return modes.
    for mode in SUPPORTED_MODES:
        orders = [stage.order for stage in PIPELINE_STAGES
                  if mode in stage.applicable_modes]
        if orders != list(range(len(orders))):
            raise RuntimeError(
                f"mode {mode!r} must use a contiguous pipeline prefix")


_validate_contract()
