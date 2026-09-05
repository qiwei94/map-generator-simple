"""Admin-only projection of one generation job into a pipeline console.

The console is deliberately read-only.  It summarizes job metadata, durable
progress events and files already emitted by the generator; it never feeds
values back into geometry generation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping


# ``python webapp/server.py`` imports this module before server.py adds the
# repository root to sys.path.  Keep the lightweight contract import available
# without importing any geometry dependencies.
_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from aesthetic.pipeline_contract import (  # noqa: E402
    CONTRACT_VERSION,
    PIPELINE_STAGES,
    stage_by_id,
    terminal_stage,
)
from aesthetic.pipeline_ledger import (  # noqa: E402
    PipelineLedgerError,
    validate_pipeline_ledger_state,
)


SCHEMA_VERSION = "admin-pipeline-console-v1"

_ALLOWED_SUFFIXES = {
    ".3mf", ".glb", ".html", ".jpeg", ".jpg", ".json", ".png",
    ".stl", ".txt",
}
_SOURCE_RE = re.compile(r"BL=(\d+)\s+BO=(\d+)\s+WL=(\d+)\s+WO=(\d+).*?roads=(\d+)")
_ATTEMPT_LEDGER_RE = re.compile(
    r"^pipeline_state\.[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+\.json$"
)


def _utc(ts: float | int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _compact(value: Any, depth: int = 0) -> Any:
    """Bound arbitrary sidecars before returning them to the browser."""
    if depth >= 6:
        if isinstance(value, (dict, list, tuple)):
            return "…"
        return value
    if isinstance(value, Mapping):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 80:
                result["…"] = f"其余 {len(value) - 80} 项已折叠"
                break
            label = str(key)
            lowered = label.lower()
            if any(token in lowered for token in (
                    "token", "secret", "password", "authorization")):
                continue
            if lowered in {"cmd", "command", "env", "environment"}:
                continue
            if lowered.endswith("path") and isinstance(item, str):
                item = Path(item).name
            result[label] = _compact(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = [_compact(item, depth + 1) for item in value[:40]]
        if len(value) > 40:
            result.append(f"…其余 {len(value) - 40} 项已折叠")
        return result
    if isinstance(value, str):
        return value if len(value) <= 800 else value[:800] + "…"
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)


def _artifact_kind(path: Path) -> str:
    if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        return "image"
    if path.suffix.lower() == ".json":
        return "context"
    if path.suffix.lower() in {".3mf", ".glb", ".stl"}:
        return "model"
    if path.suffix.lower() == ".html":
        return "report"
    return "file"


def _artifact_stage(name: str, *, mode: str = "full") -> str:
    lowered = name.lower()
    if (lowered == "pipeline_state.json"
            or _ATTEMPT_LEDGER_RE.fullmatch(name)):
        # The ledger is the run-wide source of truth, not an output of S10.
        # Keep it in the global artifact inventory without presenting it as a
        # seventh member of the canonical S10 delivery bundle.
        return "RUN"
    if "pipeline_measurement_report" in lowered:
        # S4 owns the measurements themselves; the assembled, human-readable
        # report is an S7 diagnostic artifact in the canonical contract.
        return "S7"
    if "scene_character" in lowered or "block_grammar" in lowered:
        return "S4"
    if "scene_policy" in lowered or "param_decision" in lowered:
        return "S5"
    if any(token in lowered for token in (
            "building_mass", "height_hierarchy", "height_emphasis")):
        return "S6"
    if any(token in lowered for token in (
            "composition", "diagnostic", "review", "topdown", "preview")):
        return "S7"
    if lowered.endswith((".glb", ".stl")):
        return "S7" if mode in {"draft", "styles", "review"} else "S8"
    if any(token in lowered for token in (
            "validator", "validation", "slicer", "acceptance")):
        return "S11"
    if any(token in lowered for token in (
            "clearance", "manifold")):
        return "S9"
    if lowered.endswith(".3mf") or "design_spec" in lowered or "pipeline_observation" in lowered:
        return "S10"
    return "S10"


def _artifact_record(path: Path, output_root: Path, *, mode: str = "full") -> dict:
    stat = path.stat()
    relative = path.relative_to(output_root)
    return {
        "name": path.name,
        "stage_id": _artifact_stage(path.name, mode=mode),
        "kind": _artifact_kind(path),
        "url": "/files/" + "/".join(relative.parts),
        "size_bytes": int(stat.st_size),
        "updated_at": _utc(stat.st_mtime),
    }


def scan_artifacts(job: Mapping, output_root: Path) -> list[dict]:
    city = str(job.get("city") or "")
    if not city or Path(city).name != city:
        return []
    city_dir = output_root / city
    directories = [
        city_dir,
        city_dir / ".pipeline_runs",
        output_root / "style_gallery" / city,
    ]
    started = float(job.get("started") or 0)
    allow_old = bool(job.get("cached"))
    results = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if (not path.is_file() or path.name.endswith(".part") or
                    path.suffix.lower() not in _ALLOWED_SUFFIXES):
                continue
            stat = path.stat()
            if started and not allow_old and stat.st_mtime < started - 2:
                continue
            results.append(_artifact_record(
                path, output_root, mode=str(job.get("mode") or "full")))
    return sorted(results, key=lambda item: (
        item["stage_id"], item["name"]))


def _find_sidecar(artifacts: Iterable[dict], name: str,
                  output_root: Path) -> dict:
    for artifact in artifacts:
        if artifact["name"] != name:
            continue
        relative = artifact["url"].removeprefix("/files/")
        return _read_json(output_root / Path(relative))
    return {}


def _find_claimed_sidecar(
    ledger: Mapping, job: Mapping, output_root: Path, role: str,
    *, preferred_stages: tuple[str, ...] = (),
) -> dict:
    """Read the exact JSON claimed by this attempt, with hash verification."""

    if not ledger:
        return {}
    city = str(job.get("city") or "")
    if not city or Path(city).name != city:
        return {}
    stages = {
        str(item.get("id") or ""): item
        for item in (ledger.get("stages") or []) if isinstance(item, Mapping)
    }
    order = preferred_stages or tuple(reversed([
        stage.id for stage in PIPELINE_STAGES]))
    for stage_id in order:
        stage = stages.get(stage_id) or {}
        for claim in stage.get("artifacts") or []:
            if not isinstance(claim, Mapping) or claim.get("name") != role:
                continue
            relative = Path(str(claim.get("path") or ""))
            if (not relative.parts or relative.is_absolute()
                    or ".." in relative.parts or relative.suffix != ".json"):
                return {}
            path = output_root / city / relative
            try:
                payload = path.read_bytes()
            except OSError:
                return {}
            if (len(payload) != claim.get("size_bytes")
                    or hashlib.sha256(payload).hexdigest()
                    != claim.get("sha256")):
                return {}
            try:
                result = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}
            return result if isinstance(result, dict) else {}
    return {}


def _find_attempt_sidecar(
    ledger: Mapping, job: Mapping, artifacts: Iterable[dict],
    output_root: Path, role: str, legacy_name: str,
    *, preferred_stages: tuple[str, ...] = (),
) -> dict:
    if ledger:
        return _find_claimed_sidecar(
            ledger, job, output_root, role,
            preferred_stages=preferred_stages)
    return _find_sidecar(artifacts, legacy_name, output_root)


def _attempt_sidecar_without_authority(name: str) -> bool:
    fixed_aliases = {
        "scene_character.json", "scene_policy.json", "composition_spec.json",
        "design_spec.json", "pipeline_measurement_report.json",
        "pipeline_measurement_report.html", "pipeline_observation.json",
        "pipeline_observation.html", "pipeline_measurement_report_s7.json",
        "pipeline_measurement_report_s7.html",
    }
    if name in fixed_aliases:
        return False
    return any(name.startswith(prefix) for prefix in (
        "scene_character.", "scene_policy.", "composition_spec.",
        "design_spec.", "pipeline_measurement_report.",
        "pipeline_measurement_report_s7.", "pipeline_observation.",
        "acceptance_report.", "validator_report.",
    ))


def valid_pipeline_ledger(ledger: Mapping) -> bool:
    """Use the core state-machine validator; the UI owns no weaker copy."""

    try:
        validate_pipeline_ledger_state(ledger)
    except (PipelineLedgerError, KeyError, TypeError, ValueError):
        return False
    return True


def _ledger_search_directories(job: Mapping, output_root: Path) -> list[Path]:
    city = str(job.get("city") or "")
    if not city or Path(city).name != city:
        return []
    city_dir = output_root / city
    return [city_dir, city_dir / ".pipeline_runs"]


def _parse_iso_timestamp(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace(
            "Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _pipeline_ledger(job: Mapping, output_root: Path) -> tuple[dict, str | None]:
    """Select only the newest ledger belonging to this job/run attempt.

    Attempt ledgers may coexist while two users generate the same city or a
    worker retries one run.  Never use "newest file in the city" as identity:
    first match run_id, optionally match the requested attempt_id, and only
    then compare revision/timestamps.  The old unscoped ``pipeline_state.json``
    remains a compatibility fallback only for legacy jobs that do not pin an
    attempt.  Once ``pipeline_attempt_id`` is present, a missing or invalid
    exact ledger must fail closed instead of showing another retry's state.
    """
    expected_run = str(
        job.get("pipeline_run_id") or job.get("run_id") or job.get("id") or ""
    )
    expected_attempt = str(
        job.get("pipeline_attempt_id") or job.get("attempt_id") or ""
    )
    attempt_candidates: list[tuple[dict, float, Path | None]] = []
    legacy_candidates: list[tuple[dict, float, Path]] = []
    live_ledger = job.get("pipeline_ledger") or {}
    if (valid_pipeline_ledger(live_ledger)
            and str(live_ledger.get("run_id") or "") == expected_run
            and (not expected_attempt or
                 str(live_ledger.get("attempt_id") or "") ==
                 expected_attempt)):
        # Heartbeats and uploaded/on-host ledgers are replicas of the same
        # attempt.  Revision, not transport, decides authority: after S11 is
        # performed on the API host a newer disk revision must supersede the
        # last S10 heartbeat instead of leaving the UI permanently stale.
        attempt_candidates.append((
            dict(live_ledger),
            _parse_iso_timestamp(live_ledger.get("updated_at")),
            None,
        ))
    for directory in _ledger_search_directories(job, output_root):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not path.is_file() or path.name.endswith(".part"):
                continue
            attempt_scoped = bool(_ATTEMPT_LEDGER_RE.fullmatch(path.name))
            if not attempt_scoped and path.name != "pipeline_state.json":
                continue
            if attempt_scoped and expected_attempt:
                if path.name != (
                        f"pipeline_state.{expected_run}."
                        f"{expected_attempt}.json"):
                    continue
            elif (attempt_scoped and expected_run and
                  not path.name.startswith(
                      f"pipeline_state.{expected_run}.")):
                continue
            ledger = _read_json(path)
            if not valid_pipeline_ledger(ledger):
                continue
            run_id = str(ledger.get("run_id") or "")
            attempt_id = str(ledger.get("attempt_id") or "")
            modified = float(path.stat().st_mtime)
            if attempt_scoped:
                # The filename is part of the attempt isolation contract.  A
                # mismatching payload is ignored instead of being guessed.
                expected_name = (
                    f"pipeline_state.{run_id}.{attempt_id}.json"
                )
                if path.name != expected_name:
                    continue
                if not expected_run or run_id != expected_run:
                    continue
                if expected_attempt and attempt_id != expected_attempt:
                    continue
                attempt_candidates.append((ledger, modified, path))
            elif not run_id or not expected_run or run_id == expected_run:
                legacy_candidates.append((ledger, modified, path))

    # A pinned attempt is an identity/security boundary, not merely a search
    # preference.  In particular, do not let a corrupt exact ledger silently
    # fall through to the mutable legacy alias from a previous retry.
    candidates = (
        attempt_candidates
        if expected_attempt
        else (attempt_candidates or legacy_candidates)
    )
    if not candidates:
        return {}, None

    def _selection_key(candidate: tuple[dict, float, Path | None]) -> tuple:
        ledger, modified, _ = candidate
        revision = ledger.get("revision")
        revision = revision if isinstance(revision, int) else -1
        updated = _parse_iso_timestamp(ledger.get("updated_at")) or modified
        # For one pinned attempt, revision is authoritative.  Across retries,
        # choose the most recently updated attempt, then its latest revision.
        if expected_attempt:
            return revision, updated, modified
        return updated, revision, modified

    ledger, _, path = max(candidates, key=_selection_key)
    relative = path.relative_to(output_root).as_posix() if path else None
    return ledger, relative


def _is_pipeline_state_artifact(artifact: Mapping) -> bool:
    name = str(artifact.get("name") or "")
    return name == "pipeline_state.json" or bool(
        _ATTEMPT_LEDGER_RE.fullmatch(name))


def _apply_ledger_artifact_stages(
    artifacts: Iterable[Mapping], ledger: Mapping, job: Mapping,
) -> list[dict]:
    """Prefer exact Stage provenance recorded by the selected attempt.

    Filename inference exists only for old runs.  A canonical Ledger already
    records the producing Stage for each artifact, so the administrator view
    must not relabel (for example) an S7 draft GLB as an S8 formal mesh.
    """

    city = str(job.get("city") or "")
    known_stage_ids = {stage.id for stage in PIPELINE_STAGES}
    claims: dict[str, str] = {}
    if city and Path(city).name == city:
        for claim in ledger.get("artifacts") or []:
            if not isinstance(claim, Mapping):
                continue
            stage_id = str(claim.get("stage_id") or "")
            relative = Path(str(claim.get("path") or ""))
            if (stage_id not in known_stage_ids or not relative.parts
                    or relative.is_absolute() or ".." in relative.parts):
                continue
            claims[(Path(city) / relative).as_posix()] = stage_id

    result = []
    for artifact in artifacts:
        item = dict(artifact)
        relative = str(item.get("url") or "").removeprefix("/files/")
        if relative in claims:
            item["stage_id"] = claims[relative]
            item["stage_source"] = "pipeline_ledger"
        result.append(item)
    return result


def _filter_artifacts_for_attempt(
    artifacts: Iterable[Mapping], ledger: Mapping, job: Mapping,
    ledger_relative_path: str | None,
) -> list[dict]:
    """Keep one job from seeing a concurrent same-city attempt's files."""

    if not ledger:
        if job.get("pipeline_attempt_id") or job.get("attempt_id"):
            # A modern job without its exact authority has no safe artifact
            # fallback.  Fixed aliases belong to "latest city output", not to
            # the requested attempt, and would reintroduce the same identity
            # bug even if Stage statuses themselves fail closed.
            return []
        # Without a valid run/attempt authority, fixed legacy aliases are the
        # only safe sidecar fallback.  Never guess among immutable files from
        # concurrent attempts in the same city directory.
        return [
            dict(item) for item in artifacts
            if not _attempt_sidecar_without_authority(str(item.get("name") or ""))
        ]
    city = str(job.get("city") or "")
    allowed = set()
    for claim in ledger.get("artifacts") or []:
        relative = Path(str(claim.get("path") or ""))
        if (relative.parts and not relative.is_absolute()
                and ".." not in relative.parts):
            allowed.add((Path(city) / relative).as_posix())
    if ledger_relative_path:
        allowed.add(ledger_relative_path)
    return [
        dict(item) for item in artifacts
        if str(item.get("url") or "").removeprefix("/files/") in allowed
    ]


