"""Build a compact, auditable observation report for one generation run.

The report is a post-run projection of evidence already produced by the
pipeline.  It does not participate in geometry decisions and cannot mutate
layers, meshes, Z values or boolean operations.
"""

from __future__ import annotations

from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
from typing import Any, Mapping

from aesthetic.pipeline_contract import (
    CONTRACT_VERSION,
    PIPELINE_STAGES,
    S10_REQUIRED_ARTIFACT_BUNDLE,
)


SCHEMA_VERSION = "pipeline-observation-v1"
HTML_VERSION = "pipeline-observation-html-v1"

_ACCEPTED_STATUSES = {"accepted", "passed", "success", "validated"}
_REJECTED_STATUSES = {"error", "failed", "rejected"}
_EMBEDDED_POSIX_PATH = re.compile(
    r"/(?:Users|home|root|var|tmp|private|opt|srv|mnt|data)/"
    r"[^\s,;:()<>\"']+"
)
_EMBEDDED_WINDOWS_PATH = re.compile(
    r"[A-Za-z]:\\[^\s,;:()<>\"']+"
)


def _first_present(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _issue_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        return len(value)
    return None


def _acceptance_gate(validation: Mapping | None) -> dict:
    """Resolve formal acceptance without treating summaries as authority."""

    evidence = dict(validation or {})
    validator = evidence.get("validator")
    slicer = evidence.get("slicer")
    formal = evidence.get("schema_version") == "pipeline-acceptance-v1"
    complete_subgates = (
        isinstance(validator, Mapping) and isinstance(slicer, Mapping)
    )
    clean_lists = all(
        isinstance(item, (list, tuple)) and not item
        for item in (
            evidence.get("errors"), evidence.get("warnings"),
            validator.get("errors") if isinstance(validator, Mapping) else None,
            validator.get("warnings") if isinstance(validator, Mapping) else None,
            slicer.get("errors") if isinstance(slicer, Mapping) else None,
            slicer.get("warnings") if isinstance(slicer, Mapping) else None,
        )
    )
    proven_pass = bool(
        formal and complete_subgates and evidence.get("accepted") is True
        and validator.get("strict_passed") is True
        and slicer.get("status") in {"passed", "accepted"}
        and clean_lists
    )
    proven_rejection = bool(
        formal and evidence.get("accepted") is False
        and isinstance(evidence.get("errors"), (list, tuple))
        and evidence.get("errors")
    )
    accepted = True if proven_pass else False if proven_rejection else None

    if accepted is True:
        status = "accepted"
    elif accepted is False:
        status = "rejected"
    else:
        status = "pending_validation"
    return {
        "stage_id": "S11",
        "status": status,
        "accepted": accepted,
        "status_source": {
            "kind": "validation_evidence",
            "reference": "validation",
            "rule": (
                "只有 pipeline-acceptance-v1 报告同时记录项目验证器与切片器"
                "通过证据，或明确记录正式拒绝，才能形成 S11 结论"
            ),
        },
        "evidence": evidence,
    }


def _is_absolute_filesystem_path(value: str) -> bool:
    if value.startswith(("http://", "https://")):
        return False
    if value.startswith("file://"):
        return True
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _safe_string(value: str) -> str:
    """Remove host filesystem layout while retaining portable identities."""

    if _is_absolute_filesystem_path(value):
        if value.startswith("file://"):
            value = value.removeprefix("file://")
        windows = PureWindowsPath(value)
        candidate = (
            windows.name if windows.is_absolute() else Path(value).name)
        return candidate or "<absolute-path>"

    def basename(match: re.Match) -> str:
        path = match.group(0)
        windows = PureWindowsPath(path)
        return (windows.name if windows.is_absolute()
                else Path(path).name) or "<absolute-path>"

    value = _EMBEDDED_WINDOWS_PATH.sub(basename, value)
    return _EMBEDDED_POSIX_PATH.sub(basename, value)


def _safe_report_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            _safe_string(str(key)): _safe_report_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_report_value(item) for item in value]
    if isinstance(value, os.PathLike):
        return _safe_string(os.fspath(value))
    if isinstance(value, str):
        return _safe_string(value)
    return value


