"""Read-only state evaluation (``evaluate``): witnesses under every fault."""

from __future__ import annotations

from .model import Case, initial_state
from .recovery import (
    NO_FAULT,
    DoubleRecoveryCache,
    EvaluationContext,
    retirement_report,
)


def evaluate(case: Case) -> dict:
    state = initial_state(case)
    targets = [obj.target for obj in case.objectives]

    target_reports: list[dict] = []
    for target in targets:
        scenarios: list[dict] = []
        no_fault_ctx = EvaluationContext(case, state, strict=True, fault_domain=NO_FAULT)
        scenarios.append(
            {
                "fault_domain": "none",
                "view": "strict",
                "recoverable": no_fault_ctx.best_witness(target) is not None,
                "minimal_witness": no_fault_ctx.best_witness(target),
            }
        )
        for domain in case.domains:
            ctx = EvaluationContext(case, state, strict=True, fault_domain=domain)
            scenarios.append(
                {
                    "fault_domain": domain,
                    "view": "strict",
                    "recoverable": ctx.best_witness(target) is not None,
                    "minimal_witness": ctx.best_witness(target),
                }
            )
        cache = DoubleRecoveryCache(case, strict=True)
        verdict = cache.verify(state, target)
        target_reports.append(
            {
                "target": target,
                "double_recoverable": verdict.ok,
                "failed_fault_domain": verdict.fault_domain,
                "scenarios": scenarios,
            }
        )

    # Potential-view snapshot used for retirement reasoning at step 0.
    potential: list[dict] = []
    for obj in case.objectives:
        if obj.goal != "retire":
            continue
        report = retirement_report(case, state, obj.target)
        potential.append(
            {
                "target": obj.target,
                "recoverable": report.recoverable,
                "minimal_witness": report.best_witness,
                "irrecoverable_reason": report.irrecoverable_reason,
                "blocking_leaves": report.blocking_leaves,
            }
        )

    return {
        "case_id": case.case_id,
        "step": 0,
        "targets": target_reports,
        "retirement_view_potential": potential,
        "domains": list(case.domains),
    }