def _ledger_context(stage: Mapping) -> dict:
    record = stage.get("context_out") or stage.get("context_in") or {}
    value = record.get("value") or {}
    return _compact(value) if isinstance(value, Mapping) else {}


def _context_handoff(
    stage_id: str,
    ledger_stage: Mapping,
    ledger_stages: Mapping[str, Mapping],
) -> dict:
    """Expose the already validated Context hash chain to the admin UI.

    The core ledger validator remains the authority.  This is a read-only
    projection that lets an operator distinguish a real predecessor handoff
    from a Stage label inferred from logs.
    """

    if not ledger_stage:
        return {
            "status": "unavailable",
            "input_sha256": None,
            "output_sha256": None,
            "predecessor_output_sha256": None,
        }

    context_in = ledger_stage.get("context_in")
    context_out = ledger_stage.get("context_out")
    input_sha = (
        str(context_in.get("sha256"))
        if isinstance(context_in, Mapping) and context_in.get("sha256")
        else None
    )
    output_sha = (
        str(context_out.get("sha256"))
        if isinstance(context_out, Mapping) and context_out.get("sha256")
        else None
    )
    order = stage_by_id(stage_id).order
    if order == 0:
        status = "root" if input_sha else "pending"
        predecessor_sha = None
    else:
        predecessor = ledger_stages.get(f"S{order - 1}") or {}
        predecessor_out = predecessor.get("context_out")
        predecessor_sha = (
            str(predecessor_out.get("sha256"))
            if (isinstance(predecessor_out, Mapping)
                and predecessor_out.get("sha256"))
            else None
        )
        if input_sha and predecessor_sha:
            status = "verified" if input_sha == predecessor_sha else "mismatch"
        elif str(ledger_stage.get("status") or "") in {
                "completed", "running", "failed", "rejected"}:
            status = "missing"
        else:
            status = "pending"
    return {
        "status": status,
        "input_sha256": input_sha,
        "output_sha256": output_sha,
        "predecessor_output_sha256": predecessor_sha,
    }


