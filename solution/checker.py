"""Stepwise re-validation of an externally supplied plan (``check``)."""

from __future__ import annotations

from typing import Optional

from .errors import CaseError, err_type, err_value
from .model import (
    Case,
    RuntimeState,
    advance_wait,
    apply_action,
    initial_state,
)
from .planner import plan as run_planner
from .recovery import DoubleRecoveryCache, retirement_report


def _first_failure(
    case: Case, state: RuntimeState, cache: DoubleRecoveryCache
) -> Optional[dict]:
    """First violated retention policy at this state, deterministic order."""
    candidates = [
        seg
        for seg in case.policy_segments
        if seg.retain and seg.start <= state.step < seg.end
    ]
    for seg in sorted(candidates, key=lambda s: (s.target, s.start)):
        verdict = cache.verify(state, seg.target)
        if not verdict.ok:
            return {
                "step": state.step,
                "violation_code": "PRESERVATION_LOST",
                "action_id": state.history[-1]["action_id"]
                if state.history and state.history[-1]["step"] == state.step - 1
                else None,
                "affected_target": seg.target,
                "fault_domain": "none"
                if verdict.fault_domain is None
                else verdict.fault_domain,
            }
    return None


def check(case: Case, doc: dict) -> dict:
    raw_schedule = doc.get("schedule")
    if not isinstance(raw_schedule, list):
        raise err_type("schedule", "array", raw_schedule)
    if len(raw_schedule) > case.horizon:
        raise err_value(
            "schedule", "SCHEDULE_TOO_LONG",
            f"schedule has {len(raw_schedule)} steps but horizon is {case.horizon}",
        )

    cache = DoubleRecoveryCache(case, strict=True)
    state = initial_state(case)
    total_cost = 0
    action_ids: list[str] = []
    traces: list[dict] = []

    initial_failure = _first_failure(case, state, cache)
    if initial_failure is not None:
        return _violation(case, state, total_cost, action_ids, traces, initial_failure)

    for index, item in enumerate(raw_schedule, start=1):
        if not isinstance(item, dict):
            raise err_type(f"schedule[{index - 1}]", "object", item)
        item_type = item.get("type")
        if item_type == "wait":
            state = advance_wait(state)
            traces.append({"step": index, "type": "wait", "action_id": None})
            continue
        if item_type not in ("destroy", "migrate", "receipt"):
            raise err_value(
                f"schedule[{index - 1}].type", "UNKNOWN_KIND",
                f"type must be destroy, migrate, receipt or wait, got {item_type!r}",
            )
        action_id = item.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            raise err_type(
                f"schedule[{index - 1}].action_id", "non-empty string", action_id
            )
        action = case.actions.get(action_id)
        if action is None:
            return _violation(
                case, state, total_cost, action_ids, traces,
                {
                    "step": index,
                    "violation_code": "UNKNOWN_ACTION",
                    "action_id": action_id,
                    "affected_target": None,
                    "fault_domain": None,
                },
            )
        if action.kind != item_type:
            return _violation(
                case, state, total_cost, action_ids, traces,
                {
                    "step": index,
                    "violation_code": "ACTION_KIND_MISMATCH",
                    "action_id": action_id,
                    "affected_target": None,
                    "fault_domain": None,
                },
            )
        try:
            state = apply_action(case, state, action_id)
        except CaseError as exc:
            return _violation(
                case, state, total_cost, action_ids, traces,
                {
                    "step": index,
                    "violation_code": exc.code,
                    "action_id": action_id,
                    "affected_target": None,
                    "fault_domain": None,
                    "message": str(exc),
                },
            )
        total_cost += action.cost
        action_ids.append(action_id)

        failure = _first_failure(case, state, cache)
        if failure is not None:
            return _violation(
                case, state, total_cost, action_ids, traces, failure
            )
        traces.append(
            {
                "step": index,
                "type": action.kind,
                "action_id": action_id,
                "cost": action.cost,
            }
        )

    # All prefixes valid: check the end-state objectives.
    objective_failure = _check_objectives(case, state)
    if objective_failure is not None:
        return _violation(
            case, state, total_cost, action_ids, traces, objective_failure
        )

    result = run_planner(case)
    optimal = result.get("status") == "optimal"
    planner_key = None
    if optimal:
        planner_key = (
            result["total_cost"],
            result["action_count"],
            tuple(result["action_ids"]),
        )
    supplied_key = (total_cost, len(action_ids), tuple(action_ids))
    globally_optimal = optimal and planner_key == supplied_key

    return {
        "status": "valid" if globally_optimal else "valid_not_optimal",
        "first_violation": None,
        "total_cost": total_cost,
        "action_count": len(action_ids),
        "action_ids": action_ids,
        "final_step": state.step,
        "globally_optimal": globally_optimal,
        "planner_best": None
        if not optimal
        else {
            "total_cost": result["total_cost"],
            "action_count": result["action_count"],
            "action_ids": result["action_ids"],
        },
        "reports": _all_reports(case, state),
    }


def _check_objectives(case: Case, state: RuntimeState) -> Optional[dict]:
    for obj in case.objectives:
        if obj.goal != "retire":
            continue
        report = retirement_report(case, state, obj.target)
        if report.recoverable:
            return {
                "step": state.step,
                "violation_code": "RETIREMENT_NOT_ACHIEVED",
                "action_id": state.history[-1]["action_id"] if state.history else None,
                "affected_target": obj.target,
                "fault_domain": None,
                "minimal_witness": report.best_witness,
            }
    return None


def _violation(
    case: Case,
    state: RuntimeState,
    total_cost: int,
    action_ids: list[str],
    traces: list[dict],
    failure: dict,
) -> dict:
    return {
        "status": "invalid",
        "first_violation": failure,
        "total_cost": total_cost,
        "action_count": len(action_ids),
        "action_ids": list(action_ids),
        "final_step": state.step,
        "globally_optimal": False,
        "reports": _all_reports(case, state),
        "executed_prefix": traces,
    }


def _all_reports(case: Case, state: RuntimeState) -> list[dict]:
    reports: list[dict] = []
    for obj in case.objectives:
        if obj.goal == "retire":
            report = retirement_report(case, state, obj.target)
            reports.append(
                {
                    "target": obj.target,
                    "goal": "retire",
                    "recoverable": report.recoverable,
                    "minimal_witness": report.best_witness,
                    "irrecoverable_reason": report.irrecoverable_reason,
                    "blocking_leaves": report.blocking_leaves,
                }
            )
        else:
            cache = DoubleRecoveryCache(case, strict=True)
            verdict = cache.verify(state, obj.target)
            reports.append(
                {
                    "target": obj.target,
                    "goal": "retain",
                    "double_recoverable": verdict.ok,
                    "no_fault_witness": verdict.no_fault_witness,
                    "failed_fault_domain": verdict.fault_domain,
                }
            )
    return reports