def _finite(value: Any, digits: int = 5):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _file_ref(path: str | os.PathLike | None) -> dict | None:
    if not path:
        return None
    raw_path = os.fspath(path)
    candidate = Path(raw_path)
    windows_candidate = PureWindowsPath(raw_path)
    is_absolute = candidate.is_absolute() or windows_candidate.is_absolute()
    filename = (
        windows_candidate.name if windows_candidate.is_absolute()
        else candidate.name
    )
    # Observation reports are uploaded and may be rendered outside the host
    # that produced them.  Never persist an absolute workstation/server path:
    # it is not portable evidence and leaks deployment layout.  Callers that
    # intentionally pass a relative artifact key keep that key for navigation.
    result = {"filename": filename}
    if not is_absolute:
        result["relative_path"] = candidate.as_posix()
    if candidate.is_file():
        result["size_bytes"] = int(candidate.stat().st_size)
    return result


def summarize_meshes(meshes: Mapping[str, Any]) -> dict:
    """Return JSON-safe mesh evidence without retaining mesh objects."""

    result = {}
    for name, mesh in meshes.items():
        if mesh is None:
            result[str(name)] = {
                "status": "absent", "vertices": 0, "faces": 0,
            }
            continue
        bounds = getattr(mesh, "bounds", None)
        vertices = getattr(mesh, "vertices", ())
        faces = getattr(mesh, "faces", ())
        result[str(name)] = {
            "status": "ready",
            "vertices": int(len(vertices)) if vertices is not None else 0,
            "faces": int(len(faces)) if faces is not None else 0,
            "watertight": bool(getattr(mesh, "is_watertight", False)),
            "winding_consistent": bool(
                getattr(mesh, "is_winding_consistent", False)),
            "bounds_mm": (
                [[_finite(value, digits=6) for value in row]
                 for row in bounds]
                if bounds is not None else None),
        }
    return result


def _block_grammar(scene_character: Mapping) -> dict:
    buildings = (((scene_character.get("metrics") or {}).get("buildings"))
                 or {})
    measurement = buildings.get("block_grammar") or {}
    comparable = measurement.get("printable_comparable_sample") or {}
    metrics = comparable.get("metrics") or {}

    def p50(name):
        return _finite(((metrics.get(name) or {}).get("p50")))

    return {
        "status": measurement.get("status", "unavailable"),
        "version": measurement.get("version"),
        "source_footprint_count": measurement.get("source_footprint_count"),
        "comparable_sample_count": comparable.get("sample_count"),
        "estimated_comparable_count": comparable.get(
            "estimated_population_count"),
        "short_axis_p50_mm": p50("short_axis_mm"),
        "solidity_p50": p50("solidity"),
        "rectangularity_p50": p50("rectangularity"),
        "perimeter_excess_p50": p50("perimeter_excess"),
        "orthogonal_coherence": _finite(
            (comparable.get("orientation") or {}).get(
                "orthogonal_coherence")),
        "spatial_fullness": measurement.get("spatial_fullness") or {},
        "reference_envelope": measurement.get("reference_envelope") or {},
    }


def _strategy(scene_policy: Mapping) -> dict:
    buildings = (((scene_policy.get("roles") or {}).get("buildings")) or {})
    block_strategy = buildings.get("block_grammar_strategy") or {}
    return {
        "policy_version": scene_policy.get("policy_version"),
        "activation": scene_policy.get("activation"),
        "scene_class": scene_policy.get("scene_class"),
        "archetype": scene_policy.get("archetype"),
        "dominant_structure": scene_policy.get("dominant_structure") or {},
        "scores": scene_policy.get("scores") or {},
        "building_simplification": buildings.get("simplification_mode"),
        "building_representation": buildings.get(
            "recommended_representation"),
        "block_grammar_strategy": block_strategy,
        "tradeoff_order": scene_policy.get("tradeoff_order") or {},
        "decision_contract": scene_policy.get("decision_contract") or {},
    }