def _measurement_report_context(report: Mapping) -> dict:
    """Return the useful admin projection without duplicating raw evidence.

    The complete report remains available as an admin-protected artifact.  A
    pipeline poll should stay small, so S4 exposes coverage and the registered
    measurement -> decision -> consumer chains, but never embeds
    ``raw_measurements`` or the leaf-by-leaf ``measurement_index``.
    """
    coverage = report.get("coverage") or {}
    chains = []
    for chain in report.get("impact_chains") or []:
        if not isinstance(chain, Mapping):
            continue
        chains.append({key: chain.get(key) for key in (
            "id", "title", "measurement_leaf_count", "consumers",
            "decision_outputs", "realized_status", "effect",
            "forbidden_controls",
        ) if chain.get(key) is not None})
    realizations = []
    for row in report.get("realization_matrix") or []:
        if not isinstance(row, Mapping):
            continue
        realizations.append({key: row.get(key) for key in (
            "effect_id", "title", "consumer_stage", "consumer_symbol",
            "target_fields", "effect_kind", "realization_status",
            "consumer_called", "applied_to_current_run",
            "actual_outcome_paths", "mismatch_reason", "hard_bounds",
        ) if row.get(key) is not None})
    return {
        "schema_version": report.get("schema_version"),
        "status": report.get("status"),
        "generated_at": report.get("generated_at"),
        "city": report.get("city"),
        "coverage": coverage,
        "impact_chains": chains,
        "realization_matrix": realizations,
        "detail": "完整逐项证据请打开本 Stage 的管理员测量报告产物",
    }


