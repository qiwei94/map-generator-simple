"""Small, dependency-free orchestration guard for staged generation.

This module does not know how to fetch OSM data or build a mesh.  It exists so
the process-shaped legacy generator can be decomposed behind a testable rule:
each stage receives a snapshot of only its predecessor's Context, must return a
new Context, and failures or rejected print gates stop all later consumers.

The durable :mod:`aesthetic.pipeline_ledger` remains the source of runtime
truth.  This helper is the in-memory execution seam used by unit tests and by
future extracted stage functions; it deliberately does not write files.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Mapping, Union

from aesthetic.pipeline_contract import stages_for_mode


class PipelineOrchestrationError(RuntimeError):
    """Base class for an invalid or failed stage execution."""


class MissingStageHandler(PipelineOrchestrationError):
    """Raised when the next applicable stage has no implementation."""


class CrossStageMutationError(PipelineOrchestrationError):
    """Raised when a handler mutates the Context snapshot it consumed."""


class StageGateRejected(PipelineOrchestrationError):
    """Raised when a required printability or validation gate rejects output."""

    def __init__(self, stage_id: str, evidence: Mapping[str, Any]):
        self.stage_id = stage_id
        self.evidence = deepcopy(dict(evidence))
        super().__init__(f"{stage_id} gate rejected the pipeline")


class StageExecutionError(PipelineOrchestrationError):
    """Wrap a stage exception without allowing execution to continue."""

    def __init__(self, stage_id: str, cause: Exception):
        self.stage_id = stage_id
        self.cause = cause
        super().__init__(
            f"{stage_id} failed: {type(cause).__name__}: {cause}")


@dataclass(frozen=True)
class StageResult:
    """Explicit output of one stage.

    ``gate_passed`` is normally ``None``.  S9 must set it to ``True`` before
    S10 may export; S11 must likewise make an explicit acceptance decision.
    """

    context: Mapping[str, Any]
    gate_passed: bool | None = None
    evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class StageEvent:
    stage_id: str
    context_in: str
    context_out: str
    input_sha256: str
    output_sha256: str
    gate_passed: bool | None


@dataclass(frozen=True)
class PipelineExecution:
    mode: str
    status: str
    completed_stages: tuple[str, ...]
    pending_stages: tuple[str, ...]
    context_type: str
    context: Mapping[str, Any]
    events: tuple[StageEvent, ...]


StageHandler = Callable[
    [Mapping[str, Any]], Union[Mapping[str, Any], StageResult]
]


def _canonical_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise PipelineOrchestrationError(
            "stage Context must contain finite JSON-compatible evidence") from exc
    return encoded.encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _normalise_result(
    raw: Union[Mapping[str, Any], StageResult],
) -> StageResult:
    if isinstance(raw, StageResult):
        result = raw
    elif isinstance(raw, Mapping):
        result = StageResult(context=raw)
    else:
        raise TypeError(
            "stage handler must return a Mapping or StageResult, got "
            f"{type(raw).__name__}")
    if not isinstance(result.context, Mapping):
        raise TypeError("StageResult.context must be a Mapping")
    # Validate before the next stage can observe the output.
    _canonical_bytes(result.context)
    _canonical_bytes(result.evidence or {})
    return result


def execute_stage_chain(
    initial_request: Mapping[str, Any],
    handlers: Mapping[str, StageHandler],
    *,
    mode: str = "full",
) -> PipelineExecution:
    """Execute applicable handlers in canonical order.

    A formal generation run may intentionally omit S11 because validation is a
    separate process.  In that one case, a successful S10 returns
    ``generated_pending_validation``.  Every other missing handler is an
    error, and no stage failure or rejected S9 gate is downgraded to success.
    """

    if not isinstance(initial_request, Mapping):
        raise TypeError("initial_request must be a Mapping")
    _canonical_bytes(initial_request)

    context = deepcopy(dict(initial_request))
    completed: list[str] = []
    events: list[StageEvent] = []
    stages = stages_for_mode(mode)

    for stage in stages:
        handler = handlers.get(stage.id)
        if stage.id == "S11" and mode == "full" and handler is None:
            return PipelineExecution(
                mode=mode,
                status="generated_pending_validation",
                completed_stages=tuple(completed),
                pending_stages=("S11",),
                context_type=stage.context_in,
                context=deepcopy(context),
                events=tuple(events),
            )
        if handler is None:
            raise MissingStageHandler(
                f"missing handler for applicable stage {stage.id}")

        # A stage receives a private snapshot, not the object retained by its
        # predecessor or by the caller.  Mutation is still rejected so the
        # handler cannot accidentally rely on in-place cross-stage state.
        incoming = deepcopy(context)
        input_digest = _digest(incoming)
        try:
            raw_result = handler(incoming)
            if _digest(incoming) != input_digest:
                raise CrossStageMutationError(
                    f"{stage.id} mutated its input Context; return a new one")
            result = _normalise_result(raw_result)
        except PipelineOrchestrationError:
            raise
        except Exception as exc:
            raise StageExecutionError(stage.id, exc) from exc

        if stage.id in {"S9", "S11"}:
            if result.gate_passed is not True:
                raise StageGateRejected(stage.id, result.evidence or {})

        output = deepcopy(dict(result.context))
        output_digest = _digest(output)
        events.append(StageEvent(
            stage_id=stage.id,
            context_in=stage.context_in,
            context_out=stage.context_out,
            input_sha256=input_digest,
            output_sha256=output_digest,
            gate_passed=result.gate_passed,
        ))
        completed.append(stage.id)
        context = output

    terminal = stages[-1]
    return PipelineExecution(
        mode=mode,
        status="validated" if terminal.id == "S11" else "completed",
        completed_stages=tuple(completed),
        pending_stages=(),
        context_type=terminal.context_out,
        context=deepcopy(context),
        events=tuple(events),
    )
