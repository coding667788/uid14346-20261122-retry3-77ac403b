"""Planner tests plus an independent exhaustive search of a small case."""

from __future__ import annotations

import unittest

from solution.errors import CaseError
from solution.model import (
    advance_wait,
    apply_action,
    initial_state,
    load_case,
)
from solution.planner import plan
from solution.recovery import DoubleRecoveryCache, retirement_report

from tests.helpers import (
    base_case,
    destroy_action,
    migrate_action,
    receipt_action,
)


def exhaustive_best_plan(case, max_depth: int):
    """Independent brute-force planner over all action/order/wait sequences.

    Enumerates every ordering of every action subset, inserts arbitrary
    waits and re-applies the real transition rules; returns the best
    ``(cost, action_count, action_ids)`` tuple or ``None``.
    """
    cache = DoubleRecoveryCache(case)
    best = None

    def prefix_valid(state):
        for seg in case.policy_segments:
            if seg.retain and seg.start <= state.step < seg.end:
                if not cache.verify(state, seg.target).ok:
                    return False
        return True

    def goal(state):
        for obj in case.objectives:
            if obj.goal == "retire" and retirement_report(
                case, state, obj.target
            ).recoverable:
                return False
        return True

    def search(state, used, cost, ids):
        nonlocal best
        if goal(state):
            key = (cost, len(ids), tuple(ids))
            if best is None or key < best:
                best = key
        if state.step >= case.horizon:
            return
        for aid, action in sorted(case.actions.items()):
            if aid in used:
                continue
            if not (action.window_start <= state.step < action.window_end):
                continue
            try:
                nxt = apply_action(case, state, aid)
            except CaseError:
                continue
            if not prefix_valid(nxt):
                continue
            search(nxt, used | {aid}, cost + action.cost, ids + [aid])
        search(advance_wait(state), used, cost, ids)

    start = initial_state(case)
    if prefix_valid(start):
        search(start, frozenset(), 0, [])
    return best


def _retirement_case() -> dict:
    doc = base_case()
    doc["horizon"] = 8
    doc["policy_segments"] = [
        {"target": "T_RET", "retain": True, "start": 0, "end": 9}
    ]
    # T_OLD keeps its AND recipe (C1 + C3, threshold 2).  Destroying C1 is
    # prefix-safe because T_RET can fall back to C2 in D2.  The migration is
    # a harmless alternative branch for the exhaustive search to compare.
    doc["actions"] = [
        migrate_action("MIG", "C2", "D2", "D3", "O3", start=0, end=3, cost=1),
        destroy_action("D1", "C1", 1, "O1", start=0, end=6, cost=2),
        receipt_action("Q1", "D1", "O2", start=1, end=8, cost=1),
        destroy_action("D3", "C3", 1, "O3", start=0, end=6, cost=2),
        receipt_action("Q3", "D3", "O1", start=1, end=8, cost=1),
    ]
    return doc