def _event_timings(events: Iterable[Mapping]) -> dict[str, dict]:
    timings: dict[str, dict] = {}
    for event in events:
        data = event.get("data") or {}
        try:
            pct = int(data.get("progress_pct") or 0)
        except (TypeError, ValueError):
            pct = 0
        created_at = float(event.get("created_at") or 0)
        for index, stage in enumerate(PIPELINE_STAGES):
            next_threshold = (
                PIPELINE_STAGES[index + 1].progress_threshold
                if index + 1 < len(PIPELINE_STAGES) else 101
            )
            item = timings.setdefault(stage.id, {})
            if pct >= stage.progress_threshold and "started_at" not in item:
                item["started_at"] = _utc(created_at)
            if pct >= next_threshold and "completed_at" not in item:
                item["completed_at"] = _utc(created_at)
    return timings


def _meaningful_mapping(value: Any) -> dict:
    """Drop empty placeholders before merging live context over old evidence."""
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item for key, item in value.items()
        if item not in (None, "", [], {})
    }


def _issue_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        return len(value)
    return None


def _zero_issues(report: Mapping) -> bool:
    errors = _issue_count(report.get("errors"))
    warnings = _issue_count(report.get("warnings"))
    return errors == 0 and warnings == 0


def _acceptance_matches_ledger(report: Mapping, job: Mapping,
                               ledger: Mapping) -> bool:
    if not ledger:
        return False
    expected_run = str(ledger.get("run_id") or job.get("id") or "")
    expected_attempt = str(ledger.get("attempt_id") or "")
    if (str(report.get("run_id") or "") != expected_run
            or str(report.get("attempt_id") or "") != expected_attempt):
        return False
    claims = [item for item in ledger.get("artifacts") or []
              if isinstance(item, Mapping)
              and item.get("stage_id") == "S10"
              and item.get("name") == "3mf"]
    if len(claims) != 1:
        return False
    artifact = report.get("artifact") or {}
    digest = str(artifact.get("sha256") or "")
    return (bool(re.fullmatch(r"[0-9a-f]{64}", digest))
            and digest == claims[0].get("sha256"))


