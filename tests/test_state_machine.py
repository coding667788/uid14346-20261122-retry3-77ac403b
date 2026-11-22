"""Credential custody state machine: destroy / migrate / receipt rules."""

from __future__ import annotations

import unittest

from solution.errors import CaseError
from solution.model import (
    DESTROYED,
    PENDING,
    USABLE,
    advance_wait,
    apply_action,
    initial_state,
    load_case,
)

from tests.helpers import (
    base_case,
    destroy_action,
    migrate_action,
    receipt_action,
)


class StateMachineTests(unittest.TestCase):
    def _case_with_lifecycle(self) -> dict:
        doc = base_case()
        doc["actions"] = [
            destroy_action("DESTROY_C3", "C3", 1, "O3"),
            receipt_action("RECEIPT_C3", "DESTROY_C3", "O1"),
            migrate_action("MIG_C1_D1_D2", "C1", "D1", "D2", "O2"),
            destroy_action("DESTROY_C3_GEN2", "C3", 2, "O2"),
        ]
        return doc

    def test_destroy_then_receipt_lifecycle(self):
        case = load_case(self._case_with_lifecycle())
        state = initial_state(case)
        state = apply_action(case, state, "DESTROY_C3")
        self.assertEqual(state.creds["C3"].status, PENDING)
        self.assertEqual(state.creds["C3"].pending_action, "DESTROY_C3")

        state = apply_action(case, state, "RECEIPT_C3")
        cred = state.creds["C3"]
        self.assertEqual(cred.status, DESTROYED)
        self.assertIsNotNone(cred.evidence)
        self.assertEqual(cred.evidence["confirmer"], "O1")

    def test_pending_replica_cannot_migrate_or_be_destroyed_again(self):
        doc = self._case_with_lifecycle()
        doc["actions"].append(
            migrate_action("MIG_C3", "C3", "D3", "D2", "O2")
        )
        case = load_case(doc)
        state = initial_state(case)
        state = apply_action(case, state, "DESTROY_C3")
        # Reusing the same destroy is rejected by the at-most-once rule.
        with self.assertRaises(CaseError) as ctx:
            apply_action(case, state, "DESTROY_C3")
        self.assertEqual(ctx.exception.code, "ACTION_ALREADY_USED")
        # The pending replica cannot be migrated away.
        with self.assertRaises(CaseError) as ctx:
            apply_action(case, state, "MIG_C3")
        self.assertEqual(ctx.exception.code, "NOT_USABLE")

    def test_duplicate_receipt_rejected_after_confirmation(self):
        case = load_case(self._case_with_lifecycle())
        state = initial_state(case)
        state = apply_action(case, state, "DESTROY_C3")
        state = apply_action(case, state, "RECEIPT_C3")
        with self.assertRaises(CaseError) as ctx:
            apply_action(case, state, "RECEIPT_C3")
        self.assertEqual(ctx.exception.code, "ACTION_ALREADY_USED")

    def test_migration_is_atomic_and_bumps_generation(self):
        case = load_case(self._case_with_lifecycle())
        state = initial_state(case)
        state = apply_action(case, state, "MIG_C1_D1_D2")
        cred = state.creds["C1"]
        self.assertEqual(cred.status, USABLE)
        self.assertEqual(cred.generation, 2)
        self.assertEqual(cred.domain, "D2")
        self.assertEqual(cred.custodian, "O2")

    def test_only_usable_replica_may_migrate(self):
        doc = self._case_with_lifecycle()
        doc["actions"].append(
            migrate_action("MIG_C3", "C3", "D3", "D2", "O2")
        )
        case = load_case(doc)
        state = initial_state(case)
        state = apply_action(case, state, "DESTROY_C3")
        with self.assertRaises(CaseError) as ctx:
            apply_action(case, state, "MIG_C3")
        self.assertEqual(ctx.exception.code, "NOT_USABLE")

    def test_old_generation_destroy_rejected_after_successor_created(self):
        # Migrate first (gen-1 dies, gen-2 becomes current), then attempt
        # the gen-1 destroy authorization: the stale generation cannot bind
        # to the live successor.
        doc = self._case_with_lifecycle()
        doc["actions"] = [
            migrate_action("MIG_C3", "C3", "D3", "D2", "O2"),
            destroy_action("DESTROY_C3_OLD", "C3", 1, "O2"),
        ]
        case = load_case(doc)
        state = initial_state(case)
        state = apply_action(case, state, "MIG_C3")
        self.assertEqual(state.creds["C3"].generation, 2)
        with self.assertRaises(CaseError) as ctx:
            apply_action(case, state, "DESTROY_C3_OLD")
        self.assertEqual(ctx.exception.code, "GENERATION_MISMATCH")

    def test_window_is_half_open(self):
        doc = base_case()
        doc["horizon"] = 3
        doc["policy_segments"] = [
            {"target": "T_RET", "retain": True, "start": 0, "end": 4}
        ]
        doc["actions"] = [
            destroy_action("D", "C3", 1, "O3", start=2, end=3),
        ]
        case = load_case(doc)
        state = initial_state(case)
        state = advance_wait(state)  # step 1: still closed
        with self.assertRaises(CaseError) as ctx:
            apply_action(case, state, "D")
        self.assertEqual(ctx.exception.code, "OUTSIDE_WINDOW")
        state = advance_wait(state)  # step 2: inside [2,3)
        state = apply_action(case, state, "D")
        self.assertEqual(state.creds["C3"].status, PENDING)
        # The action is now spent; reuse is rejected by the at-most-once
        # rule regardless of window position.
        state = advance_wait(state)  # step 3: window closed (right-open)
        with self.assertRaises(CaseError) as ctx:
            apply_action(case, state, "D")
        self.assertEqual(ctx.exception.code, "ACTION_ALREADY_USED")

    def test_action_at_exact_right_boundary_rejected(self):
        doc = base_case()
        doc["horizon"] = 4
        doc["policy_segments"] = [
            {"target": "T_RET", "retain": True, "start": 0, "end": 5}
        ]
        doc["actions"] = [
            destroy_action("D", "C3", 1, "O3", start=1, end=3),
            destroy_action("D2", "C2", 1, "O2", start=1, end=3),
        ]
        case = load_case(doc)
        state = initial_state(case)
        for _ in range(3):
            state = advance_wait(state)
        self.assertEqual(state.step, 3)
        with self.assertRaises(CaseError) as ctx:
            apply_action(case, state, "D2")
        self.assertEqual(ctx.exception.code, "OUTSIDE_WINDOW")


    def test_receipt_for_stale_destruction_cannot_remove_successor(self):
        # Full old-generation receipt scenario:
        #   1. destroy gen-1 (pending, awaiting its receipt)
        #   2. a migration is impossible while pending -- so instead model
        #      the re-issued credential by migrating *before* destruction:
        #      gen-1 dies in the atomic move, gen-2 becomes current
        #   3. the gen-1 destroy authorization no longer binds, and a
        #      receipt addressed at it can never remove the gen-2 successor
        doc = self._case_with_lifecycle()
        doc["actions"] = [
            migrate_action("MIG_C3", "C3", "D3", "D2", "O2"),
            destroy_action("D_OLD", "C3", 1, "O2"),
            receipt_action("Q_OLD", "D_OLD", "O1"),
        ]
        case = load_case(doc)
        state = initial_state(case)
        state = apply_action(case, state, "MIG_C3")
        self.assertEqual(state.creds["C3"].generation, 2)
        with self.assertRaises(CaseError) as ctx:
            apply_action(case, state, "D_OLD")
        self.assertEqual(ctx.exception.code, "GENERATION_MISMATCH")
        # The receipt has no matching pending destruction; the live
        # successor means the old-generation receipt is refused outright.
        with self.assertRaises(CaseError) as ctx:
            apply_action(case, state, "Q_OLD")
        self.assertEqual(ctx.exception.code, "RECEIPT_OLD_GENERATION")
        # The successor remains usable and therefore recoverable.
        self.assertEqual(state.creds["C3"].status, USABLE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