class PlannerTests(unittest.TestCase):
    def test_optimal_plan_retirement_with_evidence(self):
        case = load_case(_retirement_case())
        result = plan(case)
        self.assertEqual(result["status"], "optimal")
        # The AND recipe makes T_OLD irrecoverable as soon as C3 (its D3
        # ingredient) is destroyed with a confirmed receipt; C1's matching
        # pair is the lexicographically natural completion.  Every executed
        # destroy must carry a cross-organization receipt in the final plan.
        ids = set(result["action_ids"])
        self.assertIn("D3", ids)
        self.assertIn("Q3", ids)
        retire_report = next(r for r in result["reports"] if r["goal"] == "retire")
        self.assertFalse(retire_report["recoverable"])
        self.assertEqual(
            retire_report["irrecoverable_reason"], "destroyed_evidence"
        )
        for leaf in retire_report["blocking_leaves"]:
            if leaf["status"] == "destroyed":
                self.assertIsNotNone(leaf["evidence"])
        retain_report = next(r for r in result["reports"] if r["goal"] == "retain")
        self.assertTrue(retain_report["double_recoverable"])

    def test_matches_independent_exhaustive_search(self):
        case = load_case(_retirement_case())
        result = plan(case)
        brute = exhaustive_best_plan(case, case.horizon)
        self.assertIsNotNone(brute)
        self.assertEqual(
            (result["total_cost"], result["action_count"],
             tuple(result["action_ids"])),
            brute,
        )

    def test_shared_credential_destroy_breaks_retention(self):
        # Relocate C2 into D1: T_RET then has two leaves but both die on a
        # D1 fault, so the initial state already fails double-recovery.
        # This is the "direct destroy would break retention" guard detected
        # before any search starts.
        doc = _retirement_case()
        for cred in doc["credentials"]:
            if cred["id"] == "C2":
                cred["domain"] = "D1"
                cred["custodian"] = "O1"
        case = load_case(doc)
        result = plan(case)
        self.assertEqual(result["status"], "infeasible")
        self.assertEqual(result["reason"], "INITIAL_PRESERVATION_FAILURE")
        self.assertEqual(result["detail"]["target"], "T_RET")

    def test_prefix_pruning_blocks_a_destroy_that_kills_last_witness(self):
        # Clean design:
        #   T_RET = C1(D1) OR C2(D2)            -> initially double-recoverable
        #   T_OLD has two alternative recipes, one via C1 and one via C3,
        #   so BOTH credentials must be destroyed before retirement holds.
        # Destroying C1 leaves T_RET with C2 in D2 only: no-fault stays
        # recoverable, but a D2 fault has no witness, so prefix
        # preservation rejects the branch and no feasible plan exists.
        doc = base_case()
        doc["horizon"] = 6
        doc["policy_segments"] = [
            {"target": "T_RET", "retain": True, "start": 0, "end": 7}
        ]
        doc["nodes"][4]["recipes"] = [
            {"id": "VIA_C1", "children": ["L1"], "distinct_domains": 1},
            {"id": "VIA_C3", "children": ["L3"], "distinct_domains": 1},
        ]
        doc["actions"] = [
            destroy_action("D1", "C1", 1, "O1", start=0, end=7),
            receipt_action("Q1", "D1", "O2", start=0, end=7),
            destroy_action("D3", "C3", 1, "O3", start=0, end=7),
            receipt_action("Q3", "D3", "O1", start=0, end=7),
        ]
        case = load_case(doc)
        # Sanity check of the mechanism itself before asserting planner
        # behavior: after destroying C1, only a D2 fault breaks T_RET.
        from solution.recovery import DoubleRecoveryCache
        cache = DoubleRecoveryCache(case)
        probe = apply_action(case, initial_state(case), "D1")
        verdict = cache.verify(probe, "T_RET")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.fault_domain, "D2")

        result = plan(case)
        self.assertEqual(result["status"], "infeasible")
        self.assertEqual(result["reason"], "NO_FEASIBLE_PLAN_WITHIN_HORIZON")
        self.assertGreater(result["stats"]["transitions_rejected_prefix"], 0)

    def test_unconfirmed_destruction_allows_neither_retirement_claim(self):
        # Destroy actions without matching receipt actions: retirement is
        # never achieved because pending leaves still count as potential
        # recovery witnesses.
        doc = base_case()
        doc["horizon"] = 4
        doc["policy_segments"] = [
            {"target": "T_RET", "retain": True, "start": 0, "end": 5}
        ]
        doc["actions"] = [
            destroy_action("D1", "C1", 1, "O1", start=0, end=5),
            destroy_action("D3", "C3", 1, "O3", start=0, end=5),
        ]
        case = load_case(doc)
        result = plan(case)
        self.assertEqual(result["status"], "infeasible")

    def test_migration_preserves_witness_while_shared_domain_is_emptied(self):
        # T_RET has leaves C1(D1) and C2(D2): initially double-recoverable.
        # Retiring T_OLD requires destroying C1 (D1) and C3 (D3).  Destroying
        # C1 straight away is prefix-legal under no fault (C2 remains), but
        # a D2 fault then removes the last witness: prefix preservation
        # rejects it.  Migrating C1 into D3 first keeps surviving witnesses
        # spread across D2 and D3, so the planner can still finish.
        doc = _retirement_case()
        case = load_case(doc)
        result = plan(case)
        self.assertEqual(result["status"], "optimal")
        retain = next(r for r in result["reports"] if r["goal"] == "retain")
        self.assertTrue(retain["double_recoverable"])

        # Explicitly verify the no-migration, immediate-destroy branch is
        # pruned for a D2 fault (the case's own destroy window opens at
        # step 0, so no waiting is needed).
        from solution.recovery import DoubleRecoveryCache
        cache = DoubleRecoveryCache(case)
        state = initial_state(case)
        state = apply_action(case, state, "D1")
        verdict = cache.verify(state, "T_RET")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.fault_domain, "D2")

    def test_initial_preservation_failure_is_reported_not_searched(self):
        # Single witness in D1: a D1 fault can never be survived, so the
        # planner reports the initial failure instead of searching.
        doc = base_case()
        doc["nodes"][3]["recipes"] = [
            {"id": "R_L1", "children": ["L1"], "distinct_domains": 1},
        ]
        doc["objectives"] = [{"target": "T_RET", "goal": "retain"}]
        doc["policy_segments"] = [
            {"target": "T_RET", "retain": True, "start": 0, "end": 7}
        ]
        case = load_case(doc)
        result = plan(case)
        self.assertEqual(result["status"], "infeasible")
        self.assertEqual(result["reason"], "INITIAL_PRESERVATION_FAILURE")
        self.assertEqual(result["detail"]["target"], "T_RET")

    def test_wait_is_available_and_costs_nothing(self):
        doc = _retirement_case()
        # Narrow every window to later steps: the planner must insert
        # waits and still find the optimum.
        for action in doc["actions"]:
            action["window_start"] = 3
            action["window_end"] = 9
        case = load_case(doc)
        result = plan(case)
        self.assertEqual(result["status"], "optimal")
        self.assertTrue(
            any(item["type"] == "wait" for item in result["schedule"])
        )

    def test_cost_then_count_then_id_tie_break(self):
        # Two isolated retire targets, each retired by an identically priced
        # pair; the lexicographically smaller full action-id sequence wins.
        doc = base_case()
        doc["horizon"] = 6
        # Keep T_RET retained; replace T_OLD's AND recipe with two isolated
        # single-leaf retire targets so each needs exactly one pair.
        doc["nodes"].append(
            {"id": "L4", "kind": "leaf", "credential": "C4"}
        )
        doc["nodes"][4]["recipes"] = [
            {"id": "VIA_L3", "children": ["L3"], "distinct_domains": 1},
        ]
        doc["nodes"].append(
            {
                "id": "T_ISO2",
                "kind": "internal",
                "recipes": [
                    {"id": "VIA_L4", "children": ["L4"], "distinct_domains": 1}
                ],
            }
        )
        doc["credentials"].append(
            {"id": "C4", "initial_generation": 1, "domain": "D2", "custodian": "O2"}
        )
        doc["objectives"] = [
            {"target": "T_RET", "goal": "retain"},
            {"target": "T_OLD", "goal": "retire"},
            {"target": "T_ISO2", "goal": "retire"},
        ]
        doc["actions"] = [
            destroy_action("Z_DESTROY", "C3", 1, "O3", start=0, end=7),
            receipt_action("Z_RECEIPT", "Z_DESTROY", "O1", start=0, end=7),
            destroy_action("A_DESTROY", "C4", 1, "O2", start=0, end=7),
            receipt_action("A_RECEIPT", "A_DESTROY", "O1", start=0, end=7),
        ]
        case = load_case(doc)
        result = plan(case)
        self.assertEqual(result["status"], "optimal")
        # Equal costs/counts: the sequence beginning with A_ sorts first.
        self.assertEqual(
            result["action_ids"],
            ["A_DESTROY", "A_RECEIPT", "Z_DESTROY", "Z_RECEIPT"],
        )
        self.assertEqual(result["total_cost"], 6)

    def test_each_action_happens_at_most_once(self):
        doc = _retirement_case()
        case = load_case(doc)
        result = plan(case)
        self.assertEqual(
            len(result["action_ids"]), len(set(result["action_ids"]))
        )
        # The memoized search records dominated-state pruning.
        self.assertGreaterEqual(
            result["stats"]["states_pruned_dominated"], 0
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