def _acceptance_identity_matches(report: Mapping, job: Mapping,
                                 ledger: Mapping) -> bool:
    return bool(ledger) and (
        str(report.get("run_id") or "") == str(
            ledger.get("run_id") or job.get("id") or "")
        and str(report.get("attempt_id") or "") == str(
            ledger.get("attempt_id") or "")
    )


def _acceptance_passed(report: Mapping, job: Mapping,
                       ledger: Mapping) -> bool:
    validator = report.get("validator") or {}
    slicer = report.get("slicer") or {}
    tool = slicer.get("tool") or {}
    checks = slicer.get("checks") or {}
    artifact = report.get("artifact") or {}
    return bool(
        report.get("schema_version") == "pipeline-acceptance-v1"
        and _acceptance_matches_ledger(report, job, ledger)
        and report.get("accepted") is True
        and _zero_issues(report)
        and isinstance(validator, Mapping)
        and validator.get("strict_passed") is True
        and _zero_issues(validator)
        and isinstance(slicer, Mapping)
        and slicer.get("schema_version") == "slicer-acceptance-v1"
        and slicer.get("artifact_sha256") == artifact.get("sha256")
        and slicer.get("status") in {"passed", "accepted"}
        and _zero_issues(slicer)
        and isinstance(tool, Mapping)
        and str(tool.get("name") or "").strip()
        and str(tool.get("version") or "").strip()
        and isinstance(checks, Mapping)
        and checks.get("loaded") is True
        and checks.get("sliced") is True
    )


