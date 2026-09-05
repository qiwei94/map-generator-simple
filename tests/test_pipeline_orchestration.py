import pytest

from aesthetic.pipeline_contract import stages_for_mode
from aesthetic.pipeline_orchestration import (
    CrossStageMutationError,
    MissingStageHandler,
    StageExecutionError,
    StageGateRejected,
    StageResult,
    execute_stage_chain,
)


def _handlers(*, calls, include_validation=False):
    handlers = {}
    for stage in stages_for_mode("full"):
        if stage.id == "S11" and not include_validation:
            continue

        def run(context, stage_id=stage.id):
            calls.append(stage_id)
            output = {**context, "last_stage": stage_id}
            if stage_id in {"S9", "S11"}:
                return StageResult(
                    context=output,
                    gate_passed=True,
                    evidence={"errors": 0, "warnings": 0},
                )
            return output

        handlers[stage.id] = run
    return handlers


def test_formal_generation_has_one_directional_s4_to_s10_then_pending_s11():
    calls = []
    execution = execute_stage_chain(
        {"city": "Chicago"}, _handlers(calls=calls), mode="full")

    assert calls == [f"S{index}" for index in range(11)]
    assert calls[4:] == ["S4", "S5", "S6", "S7", "S8", "S9", "S10"]
    assert execution.completed_stages == tuple(
        f"S{index}" for index in range(11))
    assert execution.pending_stages == ("S11",)
    assert execution.status == "generated_pending_validation"
    assert execution.context_type == "PipelineContextV10"
    for previous, current in zip(execution.events, execution.events[1:]):
        assert current.input_sha256 == previous.output_sha256
    assert [event.context_out for event in execution.events[4:]] == [
        "PipelineContextV4",
        "PipelineContextV5",
        "PipelineContextV6",
        "PipelineContextV7",
        "PipelineContextV8",
        "PipelineContextV9",
        "PipelineContextV10",
    ]


def test_s6_failure_stops_preview_mesh_gate_and_export():
    calls = []
    handlers = _handlers(calls=calls)

    def fail_building_stage(context):
        calls.append("S6")
        raise ValueError("building mass invalid")

    handlers["S6"] = fail_building_stage
    with pytest.raises(StageExecutionError) as failure:
        execute_stage_chain({"city": "Chicago"}, handlers, mode="full")

    assert failure.value.stage_id == "S6"
    assert calls == ["S0", "S1", "S2", "S3", "S4", "S5", "S6"]
    assert not set(calls) & {"S7", "S8", "S9", "S10", "S11"}


def test_s9_print_gate_must_explicitly_pass_before_s10_export():
    calls = []
    handlers = _handlers(calls=calls)

    def reject_clearance(context):
        calls.append("S9")
        return StageResult(
            context={**context, "last_stage": "S9"},
            gate_passed=False,
            evidence={"minimum_gap_mm": 0.22, "required_gap_mm": 0.84},
        )

    handlers["S9"] = reject_clearance
    with pytest.raises(StageGateRejected) as failure:
        execute_stage_chain({"city": "Chicago"}, handlers, mode="full")

    assert failure.value.stage_id == "S9"
    assert failure.value.evidence["minimum_gap_mm"] == 0.22
    assert calls[-1] == "S9"
    assert "S10" not in calls


def test_s9_plain_mapping_cannot_silently_bypass_print_gate():
    calls = []
    handlers = _handlers(calls=calls)
    handlers["S9"] = lambda context: {**context, "claimed": "checked"}

    with pytest.raises(StageGateRejected) as failure:
        execute_stage_chain({"city": "Chicago"}, handlers, mode="full")

    assert failure.value.stage_id == "S9"
    assert "S10" not in calls


def test_handler_must_return_new_context_instead_of_mutating_input():
    calls = []
    handlers = _handlers(calls=calls)

    def mutate_measurements(context):
        context["scene_character"] = {"invented": True}
        return context

    handlers["S4"] = mutate_measurements
    initial = {"city": "Chicago"}
    with pytest.raises(CrossStageMutationError):
        execute_stage_chain(initial, handlers, mode="full")

    assert initial == {"city": "Chicago"}
    assert "S5" not in calls


def test_missing_s5_strategy_cannot_jump_from_measurement_to_building():
    calls = []
    handlers = _handlers(calls=calls)
    del handlers["S5"]

    with pytest.raises(MissingStageHandler, match="S5"):
        execute_stage_chain({"city": "Chicago"}, handlers, mode="full")

    assert calls == ["S0", "S1", "S2", "S3", "S4"]
    assert "S6" not in calls


def test_explicit_s11_validation_can_finish_as_validated():
    calls = []
    execution = execute_stage_chain(
        {"city": "Chicago"},
        _handlers(calls=calls, include_validation=True),
        mode="full",
    )

    assert calls[-1] == "S11"
    assert execution.status == "validated"
    assert execution.pending_stages == ()
    assert execution.context_type == "AcceptanceReport"


def test_rejected_s11_acceptance_never_becomes_validated():
    calls = []
    handlers = _handlers(calls=calls, include_validation=True)

    def reject_validation(context):
        calls.append("S11")
        return StageResult(
            context={**context, "last_stage": "S11"},
            gate_passed=False,
            evidence={"errors": 1, "warnings": 0},
        )

    handlers["S11"] = reject_validation
    with pytest.raises(StageGateRejected) as failure:
        execute_stage_chain({"city": "Chicago"}, handlers, mode="full")

    assert failure.value.stage_id == "S11"
    assert failure.value.evidence == {"errors": 1, "warnings": 0}


@pytest.mark.parametrize("mode", ["review", "styles", "draft"])
def test_preview_modes_stop_at_s7_and_never_materialize_formal_mesh(mode):
    calls = []
    all_handlers = _handlers(calls=calls)
    execution = execute_stage_chain(
        {"city": "preview"}, all_handlers, mode=mode)

    assert calls == [f"S{index}" for index in range(8)]
    assert execution.completed_stages[-1] == "S7"
    assert execution.status == "completed"
    assert not set(calls) & {"S8", "S9", "S10", "S11"}