def build_pipeline_observation_report(
    *,
    run: Mapping,
    source_features: Mapping,
    printable_features: Mapping,
    composition_spec: Mapping,
    scene_character: Mapping,
    scene_policy: Mapping,
    building_mass_evidence: Mapping,
    height_hierarchy_evidence: Mapping,
    height_emphasis_evidence: Mapping,
    meshes: Mapping[str, Any],
    block_base_clearance: Mapping | None,
    artifacts: Mapping,
    geometry_gate: Mapping | None = None,
    validation: Mapping | None = None,
    generated_at: str | None = None,
    contract_version: str | None = None,
    run_id: str | None = None,
    attempt_id: str | None = None,
    revision: int | None = None,
    derived_from_revision: str | None = None,
    stage_statuses: Mapping[str, Any] | None = None,
) -> dict:
    """Create the stage-by-stage report consumed by JSON and HTML writers."""

    mesh_summary = summarize_meshes(meshes)
    mesh_ready = any(
        item.get("status") == "ready" for item in mesh_summary.values())
    measurement = _block_grammar(scene_character)
    strategy = _strategy(scene_policy)
    artifact_refs = {
        str(name): _file_ref(path) for name, path in artifacts.items()
        if path
    }
    generated = "3mf" in artifact_refs
    required_bundle = list(S10_REQUIRED_ARTIFACT_BUNDLE)
    bundle_complete = set(required_bundle).issubset(artifact_refs)
    generation_gate = {
        "stage_id": "S10",
        "status": "generated" if generated else "not_generated",
        "generated": generated,
        "bundle_complete": bundle_complete,
        "required_artifacts": required_bundle,
        "present_artifacts": sorted(artifact_refs),
        "status_source": {
            "kind": "artifact_evidence",
            "reference": "artifacts.3mf",
            "rule": "存在 3MF 只能证明导出完成，不能证明正式验收通过",
        },
    }
    acceptance_gate = _acceptance_gate(validation)

    def status_source(reference: str, rule: str) -> dict:
        return {
            "kind": "post_run_projection",
            "reference": reference,
            "rule": rule,
        }

    stage_runtime = [
        {
            "id": "S0", "status": "completed",
            "status_source": status_source(
                "run", "生成后报告包含已经解析的运行配置证据"),
            "evidence": dict(run),
        },
        {
            "id": "S1", "status": "completed",
            "status_source": status_source(
                "source_features", "已经提供原始要素数量与来源证据"),
            "evidence": {"source_feature_counts": dict(source_features)},
        },
        {
            "id": "S2", "status": "completed",
            "status_source": status_source(
                "run.scale_mm_per_m + source_features",
                "依据运行证据投影坐标并执行关键要素非零检查"),
            "evidence": {
                "scale_mm_per_m": run.get("scale_mm_per_m"),
                "model_span_mm": run.get("model_span_mm"),
                "nonzero_feature_check": {
                    key: int(value or 0) > 0
                    for key, value in source_features.items()
                },
            },
        },
        {
            "id": "S3", "status": "completed",
            "status_source": status_source(
                "composition_spec + printable_features",
                "已经提供预处理图层与构图证据"),
            "evidence": {
                "composition": {
                    "schema_version": composition_spec.get("schema_version"),
                    "policy_version": composition_spec.get("policy_version"),
                    "scene": composition_spec.get("scene") or {},
                },
                "printable_feature_counts": dict(printable_features),
            },
        },
        {
            "id": "S4",
            "status": "completed" if scene_character else "unavailable",
            "status_source": status_source(
                "scene_character",
                "存在场景特征证据，证明城市与地貌观测已经完成"),
            "evidence": {
                "version": scene_character.get("version"),
                "summary": scene_character.get("summary") or {},
                "feature_counts": scene_character.get("feature_counts") or {},
                "block_grammar": measurement,
                "building_data_quality": (
                    ((scene_character.get("metrics") or {}).get(
                        "building_data_quality")) or {}),
            },
        },
        {
            "id": "S5",
            "status": "completed" if scene_policy else "unavailable",
            "status_source": status_source(
                "scene_policy",
                "存在场景策略证据，证明下游生成策略已经解析"),
            "evidence": strategy,
        },
        {
            "id": "S6",
            "status": (
                "completed" if any((building_mass_evidence,
                                    height_hierarchy_evidence,
                                    height_emphasis_evidence))
                else "unavailable"),
            "status_source": status_source(
                "building_mass_evidence + height hierarchy evidence",
                "建筑中频与高度层级证据证明有边界建筑处理已经运行"),
            "evidence": {
                "building_mass": dict(building_mass_evidence),
                "height_hierarchy": dict(height_hierarchy_evidence),
                "height_emphasis": dict(height_emphasis_evidence),
            },
        },
        {
            "id": "S7", "status": "completed",
            "status_source": status_source(
                "diagnostic artifacts",
                "依据生成后的诊断与构图产物确认状态"),
            "evidence": {
                key: value for key, value in artifact_refs.items()
                if key in {"scene_character", "scene_policy",
                           "composition_spec", "review"}
            },
        },
        {
            "id": "S8",
            "status": "completed" if mesh_ready else "unavailable",
            "status_source": status_source(
                "meshes", "至少观测到一个已经生成的语义网格"),
            "evidence": {"meshes": mesh_summary},
        },
        {
            "id": "S9",
            "status": (
                "completed" if (geometry_gate or block_base_clearance)
                else "not_applicable"),
            "status_source": status_source(
                "block_base_clearance + meshes",
                "依据街区底座间隙与网格水密性证据确认状态"),
            "evidence": {
                "geometry_gate": dict(geometry_gate or {}),
                "block_base_clearance": dict(block_base_clearance or {}),
                "watertight_by_object": {
                    name: item.get("watertight")
                    for name, item in mesh_summary.items()
                    if item.get("status") == "ready"
                },
            },
        },
        {
            "id": "S10",
            "status": "completed" if bundle_complete else "unavailable",
            "status_source": generation_gate["status_source"],
            "evidence": {
                **artifact_refs,
                "gate": generation_gate,
                "artifacts": artifact_refs,
            },
        },
        {
            "id": "S11",
            "status": (
                "completed" if acceptance_gate["status"] == "accepted"
                else "failed" if acceptance_gate["status"] == "rejected"
                else "pending_validation"),
            "status_source": acceptance_gate["status_source"],
            "evidence": acceptance_gate,
        },
    ]
    supplied_statuses = dict(stage_statuses or {})
    runtime_by_id = {stage["id"]: stage for stage in stage_runtime}
    stages = []
    for spec in PIPELINE_STAGES:
        runtime = runtime_by_id.get(spec.id, {})
        supplied = supplied_statuses.get(spec.id)
        if isinstance(supplied, Mapping):
            supplied_status = supplied.get("status")
            supplied_source = supplied.get("status_source") or {
                "kind": "provided_stage_status",
                "reference": f"stage_statuses.{spec.id}",
                "rule": "explicit lifecycle status supplied by caller",
            }
        else:
            supplied_status = supplied
            supplied_source = {
                "kind": "provided_stage_status",
                "reference": f"stage_statuses.{spec.id}",
                "rule": "explicit lifecycle status supplied by caller",
            }
        stages.append({
            "id": spec.id,
            "name": spec.name,
            "status": supplied_status or runtime.get("status", "not_run"),
            "status_source": (
                supplied_source if supplied_status
                else runtime.get("status_source", {
                    "kind": "default",
                    "reference": "pipeline_contract",
                    "rule": "no runtime evidence was supplied",
                })),
            "input": list(spec.inputs),
            "output": list(spec.outputs),
            "context_in": spec.context_in,
            "context_out": spec.context_out,
            "applicable_modes": list(spec.applicable_modes),
            "summary": spec.summary,
            "evidence": runtime.get("evidence", {}),
        })

    effective_revision = (
        revision if revision is not None else run.get("revision"))
    identity = {
        "run_id": _first_present(run_id, run.get("run_id"), run.get("id")),
        "attempt_id": _first_present(
            attempt_id, run.get("attempt_id"), run.get("attempt")),
        "revision": effective_revision,
        "derived_from_revision": _first_present(
            derived_from_revision, run.get("derived_from_revision")),
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": contract_version or CONTRACT_VERSION,
        "language": "zh-CN",
        **identity,
        "run_identity": identity,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "purpose": "生成后只读观测；不会成为几何控制面",
        "run": dict(run),
        "measurement_summary": measurement,
        "generation_strategy": strategy,
        "delivery_gates": {
            "generation": generation_gate,
            "acceptance": acceptance_gate,
            "invariant": (
                "S10 产物生成完成不代表 S11 正式验收通过"),
        },
        "stages": stages,
        "artifacts": artifact_refs,
    }
    return _safe_report_value(report)


