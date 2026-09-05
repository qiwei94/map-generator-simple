"""Atomic, append-forward state for one pipeline generation attempt.

``PipelineLedger`` records what happened; it never supplies geometry values
back to the generator.  Stage order and Context identity are enforced using
the canonical contract in :mod:`aesthetic.pipeline_contract`.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping
from uuid import uuid4

from aesthetic.pipeline_contract import (
    CARRIED_CONTEXT_KEYS,
    CONTRACT_VERSION,
    PIPELINE_STAGES,
    S10_REQUIRED_ARTIFACT_BUNDLE,
    S11_REQUIRED_ARTIFACT_BUNDLE,
    SEMANTIC_MESH_ROLES,
    stage_by_id,
    stages_for_mode,
    validate_stage_context,
)


LEDGER_SCHEMA_VERSION = "pipeline-ledger-v1"


class PipelineLedgerError(RuntimeError):
    """Base class for ledger contract failures."""


class StageOrderError(PipelineLedgerError):
    """Raised when a stage is started or completed out of order."""


class ContextChainError(PipelineLedgerError):
    """Raised when a stage receives a Context other than its predecessor's."""


class ConcurrentLedgerUpdateError(PipelineLedgerError):
    """Raised when another writer has advanced the same attempt ledger."""


class ArtifactBoundaryError(PipelineLedgerError):
    """Raised when an artifact is outside the ledger output directory."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise PipelineLedgerError(
            "pipeline ledger values must be finite JSON data") from exc
    return text.encode("utf-8")


def _context_record(context_type: str, value: Any) -> dict:
    value = {} if value is None else deepcopy(value)
    payload = _canonical_bytes(value)
    return {
        "type": context_type,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "value": value,
    }


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                value, stream, ensure_ascii=False, allow_nan=False,
                sort_keys=True, indent=2,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineLedgerError(f"cannot read pipeline ledger {path}") from exc
    if not isinstance(value, dict):
        raise PipelineLedgerError("pipeline ledger root must be a JSON object")
    return value


def _valid_context_record(record: Any, expected_type: str) -> bool:
    if not isinstance(record, Mapping) or record.get("type") != expected_type:
        return False
    digest = record.get("sha256")
    value = record.get("value")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or not isinstance(value, Mapping)):
        return False
    try:
        actual = hashlib.sha256(_canonical_bytes(value)).hexdigest()
    except PipelineLedgerError:
        return False
    return actual == digest


def _valid_artifact_claim(claim: Any, expected_stage_id: str) -> bool:
    if not isinstance(claim, Mapping):
        return False
    name = claim.get("name")
    relative = claim.get("path")
    digest = claim.get("sha256")
    size = claim.get("size_bytes")
    path = Path(relative) if isinstance(relative, str) else None
    return bool(
        isinstance(name, str) and name.strip()
        and claim.get("stage_id") == expected_stage_id
        and path is not None and path.parts and not path.is_absolute()
        and ".." not in path.parts
        and isinstance(size, int) and not isinstance(size, bool) and size >= 0
        and isinstance(digest, str) and len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest)
    )


def _completed_context_value(stages: list[Mapping[str, Any]], order: int) -> Mapping:
    """Return one already schema-validated Stage output Context value."""

    record = stages[order].get("context_out")
    value = record.get("value") if isinstance(record, Mapping) else None
    if not isinstance(value, Mapping):  # Defensive; the caller gates status.
        raise PipelineLedgerError(
            f"pipeline ledger S{order} output Context is unavailable")
    return value


def _validate_cross_stage_bindings(stages: list[Mapping[str, Any]]) -> None:
    """Bind evidence that cannot be proven inside one Stage schema alone.

    Stage schemas establish local shape and internal consistency.  These
    checks establish provenance across the immutable S6/S8/S9/S10/S11
    boundaries so a later Stage cannot replace valid-looking evidence with a
    different mesh/count/artifact claim.
    """

    s9 = stages[9]
    if s9.get("status") == "completed":
        s6_value = _completed_context_value(stages, 6)
        s8_value = _completed_context_value(stages, 8)
        s9_value = _completed_context_value(stages, 9)

        summary = s8_value["mesh_summary"]
        ready_roles = {
            role for role in SEMANTIC_MESH_ROLES
            if (isinstance(summary.get(role), Mapping)
                and summary[role].get("status") == "ready")
        }
        required_roles = set(s9_value["required_roles"])
        if required_roles != ready_roles:
            missing = sorted(ready_roles - required_roles)
            unexpected = sorted(required_roles - ready_roles)
            details = []
            if missing:
                details.append("ungated ready roles: " + ", ".join(missing))
            if unexpected:
                details.append(
                    "required roles not ready in S8: "
                    + ", ".join(unexpected))
            raise PipelineLedgerError(
                "pipeline ledger S9 required_roles do not match S8 ready "
                "semantic roles (" + "; ".join(details) + ")")

        metrics = s9_value["mesh_metrics"]
        for role in sorted(required_roles):
            s8_mesh = summary[role]
            s9_mesh = metrics[role]
            # v3 ledgers written before semantic-mesh-summary-v2 did not
            # retain winding_consistent.  All other geometry measurements
            # existed and must match; new summaries also bind winding.
            bound_fields = [
                "vertices", "faces", "watertight", "bounds_mm"]
            if "winding_consistent" in s8_mesh:
                bound_fields.append("winding_consistent")
            for field in bound_fields:
                s8_metric = s8_mesh.get(field)
                s9_metric = s9_mesh.get(field)
                if field == "bounds_mm":
                    # The S9 gate has always serialized bounds at six decimal
                    # places; pre-v2 S8 summaries used five.  Compare at the
                    # producer precision so persisted v3 ledgers stay valid,
                    # while newly written v2 summaries bind all six digits.
                    digits = (
                        6 if s8_value.get("mesh_summary_version") else 5)
                    if digits == 5:
                        metric_matches = all(
                            abs(float(left) - float(right)) <= 1.0e-5
                            for left_point, right_point in zip(
                                s8_metric, s9_metric)
                            for left, right in zip(left_point, right_point)
                        )
                    else:
                        s8_metric = [
                            [round(float(value), digits) for value in point]
                            for point in s8_metric
                        ]
                        s9_metric = [
                            [round(float(value), digits) for value in point]
                            for point in s9_metric
                        ]
                        metric_matches = s9_metric == s8_metric
                else:
                    metric_matches = s9_metric == s8_metric
                if not metric_matches:
                    raise PipelineLedgerError(
                        f"pipeline ledger S9 {role} {field} does not match "
                        "the S8 mesh summary")

        final_counts = s6_value["final_layer_counts"]
        expected_family_counts = {
            "roads": final_counts["roads"],
            "water": final_counts["WL"] + final_counts["WO"],
            "buildings": final_counts["BL"] + final_counts["BO"],
            "vegetation": final_counts["VL"] + final_counts["VO"],
        }
        survival_roles = s9_value["feature_survival"]["roles"]
        for family, expected in expected_family_counts.items():
            if survival_roles[family].get("final_count") != expected:
                raise PipelineLedgerError(
                    f"pipeline ledger S9 feature_survival {family} "
                    "final_count does not match S6 final_layer_counts")

        # A non-zero polygon/line count is only survival evidence when the
        # corresponding semantic mesh actually exists.  The building family
        # has two explicit routes: BL -> landmarks and BO -> buildings, or
        # BO -> block_base when the run intentionally merges mass polygons.
        # Vegetation remains the sole explicit omission permitted by S9.
        omitted = set(
            s9_value["feature_survival"].get("intentional_omissions", ()))

        def require_ready_role(family: str, role: str) -> None:
            if role not in ready_roles:
                raise PipelineLedgerError(
                    f"pipeline ledger S6 non-zero {family} has no ready "
                    f"S8 {role} mesh role")

        if expected_family_counts["roads"] > 0:
            require_ready_role("roads", "roads")
        if expected_family_counts["water"] > 0:
            require_ready_role("water", "water")
        if (expected_family_counts["vegetation"] > 0
                and "vegetation" not in omitted):
            require_ready_role("vegetation", "vegetation")
        if final_counts["BL"] > 0:
            require_ready_role("buildings.BL", "landmarks")
        if (final_counts["BO"] > 0
                and not ({"buildings", "block_base"} & ready_roles)):
            raise PipelineLedgerError(
                "pipeline ledger S6 non-zero buildings.BO has no ready S8 "
                "buildings or block_base mesh role")

    s11 = stages[11]
    if s11.get("status") in {"completed", "rejected"}:
        s11_value = _completed_context_value(stages, 11)
        s10_3mf = next(
            (claim for claim in stages[10].get("artifacts", ())
             if claim.get("name") == "3mf"),
            None,
        )
        if (not isinstance(s10_3mf, Mapping)
                or s11_value.get("artifact_sha256") != s10_3mf.get("sha256")):
            raise PipelineLedgerError(
                "pipeline ledger S11 artifact_sha256 does not match the "
                "S10 3mf artifact claim")


def validate_pipeline_ledger_state(state: Mapping[str, Any]) -> None:
    """Validate the complete durable state machine without touching files.

    This is the single semantic validator shared by local resume, remote
    worker upload and the administrator projection.  Transport-specific code
    may add identity/file checks, but it must not invent a weaker state model.
    """

    if not isinstance(state, Mapping):
        raise PipelineLedgerError("pipeline ledger root is invalid")
    if state.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise PipelineLedgerError("unsupported pipeline ledger schema")
    if state.get("contract_version") != CONTRACT_VERSION:
        raise PipelineLedgerError(
            "pipeline ledger was written for a different contract")
    if (not isinstance(state.get("run_id"), str)
            or not state["run_id"].strip()
            or not isinstance(state.get("attempt_id"), str)
            or not state["attempt_id"].strip()):
        raise PipelineLedgerError("pipeline ledger identity is invalid")
    mode = str(state.get("mode") or "")
    try:
        applicable_ids = {stage.id for stage in stages_for_mode(mode)}
    except ValueError as exc:
        raise PipelineLedgerError("pipeline ledger mode is invalid") from exc
    revision = state.get("revision")
    if (not isinstance(revision, int) or isinstance(revision, bool)
            or revision < 0):
        raise PipelineLedgerError("pipeline ledger revision is invalid")
    stages = state.get("stages")
    if (not isinstance(stages, list) or len(stages) != len(PIPELINE_STAGES)
            or not all(isinstance(item, Mapping) for item in stages)):
        raise PipelineLedgerError("pipeline ledger stage registry is invalid")

    allowed_statuses = {
        "pending", "running", "completed", "failed", "not_applicable",
        "pending_validation", "rejected",
    }
    seen_incomplete = False
    running_ids = []
    failed_ids = []
    flattened_artifacts = []
    artifact_paths = set()
    for spec, stage in zip(PIPELINE_STAGES, stages):
        applicable = spec.id in applicable_ids
        if (stage.get("id") != spec.id or stage.get("name") != spec.name
                or stage.get("order") != spec.order
                or stage.get("context_in_type") != spec.context_in
                or stage.get("context_out_type") != spec.context_out
                or stage.get("applicable") is not applicable):
            raise PipelineLedgerError(
                f"pipeline ledger {spec.id} registry fields are invalid")
        status = stage.get("status")
        if status not in allowed_statuses:
            raise PipelineLedgerError(
                f"pipeline ledger {spec.id} status is invalid")
        if not applicable:
            if (status != "not_applicable"
                    or stage.get("context_in") is not None
                    or stage.get("context_out") is not None
                    or stage.get("artifacts") != []):
                raise PipelineLedgerError(
                    f"pipeline ledger {spec.id} must be an empty "
                    "not_applicable Stage")
            continue
        if status == "not_applicable":
            raise PipelineLedgerError(
                f"pipeline ledger {spec.id} is unexpectedly not_applicable")
        if status == "running":
            running_ids.append(spec.id)
        if status in {"failed", "rejected"}:
            failed_ids.append(spec.id)
        if status == "pending_validation" and not (
                mode == "full" and spec.id == "S11"):
            raise PipelineLedgerError("pending_validation is only valid for S11")
        if status != "completed":
            seen_incomplete = True
        elif seen_incomplete:
            raise PipelineLedgerError("pipeline stages completed out of order")

        incoming = stage.get("context_in")
        outgoing = stage.get("context_out")
        if status in {"running", "completed", "failed", "rejected"}:
            if not _valid_context_record(incoming, spec.context_in):
                raise PipelineLedgerError(
                    f"pipeline ledger {spec.id} input Context is invalid")
        if status in {"completed", "rejected"}:
            if not _valid_context_record(outgoing, spec.context_out):
                raise PipelineLedgerError(
                    f"pipeline ledger {spec.id} output Context is invalid")
            validate_stage_context(
                spec.id, outgoing["value"],
                outcome="reject" if status == "rejected" else "complete")
            if (spec.id == "S0"
                    and outgoing["value"].get("mode") != mode):
                raise PipelineLedgerError(
                    "pipeline ledger S0 mode does not match root mode")
        elif outgoing is not None:
            raise PipelineLedgerError(
                f"pipeline ledger {spec.id} has an unexpected output Context")
        if spec.order and incoming is not None:
            previous = stages[spec.order - 1].get("context_out")
            if previous is None or incoming != previous:
                raise PipelineLedgerError(
                    f"pipeline ledger Context chain breaks at {spec.id}")
        if status in {"completed", "rejected"}:
            incoming_value = (incoming or {}).get("value") or {}
            outgoing_value = (outgoing or {}).get("value") or {}
            for key in CARRIED_CONTEXT_KEYS.get(spec.id, ()):
                if outgoing_value.get(key) != incoming_value.get(key):
                    raise PipelineLedgerError(
                        f"pipeline ledger {spec.id} did not preserve {key}")

        claims = stage.get("artifacts")
        if not isinstance(claims, list):
            raise PipelineLedgerError(
                f"pipeline ledger {spec.id} artifacts are invalid")
        names = set()
        for claim in claims:
            if (not _valid_artifact_claim(claim, spec.id)
                    or claim["name"] in names
                    or claim["path"] in artifact_paths):
                raise PipelineLedgerError(
                    f"pipeline ledger {spec.id} artifact claim is invalid")
            names.add(claim["name"])
            artifact_paths.add(claim["path"])
            flattened_artifacts.append(dict(claim))
        if claims and status not in {
                "running", "completed", "failed", "rejected"}:
            raise PipelineLedgerError(
                f"pipeline ledger {spec.id} has premature artifacts")
        if spec.id == "S10" and status == "completed" and names != set(
                S10_REQUIRED_ARTIFACT_BUNDLE):
            raise PipelineLedgerError("pipeline ledger S10 bundle is incomplete")
        if (spec.id == "S11" and status in {"completed", "rejected"}
                and names != set(S11_REQUIRED_ARTIFACT_BUNDLE)):
            raise PipelineLedgerError(
                "pipeline ledger S11 evidence bundle is incomplete")

    _validate_cross_stage_bindings(stages)

    root_artifacts = state.get("artifacts")
    if not isinstance(root_artifacts, list):
        raise PipelineLedgerError("pipeline ledger artifact index is invalid")
    try:
        stage_claims = sorted(_canonical_bytes(item)
                              for item in flattened_artifacts)
        root_claims = sorted(_canonical_bytes(item) for item in root_artifacts)
    except PipelineLedgerError as exc:
        raise PipelineLedgerError(
            "pipeline ledger artifact index is not JSON-safe") from exc
    if stage_claims != root_claims:
        raise PipelineLedgerError(
            "pipeline ledger artifact index does not match Stage claims")
    if len(running_ids) > 1 or len(failed_ids) > 1:
        raise PipelineLedgerError("pipeline ledger has multiple active outcomes")

    current = state.get("current_stage_id")
    if current is not None and current not in applicable_ids:
        raise PipelineLedgerError("pipeline ledger current Stage is invalid")
    root_status = state.get("status")
    if root_status == "initialized":
        valid_root = current is None and all(
            item["status"] in {"pending", "not_applicable"} for item in stages)
    elif root_status == "running":
        valid_root = (not failed_ids and (
            current in running_ids if running_ids else current is None))
    elif root_status == "failed":
        valid_root = (not running_ids and len(failed_ids) == 1
                      and stages[int(failed_ids[0][1:])]["status"] == "failed"
                      and current == failed_ids[0])
    elif root_status == "generated_pending_validation":
        valid_root = (not running_ids and not failed_ids
                      and mode == "full" and current == "S11"
                      and stages[10]["status"] == "completed"
                      and stages[11]["status"] == "pending_validation")
    elif root_status == "validation_rejected":
        valid_root = (not running_ids and failed_ids == ["S11"]
                      and mode == "full" and current == "S11"
                      and stages[11]["status"] == "rejected")
    elif root_status in {"completed", "validated"}:
        terminal = stages_for_mode(mode)[-1]
        expected = "validated" if terminal.id == "S11" else "completed"
        valid_root = (not running_ids and not failed_ids
                      and root_status == expected and current is None
                      and stages[terminal.order]["status"] == "completed")
    else:
        valid_root = False
    if not valid_root:
        raise PipelineLedgerError("pipeline ledger root state is inconsistent")


def _lock_path(path: Path) -> Path:
    """Return the stable lock inode shared by all revisions of ``path``.

    The ledger itself is replaced atomically for every revision, so locking
    that inode would stop protecting writers as soon as ``os.replace`` runs.
    A persistent sidecar is intentionally retained beside the ledger.
    """

    return path.with_name(f".{path.name}.lock")


@contextmanager
def _exclusive_ledger_lock(path: Path):
    """Hold an inter-process exclusive lock for one ledger commit.

    POSIX hosts (including macOS, Linux and WSL) use ``flock``.  Native
    Windows uses a one-byte ``msvcrt.locking`` region.  Both locks are owned
    by the open file description and are released by the OS if a writer
    exits unexpectedly.
    """

    lock_path = _lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI.
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {
                            errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class PipelineLedger:
    """Durable lifecycle for one ``run_id`` / ``attempt_id`` pair."""

    def __init__(self, path: Path, output_dir: Path, state: dict):
        self.path = path.resolve()
        self.output_dir = output_dir.resolve()
        self._state = state
        self._expected_revision = int(state["revision"])

    @classmethod
    def create(
        cls,
        path: str | os.PathLike,
        *,
        output_dir: str | os.PathLike,
        run_id: str,
        mode: str,
        attempt_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "PipelineLedger":
        path = Path(path)
        output_dir = Path(output_dir)
        if path.exists():
            raise FileExistsError(f"pipeline ledger already exists: {path}")
        if not str(run_id).strip():
            raise ValueError("run_id must not be empty")
        applicable_ids = {stage.id for stage in stages_for_mode(mode)}
        created_at = _now()
        state = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "run_id": str(run_id),
            "attempt_id": str(attempt_id or uuid4().hex),
            "mode": mode,
            "revision": 0,
            "status": "initialized",
            "current_stage_id": None,
            "created_at": created_at,
            "updated_at": created_at,
            "metadata": deepcopy(dict(metadata or {})),
            "artifacts": [],
            "stages": [
                {
                    "id": stage.id,
                    "name": stage.name,
                    "order": stage.order,
                    "context_in_type": stage.context_in,
                    "context_out_type": stage.context_out,
                    "applicable": stage.id in applicable_ids,
                    "status": (
                        "pending" if stage.id in applicable_ids
                        else "not_applicable"
                    ),
                    "started_at": None,
                    "completed_at": None,
                    "context_in": None,
                    "context_out": None,
                    "artifacts": [],
                    "error": None,
                }
                for stage in PIPELINE_STAGES
            ],
        }
        _canonical_bytes(state)
        ledger = cls(path, output_dir, state)
        ledger._commit(state, creating=True)
        return ledger

    @classmethod
    def load(
        cls,
        path: str | os.PathLike,
        *,
        output_dir: str | os.PathLike | None = None,
    ) -> "PipelineLedger":
        path = Path(path)
        state = _read_json(path)
        cls._validate_loaded_state(state)
        if output_dir is not None:
            root = Path(output_dir)
        elif path.parent.name == ".pipeline_runs":
            root = path.parent.parent
        else:
            root = path.parent
        return cls(path, root, state)

    @staticmethod
    def _validate_loaded_state(state: Mapping[str, Any]) -> None:
        validate_pipeline_ledger_state(state)

    @property
    def run_id(self) -> str:
        return str(self._state["run_id"])

    @property
    def attempt_id(self) -> str:
        return str(self._state["attempt_id"])

    @property
    def revision(self) -> int:
        return int(self._state["revision"])

    @property
    def mode(self) -> str:
        return str(self._state["mode"])

    def snapshot(self) -> dict:
        return deepcopy(self._state)

    def _stage(self, state: dict, stage_id: str) -> dict:
        stage_by_id(stage_id)
        return state["stages"][int(stage_id[1:])]

    def _next_stage(self, state: dict) -> dict | None:
        for stage in state["stages"]:
            if (stage["applicable"] and
                    stage["status"] in {"pending", "pending_validation"}):
                return stage
        return None

    def _assert_current_file(self) -> None:
        if not self.path.exists():
            raise ConcurrentLedgerUpdateError("pipeline ledger disappeared")
        disk = _read_json(self.path)
        identity = (disk.get("run_id"), disk.get("attempt_id"))
        expected_identity = (self.run_id, self.attempt_id)
        if identity != expected_identity:
            raise ConcurrentLedgerUpdateError(
                "pipeline ledger identity changed on disk")
        if disk.get("revision") != self._expected_revision:
            raise ConcurrentLedgerUpdateError(
                "pipeline ledger was advanced by another writer")

    def _commit(self, state: dict, *, creating: bool = False) -> None:
        # The lock must cover both the compare and replace.  Atomic replace
        # alone prevents torn JSON but does not make revision CAS atomic: two
        # writers could otherwise both observe revision N and publish N+1.
        with _exclusive_ledger_lock(self.path):
            if creating:
                if self.path.exists():
                    raise FileExistsError(
                        f"pipeline ledger already exists: {self.path}")
            else:
                self._assert_current_file()
            next_state = deepcopy(state)
            next_state["revision"] = int(self._expected_revision) + 1
            next_state["updated_at"] = _now()
            # Every producer commit passes through the same complete semantic
            # validator used by resume, worker upload and the admin console.
            # Method-level transition checks remain useful error messages,
            # but they are not a second, weaker definition of a valid ledger.
            validate_pipeline_ledger_state(next_state)
            _canonical_bytes(next_state)
            _atomic_write_json(self.path, next_state)
        self._state = next_state
        self._expected_revision = int(next_state["revision"])

    def start_stage(self, stage_id: str, *, context_in: Any = None) -> dict:
        state = deepcopy(self._state)
        stage = self._stage(state, stage_id)
        if not stage["applicable"]:
            raise StageOrderError(
                f"{stage_id} is not applicable to mode {self.mode!r}")
        if state["status"] in {
                "failed", "completed", "validated", "validation_rejected"}:
            raise StageOrderError(
                f"cannot start {stage_id} after run status {state['status']!r}")
        if any(item["status"] == "running" for item in state["stages"]):
            raise StageOrderError("another pipeline stage is already running")
        expected = self._next_stage(state)
        if expected is None or expected["id"] != stage_id:
            expected_id = expected["id"] if expected else None
            raise StageOrderError(
                f"cannot start {stage_id}; next stage is {expected_id!r}")

        spec = stage_by_id(stage_id)
        if spec.order == 0:
            incoming = _context_record(spec.context_in, context_in)
        else:
            previous = state["stages"][spec.order - 1]
            incoming = previous.get("context_out")
            if not incoming:
                raise ContextChainError(
                    f"{previous['id']} has no output Context for {stage_id}")
            if incoming.get("type") != spec.context_in:
                raise ContextChainError(
                    f"{stage_id} expected {spec.context_in}, got "
                    f"{incoming.get('type')}")
            if context_in is not None:
                supplied = _context_record(spec.context_in, context_in)
                if supplied["sha256"] != incoming["sha256"]:
                    raise ContextChainError(
                        f"{stage_id} input does not match {previous['id']} output")
            incoming = deepcopy(incoming)

        stage["context_in"] = incoming
        stage["status"] = "running"
        stage["started_at"] = _now()
        state["current_stage_id"] = stage_id
        state["status"] = "running"
        self._commit(state)
        return deepcopy(self._stage(self._state, stage_id))

    def _artifact_entry(
        self,
        stage_id: str,
        name: str,
        path: str | os.PathLike,
        media_type: str | None = None,
    ) -> dict:
        if not str(name).strip():
            raise ValueError("artifact name must not be empty")
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.output_dir / candidate
        resolved = candidate.resolve(strict=True)
        try:
            relative = resolved.relative_to(self.output_dir)
        except ValueError as exc:
            raise ArtifactBoundaryError(
                f"artifact must be inside {self.output_dir}") from exc
        if not resolved.is_file():
            raise FileNotFoundError(f"artifact is not a file: {resolved}")
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result = {
            "name": str(name),
            "stage_id": stage_id,
            "path": relative.as_posix(),
            "size_bytes": int(resolved.stat().st_size),
            "sha256": digest.hexdigest(),
        }
        if media_type:
            result["media_type"] = str(media_type)
        return result

    @staticmethod
    def _append_artifact(state: dict, stage: dict, artifact: dict) -> None:
        existing_names = {item["name"] for item in stage["artifacts"]}
        if artifact["name"] in existing_names:
            raise PipelineLedgerError(
                f"duplicate artifact name {artifact['name']!r} in {stage['id']}")
        existing_paths = {
            item["path"] for item in state.get("artifacts", [])
        }
        if artifact["path"] in existing_paths:
            raise PipelineLedgerError(
                f"artifact path already recorded: {artifact['path']}")
        stage["artifacts"].append(artifact)
        state.setdefault("artifacts", []).append(deepcopy(artifact))

    def record_artifact(
        self,
        stage_id: str,
        *,
        name: str,
        path: str | os.PathLike,
        media_type: str | None = None,
    ) -> dict:
        state = deepcopy(self._state)
        stage = self._stage(state, stage_id)
        if stage["status"] != "running":
            raise StageOrderError(
                f"artifacts may only be recorded while {stage_id} is running")
        artifact = self._artifact_entry(stage_id, name, path, media_type)
        self._append_artifact(state, stage, artifact)
        self._commit(state)
        return deepcopy(artifact)

    def complete_stage(
        self,
        stage_id: str,
        *,
        context_out: Any = None,
        artifacts: Mapping[str, str | os.PathLike] | None = None,
    ) -> dict:
        state = deepcopy(self._state)
        stage = self._stage(state, stage_id)
        if stage["status"] != "running":
            raise StageOrderError(f"{stage_id} is not running")
        spec = stage_by_id(stage_id)
        validate_stage_context(stage_id, context_out)
        if stage_id == "S0" and context_out.get("mode") != self.mode:
            raise PipelineLedgerError(
                "S0 Context mode must match the pipeline ledger mode")
        carried_fingerprints = CARRIED_CONTEXT_KEYS.get(stage_id, ())
        incoming_value = (stage.get("context_in") or {}).get("value") or {}
        for key in carried_fingerprints:
            if context_out.get(key) != incoming_value.get(key):
                raise ContextChainError(
                    f"{stage_id} did not preserve predecessor {key}")
        for name, path in (artifacts or {}).items():
            artifact = self._artifact_entry(stage_id, str(name), path)
            self._append_artifact(state, stage, artifact)
        if stage_id == "S10":
            declared = set(context_out["artifact_bundle"])
            recorded = {item["name"] for item in stage["artifacts"]}
            required = set(S10_REQUIRED_ARTIFACT_BUNDLE)
            missing_recorded = sorted(required - recorded)
            if missing_recorded:
                raise PipelineLedgerError(
                    "S10 required artifacts were not recorded: "
                    + ", ".join(missing_recorded))
            if declared != recorded:
                raise PipelineLedgerError(
                    "S10 artifact_bundle must exactly match recorded "
                    "artifact names")
        elif stage_id == "S11":
            recorded = {item["name"] for item in stage["artifacts"]}
            if recorded != set(S11_REQUIRED_ARTIFACT_BUNDLE):
                raise PipelineLedgerError(
                    "S11 formal acceptance requires validator_report and "
                    "acceptance_report artifacts")
        stage["context_out"] = _context_record(spec.context_out, context_out)
        stage["status"] = "completed"
        stage["completed_at"] = _now()
        state["current_stage_id"] = None

        terminal_id = stages_for_mode(self.mode)[-1].id
        if self.mode == "full" and stage_id == "S10":
            validation = self._stage(state, "S11")
            validation["status"] = "pending_validation"
            state["status"] = "generated_pending_validation"
            state["current_stage_id"] = "S11"
        elif stage_id == terminal_id:
            state["status"] = "validated" if stage_id == "S11" else "completed"
        else:
            state["status"] = "running"
        self._commit(state)
        return deepcopy(self._stage(self._state, stage_id))

    def fail_stage(self, stage_id: str, *, error: str) -> dict:
        state = deepcopy(self._state)
        stage = self._stage(state, stage_id)
        if stage["status"] != "running":
            raise StageOrderError(f"{stage_id} is not running")
        stage["status"] = "failed"
        stage["completed_at"] = _now()
        stage["error"] = str(error)
        state["status"] = "failed"
        state["current_stage_id"] = stage_id
        self._commit(state)
        return deepcopy(self._stage(self._state, stage_id))

    def reject_validation(
        self,
        *,
        context_out: Any,
        reason: str,
    ) -> dict:
        """Close S11 as rejected without mislabelling S10 generation failed.

        A valid artifact may still fail the project validator or a real
        slicer.  That outcome must remain distinguishable from a crashed
        generator and must never transition the run to ``validated``.
        """

        state = deepcopy(self._state)
        stage = self._stage(state, "S11")
        if self.mode != "full" or stage["status"] != "running":
            raise StageOrderError("S11 is not running for a full pipeline")
        spec = stage_by_id("S11")
        validate_stage_context("S11", context_out, outcome="reject")
        recorded = {item["name"] for item in stage["artifacts"]}
        if recorded != set(S11_REQUIRED_ARTIFACT_BUNDLE):
            raise PipelineLedgerError(
                "S11 formal rejection requires validator_report and "
                "acceptance_report artifacts")
        stage["context_out"] = _context_record(spec.context_out, context_out)
        stage["status"] = "rejected"
        stage["completed_at"] = _now()
        stage["error"] = str(reason)
        state["status"] = "validation_rejected"
        state["current_stage_id"] = "S11"
        self._commit(state)
        return deepcopy(self._stage(self._state, "S11"))
