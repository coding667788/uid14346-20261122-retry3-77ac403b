"""Finite-horizon planner over the authorized action set.

The search is a depth-first branch-and-bound over steps.  At every reachable
state two layers of pruning apply:

1. **Prefix preservation** -- every target whose policy currently requires
   retention must stay double-recoverable (a no-fault witness *and* a
   witness for every single custody-domain outage).  A transition that
   breaks this is dropped immediately, so no infeasible branch is expanded.
2. **State memoization** -- states are identified by step, physical custody
   state and the set of already-used actions, but not by history.  If the
   same state was already reached with a no-worse
   ``(cost, action count, schedule)`` tuple, the new branch is dominated and
   skipped: every continuation available to it was already available to the
   earlier branch.

Double-recoverability verdicts are themselves cached per physical state in
:class:`~solution.recovery.DoubleRecoveryCache`, so the memoized states share
their recovery evidence instead of recomputing witnesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .errors import CaseError
from .model import (
    Action,
    Case,
    RuntimeState,
    advance_wait,
    apply_action,
    initial_state,
)
from .recovery import (
    DoubleRecoveryCache,
    retirement_report,
)


@dataclass
class SearchStats:
    states_enqueued: int = 0
    states_evaluated: int = 0
    transitions_rejected_prefix: int = 0
    transitions_rejected_window: int = 0
    states_pruned_dominated: int = 0
    cache: Optional[DoubleRecoveryCache] = None


def _schedule_key(sequence: tuple[Optional[str], ...]) -> tuple:
    # None (an explicit wait) sorts before every action id, giving a total
    # deterministic order over schedules of equal length.
    return tuple(("" if item is None else item) for item in sequence)


def _goal_satisfied(case: Case, state: RuntimeState) -> bool:
    for obj in case.objectives:
        if obj.goal == "retain":
            continue
        report = retirement_report(case, state, obj.target)
        if report.recoverable:
            return False
    return True


def _prefix_ok(
    case: Case,
    state: RuntimeState,
    cache: DoubleRecoveryCache,
) -> Optional[tuple[str, str]]:
    """Return (target, fault_domain) of the first failing retained target."""
    active = sorted(
        (
            seg
            for seg in case.policy_segments
            if seg.retain and seg.start <= state.step < seg.end
        ),
        key=lambda seg: (seg.target, seg.start),
    )
    for seg in active:
        verdict = cache.verify(state, seg.target)
        if not verdict.ok:
            return seg.target, (
                "none" if verdict.fault_domain is None else verdict.fault_domain
            )
    return None


def initial_prefix_check(case: Case) -> Optional[dict]:
    """Validate preservation at step 0, before any action is planned."""
    cache = DoubleRecoveryCache(case, strict=True)
    state = initial_state(case)
    failure = _prefix_ok(case, state, cache)
    if failure is None:
        return None
    target, fault = failure
    return {
        "step": 0,
        "target": target,
        "fault_domain": fault,
        "reason": "INITIAL_PRESERVATION_FAILURE",
    }


def plan(case: Case) -> dict:
    stats = SearchStats()
    cache = DoubleRecoveryCache(case, strict=True)
    stats.cache = cache
    start = initial_state(case)

    initial_failure = _prefix_ok(case, start, cache)
    if initial_failure is not None:
        target, fault = initial_failure
        return {
            "status": "infeasible",
            "reason": "INITIAL_PRESERVATION_FAILURE",
            "detail": {"target": target, "fault_domain": fault},
            "stats": _stats_json(stats),
        }

    best: Optional[tuple] = None
    best_sequence: Optional[list[Optional[str]]] = None
    best_state: Optional[RuntimeState] = None
    # state key -> best (cost, count, schedule-key) seen for that state
    visited: dict[tuple, tuple[int, int, tuple]] = {}
    retire_leaves: dict[str, frozenset[str]] = {
        obj.target: case.leaves_of[obj.target]
        for obj in case.objectives
        if obj.goal == "retire"
    }

    def _remaining_required(state: RuntimeState) -> int:
        """Lower bound on actions still needed for every retire objective."""
        needed = 0
        for leaves in retire_leaves.values():
            for cid in leaves:
                cred = state.creds.get(cid)
                if cred is None:
                    continue  # never registered: nothing to execute
                if cred.status == "destroyed":
                    continue
                if cred.status == "pending":
                    needed += 1  # still needs its receipt
                else:
                    needed += 2  # destroy + receipt
        return needed

    def dfs(state: RuntimeState, cost: int,
            sequence: list[Optional[str]]) -> None:
        nonlocal best, best_sequence, best_state
        stats.states_evaluated += 1

        marker = (cost, len(state.used), _schedule_key(tuple(sequence)))
        prior = visited.get(state.state_key())
        if prior is not None and prior <= marker:
            stats.states_pruned_dominated += 1
            return
        visited[state.state_key()] = marker

        if _goal_satisfied(case, state):
            candidate = marker
            if best is None or candidate < best:
                best = candidate
                best_sequence = list(sequence)
                best_state = state
            return

        if state.step > case.horizon:
            return
        # Incumbent upper bound: even a zero-cost continuation cannot beat
        # a plan with strictly smaller cost / fewer actions.
        if best is not None and (cost, len(state.used)) > best[:2]:
            return
        # Lower bound: not enough steps left to close the remaining
        # destroy/receipt work (each action consumes one step).
        remaining_steps = case.horizon - state.step
        if _remaining_required(state) > remaining_steps:
            stats.states_pruned_dominated += 1
            return

        candidates = _candidate_actions(case, state)
        for action in candidates:
            stats.states_enqueued += 1
            try:
                nxt = apply_action(case, state, action.action_id)
            except CaseError as exc:
                if exc.code == "OUTSIDE_WINDOW":
                    stats.transitions_rejected_window += 1
                else:
                    stats.transitions_rejected_prefix += 1
                continue
            failure = _prefix_ok(case, nxt, cache)
            if failure is not None:
                stats.transitions_rejected_prefix += 1
                continue
            sequence.append(action.action_id)
            dfs(nxt, cost + action.cost, sequence)
            sequence.pop()

        # Waiting is only useful when some unused action's window has not
        # opened yet.  If every remaining action is available now (or no
        # action remains), a wait can never enable anything new and is
        # skipped; this removes all pure trailing/interstitial dead waits
        # while keeping arbitrary multi-step waits for genuinely late
        # windows.
        if (
            state.step < case.horizon
            and _some_action_needs_later_step(case, state)
        ):
            sequence.append(None)
            dfs(advance_wait(state), cost, sequence)
            sequence.pop()

    dfs(start, 0, [])

    if best is None:
        return {
            "status": "infeasible",
            "reason": "NO_FEASIBLE_PLAN_WITHIN_HORIZON",
            "stats": _stats_json(stats),
        }

    return {
        "status": "optimal",
        "schedule": _schedule_json(case, best_state, best_sequence),
        "total_cost": best[0],
        "action_count": best[1],
        "action_ids": [a for a in best_sequence if a is not None],
        "final_step": best_state.step,
        "reports": _final_reports(case, best_state),
        "stats": _stats_json(stats),
    }


def _candidate_actions(case: Case, state: RuntimeState) -> list[Action]:
    out = [
        action
        for aid, action in case.actions.items()
        if aid not in state.used
        and action.window_start <= state.step < action.window_end
    ]
    # Deterministic expansion order; the final ordering is decided by the
    # explicit tie-break tuple anyway.  Receipts first so pending
    # destructions close promptly, then cheaper actions.
    return sorted(
        out,
        key=lambda a: (0 if a.kind == "receipt" else 1, a.cost, a.action_id),
    )


def _some_action_needs_later_step(case: Case, state: RuntimeState) -> bool:
    """Whether waiting can ever enable a currently unavailable action."""
    for aid, action in case.actions.items():
        if aid in state.used:
            continue
        if action.window_start > state.step and action.window_start <= case.horizon:
            return True
    return False


def _schedule_json(
    case: Case, final_state: RuntimeState, sequence: list[Optional[str]]
) -> list[dict]:
    items: list[dict] = []
    # Reconstruct per-step detail from the recorded history, which mirrors
    # the sequence (waits produce no history entry).
    history_iter = iter(final_state.history)
    for index, item in enumerate(sequence, start=1):
        if item is None:
            items.append(
                {"step": index, "type": "wait", "action_id": None}
            )
        else:
            entry = next(history_iter)
            action = case.actions[item]
            items.append(
                {
                    "step": index,
                    "type": action.kind,
                    "action_id": item,
                    "cost": action.cost,
                }
            )
    return items


def _final_reports(case: Case, state: RuntimeState) -> list[dict]:
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


def _stats_json(stats: SearchStats) -> dict:
    from .recovery import cache_stats

    data = {
        "states_evaluated": stats.states_evaluated,
        "states_enqueued": stats.states_enqueued,
        "transitions_rejected_prefix": stats.transitions_rejected_prefix,
        "transitions_rejected_window": stats.transitions_rejected_window,
        "states_pruned_dominated": stats.states_pruned_dominated,
    }
    if stats.cache is not None:
        data["double_recovery_cache"] = cache_stats(stats.cache)
    return data