def _validation_status(final_stage: Mapping, artifacts: Iterable[Mapping],
                       output_root: Path, *, job: Mapping,
                       ledger: Mapping) -> str:
    """Return the explicit S11 state; job completion alone is not validation."""
    if ledger:
        # A valid canonical Ledger is the sole S11 state authority.  An
        # unclaimed same-name report can be stale, copied or fabricated; it
        # may never advance or reject an attempt whose durable state is still
        # pending.  Completed/rejected/running Ledger states are handled by
        # the caller before this fallback is reached.
        return "pending_validation"
    declared = str(final_stage.get("status") or "").lower()
    if declared in {"failed", "error", "rejected"}:
        return "failed"
    saw_failure = False
    for item in artifacts:
        if item.get("stage_id") != "S11" or item.get("kind") != "context":
            continue
        relative = str(item.get("url") or "").removeprefix("/files/")
        report = _read_json(output_root / relative)
        if report.get("schema_version") == "pipeline-acceptance-v1":
            # Another retry may leave its immutable report in the same city
            # directory.  It is neither success nor failure for this attempt.
            if not _acceptance_identity_matches(report, job, ledger):
                continue
            if _acceptance_passed(report, job, ledger):
                return "completed"
            saw_failure = True
            continue
        errors = _issue_count(report.get("errors"))
        warnings = _issue_count(report.get("warnings"))
        if ((errors is not None and errors > 0)
                or (warnings is not None and warnings > 0)):
            saw_failure = True
        if report.get("strict_passed") is False:
            saw_failure = True
        # Validator-only evidence can reject an artifact, but it cannot prove
        # real slicer acceptance.  Keep successful legacy reports pending.
    return "failed" if saw_failure else "pending_validation"


def _live_contexts(job: Mapping, public_job: Mapping, log_tail: str,
                   artifacts: list[dict], output_root: Path,
                   ledger: Mapping | None = None) -> dict[str, dict]:
    requirements = dict(job.get("requirements") or {})
    if requirements.get("pbf_file"):
        requirements["pbf_file"] = Path(str(requirements["pbf_file"])).name
    source_counts = {}
    matches = list(_SOURCE_RE.finditer(log_tail or ""))
    if matches:
        bl, bo, wl, wo, roads = (int(value) for value in matches[-1].groups())
        source_counts = {
            "building_layers": bl, "building_objects": bo,
            "water_layers": wl, "water_objects": wo, "roads": roads,
        }
    scene_character = _find_attempt_sidecar(
        ledger or {}, job, artifacts, output_root, "scene_character",
        "scene_character.json", preferred_stages=("S4",))
    measurement_report = _find_attempt_sidecar(
        ledger or {}, job, artifacts, output_root, "measurement_report_json",
        "pipeline_measurement_report.json",
        preferred_stages=("S10", "S7"))
    if measurement_report:
        observation_context = {
            "measurement_report": _measurement_report_context(
                measurement_report),
        }
        scene_summary = scene_character.get("summary")
        if scene_summary:
            observation_context["scene_character_summary"] = scene_summary
    else:
        observation_context = scene_character

    contexts = {
        "S0": {
            "job": {key: job.get(key) for key in (
                "id", "city", "city_title", "mode", "style",
                "generation_profile", "bbox", "preview_bbox", "prototype")
                if job.get(key) is not None},
            "execution": {
                "status": public_job.get("status"),
                "worker_id": job.get("worker_id"),
                "retry_count": job.get("retry_count", 0),
            },
        },
        "S1": {"requirements": requirements,
               "source_feature_counts": source_counts},
        "S2": {"bbox": job.get("bbox"),
               "preview_bbox": job.get("preview_bbox"),
               "quality_checks": public_job.get("quality_checks") or []},
        "S3": {"source_feature_counts": source_counts,
               "stage_detail": public_job.get("stage_detail")},
        "S4": observation_context,
        "S5": _find_attempt_sidecar(
            ledger or {}, job, artifacts, output_root, "scene_policy",
            "scene_policy.json", preferred_stages=("S5",)),
        "S6": {},
        "S7": _find_attempt_sidecar(
            ledger or {}, job, artifacts, output_root, "composition_spec",
            "composition_spec.json", preferred_stages=("S7",)),
        "S8": {"emitted_models": [item for item in artifacts
                                    if item["kind"] == "model"]},
        "S9": {"quality_checks": public_job.get("quality_checks") or [],
               "quality_warnings": public_job.get("quality_warnings") or []},
        "S10": {"artifact_count": len(artifacts)},
        "S11": {"quality_checks": public_job.get("quality_checks") or [],
                "quality_warnings": public_job.get("quality_warnings") or [],
                "error_code": public_job.get("error_code"),
                "error_msg": public_job.get("error_msg")},
    }
    design_spec = _find_attempt_sidecar(
        ledger or {}, job, artifacts, output_root, "design_spec",
        "design_spec.json", preferred_stages=("S10",))
    if design_spec:
        decisions = design_spec.get("decisions") or design_spec
        # DesignSpec is an S10 delivery record.  Selected decision evidence
        # may be projected back onto the Stage that applied it for inspection,
        # but the document itself must never masquerade as an S2 input.
        contexts["S10"]["design_spec"] = design_spec
        if decisions.get("scene_policy") and not contexts["S5"]:
            contexts["S5"]["design_spec_scene_policy"] = decisions.get(
                "scene_policy", {})
        contexts["S6"]["building_mass_strategy"] = decisions.get(
            "building_mass_strategy", {})
        contexts["S9"]["block_base_clearance"] = decisions.get(
            "block_base_clearance", {})
    return {key: _compact(value) for key, value in contexts.items()}