def _safe_observation_stem(stem: str) -> str:
    value = str(stem).strip()
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError("pipeline observation stem must be a plain filename")
    return value


def write_pipeline_observation_json(
        output_dir, report: Mapping, *,
        stem: str = "pipeline_observation") -> str:
    path = Path(output_dir) / f"{_safe_observation_stem(stem)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def _standalone_html(report: Mapping) -> str:
    payload = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")
    title = html.escape(str((report.get("run") or {}).get("city") or "Map"))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 生成过程观测报告</title>
<style>
:root{{--paper:#f3f0e9;--ink:#171714;--muted:#706d65;--line:#d4cec1;--accent:#c65332;--ok:#4d6d5f;--panel:#faf8f3}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}
#pipeline-observation{{max-width:1440px;margin:auto;padding:28px}} header{{display:flex;justify-content:space-between;gap:24px;align-items:end;border-bottom:1px solid var(--ink);padding-bottom:16px}}
h1{{font:600 32px/1.1 Georgia,serif;margin:0}} .meta{{color:var(--muted);text-align:right}} .layout{{display:grid;grid-template-columns:minmax(420px,1.15fr) minmax(360px,.85fr);gap:22px;margin-top:22px}}
.gates{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:18px}} .gate{{background:var(--panel);border:1px solid var(--line);padding:14px 16px}} .gate h2{{font-size:13px;letter-spacing:.08em;margin:0 0 5px}} .gate strong{{font-size:18px}} .gate-note{{display:block;color:var(--muted);margin-top:4px}}
.flow{{position:relative;padding-left:46px}} .flow:before{{content:"";position:absolute;left:18px;top:18px;bottom:18px;width:1px;background:var(--line)}}
button.stage{{position:relative;width:100%;display:grid;grid-template-columns:52px 1fr auto;gap:12px;align-items:center;text-align:left;background:transparent;border:0;border-bottom:1px solid var(--line);padding:13px 8px;cursor:pointer;color:inherit}}
button.stage:before{{content:"";position:absolute;left:-34px;width:9px;height:9px;border-radius:50%;background:var(--panel);border:2px solid var(--muted)}} button.stage[aria-pressed="true"]{{background:var(--panel)}} button.stage[aria-pressed="true"]:before{{background:var(--accent);border-color:var(--accent)}}
.sid{{font:600 12px ui-monospace,monospace;color:var(--accent)}} .sname{{font-weight:600}} .status{{font-size:12px;color:var(--muted)}} aside{{background:var(--panel);border:1px solid var(--line);padding:22px;min-height:600px;position:sticky;top:20px;align-self:start}}
aside h2{{font:600 24px/1.2 Georgia,serif;margin:0 0 12px}} .summary{{color:var(--muted);margin-bottom:12px}} .context-flow{{display:flex;align-items:center;gap:8px;padding:9px 11px;background:#ece8df;font:12px ui-monospace,monospace}} .context-flow b{{color:var(--accent)}} .io{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:18px 0}} h3{{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin:0 0 6px}} ul{{margin:0;padding-left:18px}} pre{{white-space:pre-wrap;word-break:break-word;background:#ece8df;padding:14px;max-height:420px;overflow:auto;font:12px/1.5 ui-monospace,monospace}}
.strategy{{margin-top:22px;border-top:1px solid var(--line);padding-top:18px}} .metric{{display:grid;grid-template-columns:1fr auto;gap:12px;padding:6px 0;border-bottom:1px dotted var(--line)}}
@media(max-width:820px){{#pipeline-observation{{padding:16px}} .layout,.gates{{grid-template-columns:1fr}} aside{{position:static;min-height:0}} header{{align-items:start;flex-direction:column}} .meta{{text-align:left}}}}
</style></head><body><main id="pipeline-observation">
<header><div><h1>{title} · 生成过程观测报告</h1><div>从数据到可打印 3MF 的逐级证据与策略</div></div><div class="meta" id="run-meta"></div></header>
<section class="gates" aria-label="交付门禁"><article class="gate"><h2>S10 · 生成交付物</h2><strong id="generation-gate">—</strong><span class="gate-note" id="generation-note"></span></article><article class="gate"><h2>S11 · 正式验收</h2><strong id="acceptance-gate">—</strong><span class="gate-note">生成成功不代表可打印验收通过</span></article></section>
<div class="layout"><section class="flow" id="stage-list"></section><aside aria-live="polite"><div id="stage-detail"></div><div class="strategy" id="strategy"></div></aside></div>
</main><script id="report-data" type="application/json">{payload}</script><script>
(()=>{{const report=JSON.parse(document.getElementById('report-data').textContent);const list=document.getElementById('stage-list');const detail=document.getElementById('stage-detail');const strategy=document.getElementById('strategy');const run=report.run||{{}};const identity=report.run_identity||{{}};document.getElementById('run-meta').textContent=[`运行 ${{identity.run_id??'—'}}`,`尝试 ${{identity.attempt_id??'—'}}`,`记录版本 r${{identity.revision??'—'}}`,`代码 ${{identity.derived_from_revision??'—'}}`,run.model_span_mm&&`${{run.model_span_mm}} 毫米`,run.area_km2&&`${{run.area_km2}} 平方公里`,report.generated_at].filter(Boolean).join(' · ');
const statusLabel=s=>({{completed:'完成',active:'已启用',inactive:'未启用',pending:'等待中',queued:'排队中',running:'运行中',pending_validation:'待正式验收',generated:'已生成',not_generated:'未生成',accepted:'已验收',rejected:'验收失败',failed:'失败',unavailable:'不可用',not_applicable:'不适用',not_run:'未运行'}}[s]||s);const valueLabel=s=>({{urban:'城市',mixed:'城市与山水混合',landscape:'自然景观',water_landscape:'水域景观',coast:'海岸',river_axis:'河流主轴',grid:'网格路网',active:'已启用',inactive:'未启用'}}[s]||s);const gates=report.delivery_gates||{{}};const generation=gates.generation||{{}};const acceptance=gates.acceptance||{{}};document.getElementById('generation-gate').textContent=statusLabel(generation.status||'not_generated');document.getElementById('generation-note').textContent=generation.bundle_complete?'S10 六件套已形成':'S10 六件套尚未完整形成';document.getElementById('acceptance-gate').textContent=statusLabel(acceptance.status||'pending_validation');const pretty=v=>JSON.stringify(v,null,2);function select(i){{[...list.children].forEach((b,j)=>b.setAttribute('aria-pressed',j===i?'true':'false'));const s=report.stages[i];detail.innerHTML=`<div class="sid">${{s.id}}</div><h2>${{s.name}}</h2><div class="status">${{statusLabel(s.status)}}</div><div class="summary"></div><h3>上下文交接</h3><div class="context-flow"><span>${{s.context_in}}</span><b>→</b><span>${{s.context_out}}</span></div><div class="io"><div><h3>输入</h3><ul>${{s.input.map(x=>`<li>${{x}}</li>`).join('')}}</ul></div><div><h3>输出</h3><ul>${{s.output.map(x=>`<li>${{x}}</li>`).join('')}}</ul></div></div><h3>状态来源</h3><pre class="status-source"></pre><h3>观测证据（技术字段）</h3><pre class="evidence"></pre>`;detail.querySelector('.summary').textContent=s.summary||'';detail.querySelector('.status-source').textContent=pretty(s.status_source);detail.querySelector('.evidence').textContent=pretty(s.evidence);}}
report.stages.forEach((s,i)=>{{const b=document.createElement('button');b.type='button';b.className='stage';b.setAttribute('aria-pressed','false');b.innerHTML=`<span class="sid">${{s.id}}</span><span class="sname">${{s.name}}</span><span class="status">${{statusLabel(s.status)}}</span>`;b.addEventListener('click',()=>select(i));list.appendChild(b)}});
const g=report.generation_strategy||{{}};const bg=g.block_grammar_strategy||{{}};strategy.innerHTML=`<h3>本次生成策略</h3><div class="metric"><span>场景类别</span><b>${{valueLabel(g.scene_class||'—')}}</b></div><div class="metric"><span>场景原型</span><b>${{valueLabel(g.archetype||'—')}}</b></div><div class="metric"><span>建筑模式</span><b>${{valueLabel(bg.mode||g.building_simplification||'—')}}</b></div><div class="metric"><span>聚合压力</span><b>${{bg.aggregation_pressure??'—'}}</b></div><div class="metric"><span>轮廓规整</span><b>${{bg.outline_regularization_pressure??'—'}}</b></div><div class="metric"><span>方向保护</span><b>${{bg.orientation_preservation_pressure??'—'}}</b></div><div class="metric"><span>纹理补强</span><b>${{bg.texture_promotion_pressure??'—'}}</b></div>`;select(4)}})();
</script></body></html>"""


def write_pipeline_observation_html(
        output_dir, report: Mapping, *,
        stem: str = "pipeline_observation") -> str:
    path = Path(output_dir) / f"{_safe_observation_stem(stem)}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_standalone_html(report), encoding="utf-8")
    return str(path)


def write_pipeline_observation(
        output_dir, report: Mapping, *,
        stem: str = "pipeline_observation") -> dict:
    return {
        "json": write_pipeline_observation_json(
            output_dir, report, stem=stem),
        "html": write_pipeline_observation_html(
            output_dir, report, stem=stem),
    }
