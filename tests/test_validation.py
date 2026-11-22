"""Structural validation, error codes and field locations."""

from __future__ import annotations

import unittest

from solution.errors import CaseError
from solution.model import load_case

from tests.helpers import base_case


class StructuralValidationTests(unittest.TestCase):
    def test_missing_field_reports_location(self):
        doc = base_case()
        del doc["horizon"]
        with self.assertRaises(CaseError) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, "MISSING_FIELD")
        self.assertEqual(ctx.exception.field, "horizon")

    def test_wrong_type_reports_location(self):
        doc = base_case(horizon="6")
        with self.assertRaises(CaseError) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, "TYPE_MISMATCH")
        self.assertEqual(ctx.exception.field, "horizon")

    def test_duplicate_id(self):
        doc = base_case()
        doc["nodes"].append(dict(doc["nodes"][0]))
        with self.assertRaises(CaseError) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")

    def test_unknown_reference(self):
        doc = base_case()
        doc["nodes"][3]["recipes"][0]["children"] = ["L1", "GHOST"]
        with self.assertRaises(CaseError) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, "UNKNOWN_REFERENCE")

    def test_cycle_is_rejected(self):
        doc = base_case()
        doc["nodes"].append(
            {
                "id": "CYCLE",
                "kind": "internal",
                "recipes": [
                    {"id": "C1", "children": ["T_RET"], "distinct_domains": 0}
                ],
            }
        )
        doc["nodes"][3]["recipes"].append(
            {"id": "BACK", "children": ["CYCLE"], "distinct_domains": 0}
        )
        with self.assertRaises(CaseError) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, "CYCLE_DETECTED")

    def test_leaf_credential_must_be_declared(self):
        doc = base_case()
        doc["nodes"][0]["credential"] = "GHOST_CRED"
        with self.assertRaises(CaseError) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, "CREDENTIAL_UNDECLARED")

    def test_unregistered_credential_declaration_is_accepted(self):
        doc = base_case()
        doc["nodes"].append(
            {"id": "L_GHOST", "kind": "leaf", "credential": "C_GHOST"}
        )
        doc["nodes"].append(
            {
                "id": "T_GHOST",
                "kind": "internal",
                "recipes": [
                    {"id": "G", "children": ["L_GHOST"], "distinct_domains": 1}
                ],
            }
        )
        doc["credentials"].append({"id": "C_GHOST", "registered": False})
        doc["objectives"].append({"target": "T_GHOST", "goal": "retire"})
        case = load_case(doc)
        self.assertNotIn("C_GHOST", case.initial_creds)

    def test_threshold_above_leaf_count_rejected(self):
        doc = base_case()
        doc["nodes"][4]["recipes"][0]["distinct_domains"] = 9
        with self.assertRaises(CaseError) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, "THRESHOLD_UNREACHABLE")

    def test_receipt_confirmer_must_differ_from_custodian(self):
        doc = base_case()
        doc["actions"] = [
            {
                "id": "D1", "kind": "destroy", "credential": "C3",
                "generation": 1, "custodian": "O3",
                "window_start": 1, "window_end": 7, "cost": 1,
            },
            {
                "id": "Q1", "kind": "receipt", "destroy_action": "D1",
                "confirmer": "O3",
                "window_start": 1, "window_end": 7, "cost": 1,
            },
        ]
        with self.assertRaises(CaseError) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, "CONFIRMER_SAME_ORG")

    def test_migration_custodian_must_match_target_domain_org(self):
        doc = base_case()
        doc["actions"] = [
            {
                "id": "M1", "kind": "migrate", "credential": "C1",
                "from_domain": "D1", "to_domain": "D2", "custodian": "O1",
                "window_start": 1, "window_end": 7, "cost": 1,
            }
        ]
        with self.assertRaises(CaseError) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, "CUSTODIAN_MISMATCH")

    def test_overlapping_policy_segments_rejected(self):
        doc = base_case()
        doc["policy_segments"] = [
            {"target": "T_RET", "retain": True, "start": 0, "end": 4},
            {"target": "T_RET", "retain": False, "start": 3, "end": 7},
        ]
        with self.assertRaises(CaseError) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, "POLICY_OVERLAP")

    def test_limits_enforced(self):
        doc = base_case()
        doc["horizon"] = 99
        with self.assertRaises(CaseError) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, "VALUE_OUT_OF_RANGE")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