def build_pipeline_console(job: Mapping, public_job: Mapping, *,
                           events: Iterable[Mapping], output_root: Path,
                           log_tail: str = "") -> dict:
    """Build the administrator view for a running or recovered job."""
    events = list(events)
    artifacts = scan_artifacts(job, output_root)
    ledger, ledger_relative_path = _pipeline_ledger(job, output_root)
    artifacts = _apply_ledger_artifact_stages(artifacts, ledger, job)
    artifacts = _filter_artifacts_for_attempt(
        artifacts, ledger, job, ledger_relative_path)
    artifacts = [
        item for item in artifacts
        if (not _is_pipeline_state_artifact(item) or
            str(item.get("url") or "").removeprefix("/files/") ==
            ledger_relative_path)
    ]
    if ledger_relative_path and not any(
            str(item.get("url") or "").removeprefix("/files/") ==
            ledger_relative_path for item in artifacts):
        selected_path = output_root / ledger_relative_path
        if selected_path.is_file():
            artifacts.append(_artifact_record(
                selected_path, output_root,
                mode=str(ledger.get("mode") or job.get("mode") or "full"),
            ))
    artifacts = sorted(artifacts, key=lambda item: (
        str(item.get("url") or "").removeprefix("/files/") !=
        ledger_relative_path,
        item["stage_id"], item["name"],
    ))[:120]
    ledger_stages = {
        str(item.get("id")): item for item in (ledger.get("stages") or [])
    }
    pinned_attempt = bool(
        job.get("pipeline_attempt_id") or job.get("attempt_id"))
    missing_exact_ledger = pinned_attempt and not ledger
    state_source = (
        "ledger" if ledger else
        "exact_ledger_missing" if missing_exact_ledger else
        "legacy_inferred"
    )
    contexts = _live_contexts(
        job, public_job, log_tail, artifacts, output_root, ledger=ledger)
    timings = _event_timings(events)
    progress = int(public_job.get("progress_pct") or 0)
    job_status = str(public_job.get("status") or job.get("status") or "pending")
    mode = str(ledger.get("mode") or job.get("mode") or "full")
    terminal_id = terminal_stage(mode).id
    terminal_index = next(
        index for index, stage in enumerate(PIPELINE_STAGES)
        if stage.id == terminal_id
    )

    final_report = _find_attempt_sidecar(
        ledger, job, artifacts, output_root, "pipeline_observation_json",
        "pipeline_observation.json", preferred_stages=("S10",))
    final_stages = {str(item.get("id")): item
                    for item in (final_report.get("stages") or [])}
    stages = []
    current_stage_id = "S0"
    for index, stage_spec in enumerate(PIPELINE_STAGES):
        stage_id = stage_spec.id
        ledger_stage = ledger_stages.get(stage_id) or {}
        next_threshold = (
            PIPELINE_STAGES[index + 1].progress_threshold
            if index + 1 < len(PIPELINE_STAGES) else 101
        )
        if ledger_stage:
            status = str(ledger_stage.get("status") or "pending")
            if (mode == "full" and stage_id == "S11"
                    and status == "pending_validation"):
                status = _validation_status(
                    final_stages.get("S11") or {}, artifacts, output_root,
                    job=job, ledger=ledger)
        elif index > terminal_index:
            status = "not_applicable"
        elif missing_exact_ledger:
            # A modern job's attempt id is an evidence boundary.  Progress
            # percentages and mutable same-city files may describe activity,
            # but they cannot prove that any canonical Stage completed for
            # this exact attempt.  Preserve the queued/running hint for S0;
            # fail closed for every completion claim.
            if job_status == "pending":
                status = "queued" if index == 0 else "pending"
            elif job_status == "running":
                status = "running" if index == 0 else "pending"
            else:
                status = "evidence_missing"
        elif job_status == "pending":
            status = "queued" if index == 0 else "pending"
        elif job_status == "done":
            status = "completed" if index <= terminal_index else "not_applicable"
            if mode == "full" and stage_id == "S11":
                status = _validation_status(
                    final_stages.get("S11") or {}, artifacts, output_root,
                    job=job, ledger=ledger)
        elif progress >= next_threshold:
            status = "completed"
        elif progress >= stage_spec.progress_threshold:
            status = "failed" if job_status == "failed" else "running"
        else:
            status = "pending"
        if status in {
                "queued", "running", "failed", "rejected",
                "pending_validation"}:
            current_stage_id = stage_id
        elif not ledger and job_status == "done" and index <= terminal_index:
            current_stage_id = stage_id

        final = final_stages.get(stage_id) or {}
        # Direct stage sidecars and live state are the explicit source.  The
        # post-run observation is a backwards-compatible fallback and must not
        # replace a newer/more specific stage context.
        final_context = _meaningful_mapping(final.get("evidence") or {})
        ledger_context = _ledger_context(ledger_stage)
        live_context = _meaningful_mapping(contexts.get(stage_id) or {})
        # The Ledger summary is authoritative for contract keys, while an
        # exact hash-verified artifact may add richer read-only evidence.  A
        # shallow precedence merge keeps both visible without letting a
        # sidecar override the durable handoff identity.
        if ledger_context:
            context = {
                **final_context,
                **live_context,
                **ledger_context,
            }
        else:
            context = live_context or final_context
        stage_artifacts = [item for item in artifacts
                           if item["stage_id"] == stage_id]
        stage = {
            "id": stage_id, "name": stage_spec.name,
            "status": status,
            "input": list(stage_spec.inputs),
            "output": list(stage_spec.outputs),
            "context_in": stage_spec.context_in,
            "context_out": stage_spec.context_out,
            "required_context_keys": list(stage_spec.required_context_keys),
            "context_handoff": _context_handoff(
                stage_id, ledger_stage, ledger_stages),
            "context": _compact(context),
            "artifacts": stage_artifacts,
            "state_source": (
                "ledger" if ledger_stage else
                "exact_ledger_missing" if missing_exact_ledger else
                "legacy_inferred"
            ),
            **(
                {
                    key: ledger_stage.get(key) for key in (
                        "started_at", "completed_at")
                    if ledger_stage.get(key)
                }
                if ledger_stage else timings.get(stage_id, {})
            ),
        }
        stages.append(stage)

    if ledger.get("current_stage_id"):
        current_stage_id = str(ledger["current_stage_id"])

    event_rows = []
    for event in events[-30:]:
        data = _compact(event.get("data") or {})
        event_rows.append({
            "id": event.get("id"), "type": event.get("type"),
            "created_at": _utc(event.get("created_at")), "data": data,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "pipeline_contract_version": CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "state_source": state_source,
        "integrity": {
            "status": (
                "verified" if ledger else
                "missing_exact_ledger" if missing_exact_ledger else
                "legacy_unbound"
            ),
            "message": (
                "精确 run/attempt Ledger 已验证" if ledger else
                "该任务已绑定 attempt，但精确 Ledger 缺失或无效；"
                "不会用日志百分比或同城文件推断 Stage 完成" if
                missing_exact_ledger else
                "旧任务没有 attempt 权威绑定，页面仅显示兼容推断"
            ),
        },
        "ledger_identity": _compact({
            key: ledger.get(key) for key in (
                "run_id", "attempt_id", "revision", "updated_at")
            if ledger.get(key) is not None
        }) if ledger else None,
        "job": _compact({
            "id": job.get("id"), "city": job.get("city"),
            "city_title": job.get("city_title") or job.get("city"),
            "mode": mode, "status": job_status,
            "progress_pct": progress, "elapsed_s": public_job.get("elapsed_s"),
            "stage_label": public_job.get("stage_label"),
            "stage_detail": public_job.get("stage_detail"),
            "worker_id": job.get("worker_id"),
        }),
        "current_stage_id": current_stage_id,
        "stages": stages,
        "artifacts": artifacts,
        "events": event_rows,
    }


def write_pipeline_snapshot(job_log_dir: Path, report: Mapping) -> Path:
    """Atomically persist the latest admin projection for recovery/audit."""
    job_id = str((report.get("job") or {}).get("id") or "")
    if not job_id or not job_id.isalnum():
        raise ValueError("invalid job id")
    path = job_log_dir / f"{job_id}_pipeline_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
    return path
