"""Formal S11 acceptance for one generated pipeline artifact.

S10 proves that an auditable bundle was generated.  This module is the only
supported transition from ``generated_pending_validation`` to ``validated``:
it re-runs the project validator, checks independently supplied real-slicer
evidence against the exact 3MF hash, writes immutable evidence, and advances
the same attempt ledger.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

from _TEXTURE_STYLE_OF_DEEPSEEK.validator import validate_3mf
from aesthetic.pipeline_contract import (
    CONTRACT_VERSION,
    S10_REQUIRED_ARTIFACT_BUNDLE,
)
from aesthetic.pipeline_ledger import PipelineLedger, PipelineLedgerError


ACCEPTANCE_SCHEMA_VERSION = "pipeline-acceptance-v1"
SLICER_SCHEMA_VERSION = "slicer-acceptance-v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_identity(path: str | os.PathLike) -> dict:
    """Hash a stable file and prove the requested path still names it."""

    requested = Path(path)
    resolved = requested.resolve(strict=True)
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        before = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, key) != getattr(after, key)
           for key in stable_fields):
        raise PipelineLedgerError("3MF changed while its identity was read")
    current_path = requested.resolve(strict=True)
    current = current_path.stat()
    if current_path != resolved or any(
            getattr(current, key) != getattr(after, key)
            for key in stable_fields):
        raise PipelineLedgerError("requested 3MF path changed during acceptance")
    return {
        "path": resolved,
        "sha256": digest.hexdigest(),
        "size_bytes": int(after.st_size),
    }


def _atomic_json(path: Path, payload: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False,
                      indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: str | os.PathLike) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("acceptance evidence must be a JSON object")
    return value


def validate_slicer_evidence(
    evidence: Mapping,
    *,
    artifact_sha256: str,
) -> list[str]:
    """Return fail-closed errors for real slicer evidence."""

    errors: list[str] = []
    if evidence.get("schema_version") != SLICER_SCHEMA_VERSION:
        errors.append("unsupported slicer evidence schema")
    if evidence.get("artifact_sha256") != artifact_sha256:
        errors.append("slicer evidence does not match the generated 3MF hash")
    if evidence.get("status") not in {"passed", "accepted"}:
        errors.append("slicer status is not passed")
    slicer_errors = evidence.get("errors")
    if not isinstance(slicer_errors, (list, tuple)):
        errors.append("slicer errors must be a list")
    elif slicer_errors:
        errors.append("slicer reported errors")
    slicer_warnings = evidence.get("warnings")
    if not isinstance(slicer_warnings, (list, tuple)):
        errors.append("slicer warnings must be a list")
    elif slicer_warnings:
        errors.append("slicer reported warnings")
    tool = evidence.get("tool") or {}
    if not isinstance(tool, Mapping) or not str(tool.get("name") or "").strip():
        errors.append("slicer tool name is missing")
    if not isinstance(tool, Mapping) or not str(tool.get("version") or "").strip():
        errors.append("slicer tool version is missing")
    checks = evidence.get("checks") or {}
    if not isinstance(checks, Mapping) or checks.get("loaded") is not True:
        errors.append("slicer did not prove that the 3MF loaded")
    if not isinstance(checks, Mapping) or checks.get("sliced") is not True:
        errors.append("slicer did not prove that slicing completed")
    return errors


def _resolve_output_dir(ledger_path: Path, output_dir) -> Path:
    parent = ledger_path.resolve().parent
    canonical = parent.parent if parent.name == ".pipeline_runs" else parent
    if output_dir is not None and Path(output_dir).resolve() != canonical:
        raise PipelineLedgerError(
            "output_dir cannot re-bind ledger artifact paths")
    return canonical


def _assert_s10_bundle(ledger: PipelineLedger) -> dict[str, dict]:
    """Verify every S10 claim against stable bytes under the ledger root."""

    state = ledger.snapshot()
    s11_running = (
        state.get("status") == "running"
        and state.get("current_stage_id") == "S11"
        and state.get("stages", [{}] * 12)[11].get("status") == "running"
    )
    if state.get("status") != "generated_pending_validation" and not s11_running:
        raise PipelineLedgerError(
            "S11 requires a pending or actively validating ledger")
    claims = [
        item for item in state.get("artifacts", [])
        if item.get("stage_id") == "S10"
    ]
    names = [str(item.get("name") or "") for item in claims]
    if (len(claims) != len(S10_REQUIRED_ARTIFACT_BUNDLE)
            or set(names) != set(S10_REQUIRED_ARTIFACT_BUNDLE)):
        raise PipelineLedgerError(
            "S10 ledger must contain exactly the required artifact bundle")

    identities: dict[str, dict] = {}
    for claim in claims:
        name = str(claim["name"])
        requested = ledger.output_dir / str(claim["path"])
        try:
            identity = _artifact_identity(requested)
        except (FileNotFoundError, OSError) as exc:
            raise PipelineLedgerError(
                f"S10 {name} artifact is missing or unreadable") from exc
        if identity["sha256"] != claim.get("sha256"):
            raise PipelineLedgerError(
                f"S10 {name} artifact hash no longer matches the file")
        if identity["size_bytes"] != claim.get("size_bytes"):
            raise PipelineLedgerError(
                f"S10 {name} artifact size no longer matches the file")
        identities[name] = identity
    return identities


def _recheck_s10_bundle_identity(
    ledger: PipelineLedger,
    initial: Mapping[str, Mapping],
) -> dict[str, dict]:
    """Prove that no claimed S10 file changed during formal acceptance."""

    current = _assert_s10_bundle(ledger)
    if set(current) != set(initial):
        raise PipelineLedgerError(
            "S10 artifact bundle identity changed during formal acceptance")
    for name, identity in current.items():
        expected = initial[name]
        if (identity["path"] != expected["path"]
                or identity["sha256"] != expected["sha256"]
                or identity["size_bytes"] != expected["size_bytes"]):
            raise PipelineLedgerError(
                f"S10 {name} artifact identity changed during formal acceptance")
    return current


def _assert_s10_artifact(
    ledger: PipelineLedger,
    artifact_path: Path,
    artifact_sha256: str,
    artifact_size_bytes: int,
) -> dict:
    state = ledger.snapshot()
    s11_running = (
        state.get("status") == "running"
        and state.get("current_stage_id") == "S11"
        and state.get("stages", [{}] * 12)[11].get("status") == "running"
    )
    if state.get("status") != "generated_pending_validation" and not s11_running:
        raise PipelineLedgerError(
            "S11 requires a pending or actively validating ledger")
    candidates = [
        item for item in state.get("artifacts", [])
        if item.get("stage_id") == "S10" and item.get("name") == "3mf"
    ]
    if len(candidates) != 1:
        raise PipelineLedgerError("S10 ledger must contain exactly one 3MF")
    claim = candidates[0]
    claimed_path = (ledger.output_dir / str(claim["path"])).resolve()
    if claimed_path != artifact_path.resolve():
        raise PipelineLedgerError("requested 3MF is not the S10 ledger artifact")
    if claim.get("sha256") != artifact_sha256:
        raise PipelineLedgerError("S10 3MF hash no longer matches the file")
    if claim.get("size_bytes") != artifact_size_bytes:
        raise PipelineLedgerError("S10 3MF size no longer matches the file")
    return claim


def _recheck_s10_artifact_identity(
    ledger: PipelineLedger,
    artifact_request: Path,
    initial_identity: Mapping,
) -> dict:
    """Re-bind an in-progress S11 decision to the unchanged S10 file."""

    final_identity = _artifact_identity(artifact_request)
    if (final_identity["path"] != initial_identity["path"]
            or final_identity["sha256"] != initial_identity["sha256"]
            or final_identity["size_bytes"]
            != initial_identity["size_bytes"]):
        raise PipelineLedgerError(
            "3MF identity changed during formal acceptance")
    _assert_s10_artifact(
        ledger,
        final_identity["path"],
        final_identity["sha256"],
        final_identity["size_bytes"],
    )
    return final_identity


def accept_pipeline_artifact(
    *,
    ledger_path: str | os.PathLike,
    artifact_path: str | os.PathLike,
    slicer_report_path: str | os.PathLike,
    output_dir: str | os.PathLike | None = None,
) -> dict:
    """Run S11 and durably update the same run/attempt ledger."""

    ledger_path = Path(ledger_path).resolve()
    output_root = _resolve_output_dir(ledger_path, output_dir)
    artifact_request = Path(artifact_path)
    ledger = PipelineLedger.load(ledger_path, output_dir=output_root)
    initial_bundle = _assert_s10_bundle(ledger)
    initial_identity = _artifact_identity(artifact_request)
    artifact = initial_identity["path"]
    artifact_hash = initial_identity["sha256"]
    _assert_s10_artifact(
        ledger, artifact, artifact_hash, initial_identity["size_bytes"])
    bundle_3mf = initial_bundle["3mf"]
    if (bundle_3mf["path"] != artifact
            or bundle_3mf["sha256"] != artifact_hash
            or bundle_3mf["size_bytes"] != initial_identity["size_bytes"]):
        raise PipelineLedgerError(
            "requested 3MF identity does not match the S10 bundle")

    # Transition before the expensive validator and slicer checks so the
    # administrator UI observes real S11 work instead of stale "pending".
    ledger.start_stage("S11")
    try:
        validator = validate_3mf(
            str(artifact),
            design_spec_path=str(initial_bundle["design_spec"]["path"]),
        )
        validator["strict_passed"] = bool(
            validator.get("passed")
            and not validator.get("errors")
            and not validator.get("warnings")
        )
        slicer = _read_json(slicer_report_path)
        validator_errors = [] if validator["strict_passed"] else [
            "project validator did not return 0 errors / 0 warnings"
        ]
        slicer_errors = validate_slicer_evidence(
            slicer, artifact_sha256=artifact_hash)
        errors = validator_errors + slicer_errors

        report = {
            "schema_version": ACCEPTANCE_SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "run_id": ledger.run_id,
            "attempt_id": ledger.attempt_id,
            "created_at": _now(),
            "artifact": {
                "filename": artifact.name,
                "sha256": artifact_hash,
                "size_bytes": initial_identity["size_bytes"],
            },
            "artifact_bundle": {
                name: {
                    "path": identity["path"].relative_to(
                        output_root).as_posix(),
                    "sha256": identity["sha256"],
                    "size_bytes": identity["size_bytes"],
                }
                for name, identity in sorted(initial_bundle.items())
            },
            "validator": validator,
            "slicer": slicer,
            "accepted": not errors,
            "errors": errors,
            "warnings": [],
        }

        # Bind the reports themselves to the unchanged file before they are
        # persisted as acceptance evidence.
        _recheck_s10_artifact_identity(
            ledger, artifact_request, initial_identity)
        _recheck_s10_bundle_identity(ledger, initial_bundle)

        report_path = output_root / (
            f"acceptance_report.{ledger.run_id}.{ledger.attempt_id}.json")
        validator_path = output_root / (
            f"validator_report.{ledger.run_id}.{ledger.attempt_id}.json")
        _atomic_json(validator_path, validator)
        _atomic_json(report_path, report)
        ledger.record_artifact(
            "S11", name="validator_report", path=validator_path,
            media_type="application/json")
        ledger.record_artifact(
            "S11", name="acceptance_report", path=report_path,
            media_type="application/json")
        context = {
            "schema_version": ACCEPTANCE_SCHEMA_VERSION,
            "accepted": report["accepted"],
            "errors": report["errors"],
            "warnings": report["warnings"],
            "artifact_sha256": artifact_hash,
            "validator_strict_passed": validator["strict_passed"],
            "slicer_status": slicer.get("status"),
        }

        # Report hashing/ledger writes are shorter but still non-zero.  Check
        # once more immediately before the terminal state transition.
        _recheck_s10_artifact_identity(
            ledger, artifact_request, initial_identity)
        _recheck_s10_bundle_identity(ledger, initial_bundle)
        if report["accepted"]:
            ledger.complete_stage("S11", context_out=context)
        else:
            ledger.reject_validation(
                context_out=context,
                reason="; ".join(errors) or "formal validation rejected",
            )
    except BaseException as exc:
        current = ledger.snapshot()["stages"][11]
        if current.get("status") == "running":
            ledger.fail_stage("S11", error=f"{type(exc).__name__}: {exc}")
        raise

    report["ledger_revision"] = ledger.revision
    report["ledger_status"] = ledger.snapshot()["status"]
    return report
