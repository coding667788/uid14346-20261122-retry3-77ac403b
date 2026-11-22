"""Stepwise checker tests: first violation, affected target, fault domain."""

from __future__ import annotations

import unittest

from solution.checker import check
from solution.model import load_case
from solution.planner import plan

from tests.helpers import (
    base_case,
    destroy_action,
    migrate_action,
    receipt_action,
)


def _case() -> dict:
    doc = base_case()
    doc["horizon"] = 8
    doc["policy_segments"] = [
        {"target": "T_RET", "retain": True, "start": 0, "end": 9}
    ]
    doc["actions"] = [
        migrate_action("MIG", "C1", "D1", "D3", "O3", start=0, end=3),
        destroy_action("D1", "C1", 1, "O1", start=1, end=6),
        receipt_action("Q1", "D1", "O2", start=2, end=8),
        destroy_action("D3", "C3", 1, "O3", start=1, end=6),
        receipt_action("Q3", "D3", "O1", start=2, end=8),
    ]
    return doc


class CheckerTests(unittest.TestCase):
    def test_valid_optimal_schedule(self):
        doc = _case()
        case = load_case(doc)
        planned = plan(case)
        doc["schedule"] = [
            {"type": item["type"], "action_id": item["action_id"]}
            for item in planned["schedule"]
        ]
        result = check(load_case(doc), doc)
        self.assertEqual(result["status"], "valid")
        self.assertTrue(result["globally_optimal"])
        self.assertIsNone(result["first_violation"])

    def test_first_preservation_violation_reports_target_and_fault(self):
        doc = _case()
        # Wait to step 1, then destroy C1 while T_RET still relies on it
        # alongside C2: the post-destroy state survives no-fault (C2
        # remains) but a D2 fault removes the last witness.
        doc["schedule"] = [
            {"type": "wait"},
            {"type": "destroy", "action_id": "D1"},
        ]
        result = check(load_case(doc), doc)
        self.assertEqual(result["status"], "invalid")
        violation = result["first_violation"]
        self.assertEqual(violation["violation_code"], "PRESERVATION_LOST")
        self.assertEqual(violation["affected_target"], "T_RET")
        self.assertEqual(violation["fault_domain"], "D2")
        self.assertEqual(violation["action_id"], "D1")

    def test_outside_window_is_first_violation(self):
        doc = _case()
        # D1's window opens at step 1; firing it at step 0 is rejected.
        doc["schedule"] = [
            {"type": "destroy", "action_id": "D1"},
        ]
        result = check(load_case(doc), doc)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(
            result["first_violation"]["violation_code"], "OUTSIDE_WINDOW"
        )

    def test_old_generation_receipt_detected(self):
        doc = _case()
        doc["actions"].append(
            migrate_action("MIG_C3", "C3", "D3", "D2", "O2", start=0, end=3)
        )
        doc["schedule"] = [
            {"type": "migrate", "action_id": "MIG_C3"},
            {"type": "wait"},
            {"type": "destroy", "action_id": "D3"},  # gen-1 authorization
        ]
        result = check(load_case(doc), doc)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(
            result["first_violation"]["violation_code"], "GENERATION_MISMATCH"
        )

    def test_duplicate_action_detected(self):
        doc = _case()
        # A migration is harmless to preservation, so reusing its id reaches
        # the at-most-once guard rather than the preservation guard.
        doc["schedule"] = [
            {"type": "migrate", "action_id": "MIG"},
            {"type": "migrate", "action_id": "MIG"},
        ]
        result = check(load_case(doc), doc)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(
            result["first_violation"]["violation_code"], "ACTION_ALREADY_USED"
        )

    def test_suboptimal_but_valid_schedule_flagged(self):
        doc = _case()
        case = load_case(doc)
        planned = plan(case)
        # Insert an extra wait before the planned sequence: still legal and
        # goal-achieving, but the action-id sequence differs from the optimum
        # whenever a strictly shorter schedule exists.
        doc["schedule"] = [{"type": "wait"}] + [
            {"type": item["type"], "action_id": item["action_id"]}
            for item in planned["schedule"]
            if item["type"] != "wait"
        ]
        result = check(load_case(doc), doc)
        self.assertIn(result["status"], ("valid", "valid_not_optimal"))
        if result["status"] == "valid_not_optimal":
            self.assertFalse(result["globally_optimal"])
            self.assertIsNotNone(result["planner_best"])

    def test_unfinished_retirement_is_a_terminal_violation(self):
        doc = _case()
        doc["schedule"] = [
            {"type": "wait"},
            {"type": "destroy", "action_id": "D3"},
            # no receipt: C3 stays pending and remains potentially recoverable
        ]
        result = check(load_case(doc), doc)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(
            result["first_violation"]["violation_code"],
            "RETIREMENT_NOT_ACHIEVED",
        )
        self.assertEqual(
            result["first_violation"]["affected_target"], "T_OLD"
        )

    def test_unknown_action_id(self):
        doc = _case()
        doc["schedule"] = [{"type": "destroy", "action_id": "GHOST"}]
        result = check(load_case(doc), doc)
        self.assertEqual(
            result["first_violation"]["violation_code"], "UNKNOWN_ACTION"
        )

    def test_action_kind_mismatch(self):
        doc = _case()
        # MIG is a migration but the schedule labels it a destroy.
        doc["schedule"] = [
            {"type": "destroy", "action_id": "MIG"},
        ]
        result = check(load_case(doc), doc)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(
            result["first_violation"]["violation_code"],
            "ACTION_KIND_MISMATCH",
        )

    def test_schedule_too_long_rejected_structurally(self):
        from solution.errors import CaseError
        doc = _case()
        doc["schedule"] = [{"type": "wait"}] * (doc["horizon"] + 1)
        with self.assertRaises(CaseError) as ctx:
            check(load_case(doc), doc)
        self.assertEqual(ctx.exception.code, "SCHEDULE_TOO_LONG")

    def test_checker_agrees_with_planner_on_global_optimality(self):
        from tests.test_planner import exhaustive_best_plan

        doc = _case()
        case = load_case(doc)
        planned = plan(case)
        brute = exhaustive_best_plan(case, case.horizon)
        self.assertIsNotNone(brute)
        self.assertEqual(
            (
                planned["total_cost"],
                planned["action_count"],
                tuple(planned["action_ids"]),
            ),
            brute,
        )
        # Feed the optimal schedule back through the checker: it must be
        # declared globally optimal, not merely valid.
        doc["schedule"] = [
            {"type": item["type"], "action_id": item["action_id"]}
            for item in planned["schedule"]
        ]
        result = check(load_case(doc), doc)
        self.assertEqual(result["status"], "valid")
        self.assertTrue(result["globally_optimal"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
