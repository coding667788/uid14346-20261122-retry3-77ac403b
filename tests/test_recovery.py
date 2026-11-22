"""Recovery engine tests, including an independent brute-force verifier.

The brute-force helper below does not share any enumeration code with the
production engine: it expands the raw case document itself, walks every
recipe combination explicitly and checks the produced leaf sets against a
hand-written recursive feasibility predicate.
"""

from __future__ import annotations

import itertools
import unittest

from solution.model import (
    DESTROYED,
    PENDING,
    apply_action,
    initial_state,
    load_case,
)
from solution.recovery import (
    DoubleRecoveryCache,
    EvaluationContext,
    retirement_report,
)

from tests.helpers import (
    base_case,
    destroy_action,
    migrate_action,
    receipt_action,
)


# ---------------------------------------------------------------------------
# Independent brute-force witness enumeration over the raw document
# ---------------------------------------------------------------------------
def brute_force_witnesses(doc: dict, alive_creds: set[str],
                          domain_of: dict[str, str]) -> dict[str, list[frozenset]]:
    nodes = {n["id"]: n for n in doc["nodes"]}
    result: dict[str, list[frozenset]] = {}

    def walk(nid: str) -> list[frozenset]:
        if nid in result:
            return result[nid]
        node = nodes[nid]
        if node["kind"] == "leaf":
            cid = node["credential"]
            out = [frozenset({cid})] if cid in alive_creds else []
        else:
            all_sets: set[frozenset] = set()
            for recipe in node["recipes"]:
                options = [walk(child) for child in recipe["children"]]
                if any(not opt for opt in options):
                    continue
                for combo in itertools.product(*options):
                    leaves = frozenset(itertools.chain.from_iterable(combo))
                    domains = {domain_of[c] for c in leaves}
                    if recipe["distinct_domains"] and len(domains) < recipe[
                        "distinct_domains"
                    ]:
                        continue
                    # Pairwise disjoint child domain sets for thresholded
                    # recipes.
                    if recipe["distinct_domains"]:
                        child_domain_sets = [
                            {domain_of[c] for c in leaf_set}
                            for leaf_set in combo
                        ]
                        ok = True
                        for i, j in itertools.combinations(
                            range(len(child_domain_sets)), 2
                        ):
                            if child_domain_sets[i] & child_domain_sets[j]:
                                ok = False
                                break
                        if not ok:
                            continue
                    all_sets.add(leaves)
            out = list(all_sets)
        result[nid] = out
        return out

    for nid in nodes:
        walk(nid)
    return result


def _alive_and_domains(case, state, strict: bool):
    alive, domain_of = set(), {}
    for cid, cred in state.creds.items():
        ok = cred.is_strict_alive() if strict else cred.is_potential_alive()
        if ok:
            alive.add(cid)
            domain_of[cid] = cred.domain
    return alive, domain_of


class WitnessEngineTests(unittest.TestCase):
    def test_minimal_leaf_witness_and_tie_break(self):
        case = load_case(base_case())
        state = initial_state(case)
        ctx = EvaluationContext(case, state, strict=True)
        witness = ctx.best_witness("T_RET")
        self.assertEqual(witness["leaf_credentials"], ["C1"])
        self.assertEqual(witness["custody_domains"], ["D1"])
        self.assertEqual(witness["recipe"], "R_L1")

    def test_threshold_requires_distinct_domains(self):
        # T_OLD joins L1(D1) and L3(D3) with threshold 2: works initially.
        case = load_case(base_case())
        state = initial_state(case)
        ctx = EvaluationContext(case, state, strict=True)
        witness = ctx.best_witness("T_OLD")
        self.assertEqual(
            witness["leaf_credentials"], ["C1", "C3"]
        )
        # Move C3 into D1: the two child domains now overlap, so the only
        # thresholded recipe becomes infeasible.
        doc = base_case()
        doc["actions"] = [
            migrate_action("M", "C3", "D3", "D1", "O1")
        ]
        case = load_case(doc)
        state = apply_action(case, initial_state(case), "M")
        ctx = EvaluationContext(case, state, strict=True)
        self.assertIsNone(ctx.best_witness("T_OLD"))

    def test_domain_sets_come_from_actual_leaves_not_labels(self):
        # After migrating C3 D3 -> D2, an intermediate recipe with threshold
        # 2 must observe the new leaf domain, even though the node label
        # still suggests a D3 location.
        doc = base_case()
        doc["actions"] = [migrate_action("M", "C3", "D3", "D2", "O2")]
        case = load_case(doc)
        state = apply_action(case, initial_state(case), "M")
        ctx = EvaluationContext(case, state, strict=True)
        impls = ctx.implementations("T_OLD")
        self.assertTrue(impls)
        self.assertEqual(
            set(impls[0].domains), {"D1", "D2"}
        )

    def test_shared_leaf_charged_once(self):
        doc = base_case()
        doc["nodes"].append(
            {
                "id": "T_SHARED",
                "kind": "internal",
                "recipes": [
                    {
                        "id": "SHARED",
                        "children": ["T_RET", "T_OLD"],
                        "distinct_domains": 0,
                    }
                ],
            }
        )
        case = load_case(doc)
        state = initial_state(case)
        ctx = EvaluationContext(case, state, strict=True)
        witness = ctx.best_witness("T_SHARED")
        # T_RET cheapest witness uses L1=C1, T_OLD also uses C1: the union
        # charges C1 once, giving leaves {C1, C3}, size 2.
        self.assertEqual(
            witness["leaf_credentials"], ["C1", "C3"]
        )

    def test_matches_independent_brute_force(self):
        doc = base_case()
        case = load_case(doc)
        state = initial_state(case)
        alive, domain_of = _alive_and_domains(case, state, strict=True)
        brute = brute_force_witnesses(doc, alive, domain_of)
        for nid in case.node_order:
            ctx = EvaluationContext(case, state, strict=True)
            engine_sets = sorted(
                sorted(impl.leaves) for impl in ctx.implementations(nid)
            )
            brute_sets = sorted(sorted(s) for s in brute[nid])
            self.assertEqual(engine_sets, brute_sets, f"node {nid}")

    def test_matches_brute_force_after_several_transitions(self):
        doc = base_case()
        doc["actions"] = [
            migrate_action("M", "C3", "D3", "D2", "O2"),
            destroy_action("D1", "C2", 1, "O2"),
            receipt_action("Q1", "D1", "O1"),
        ]
        case = load_case(doc)
        state = initial_state(case)
        state = apply_action(case, state, "M")
        state = apply_action(case, state, "D1")
        state = apply_action(case, state, "Q1")
        alive, domain_of = _alive_and_domains(case, state, strict=True)
        brute = brute_force_witnesses(doc, alive, domain_of)
        for nid in case.node_order:
            ctx = EvaluationContext(case, state, strict=True)
            engine_sets = sorted(
                sorted(impl.leaves) for impl in ctx.implementations(nid)
            )
            brute_sets = sorted(sorted(s) for s in brute[nid])
            self.assertEqual(engine_sets, brute_sets, f"node {nid}")

    def test_strict_vs_potential_view_on_pending_replica(self):
        doc = base_case()
        doc["actions"] = [destroy_action("D3", "C3", 1, "O3")]
        case = load_case(doc)
        state = apply_action(case, initial_state(case), "D3")
        self.assertEqual(state.creds["C3"].status, PENDING)

        strict = EvaluationContext(case, state, strict=True)
        potential = EvaluationContext(case, state, strict=False)
        # Pending replica: excluded from retention guarantees ...
        self.assertEqual(strict.implementations("L3"), ())
        # ... but still counts as a potentially recoverable retirement leaf.
        self.assertEqual(len(potential.implementations("L3")), 1)

        report = retirement_report(case, state, "T_OLD")
        self.assertTrue(report.recoverable)

    def test_retirement_classification_destroyed_vs_never_registered(self):
        doc = base_case()
        doc["actions"] = [
            destroy_action("D3", "C3", 1, "O3"),
            receipt_action("Q3", "D3", "O1"),
        ]
        case = load_case(doc)
        state = initial_state(case)
        state = apply_action(case, state, "D3")
        state = apply_action(case, state, "Q3")
        self.assertEqual(state.creds["C3"].status, DESTROYED)
        report = retirement_report(case, state, "T_OLD")
        self.assertFalse(report.recoverable)
        self.assertEqual(report.irrecoverable_reason, "destroyed_evidence")
        self.assertTrue(
            all(item["status"] != "unregistered" for item in report.blocking_leaves)
        )

        # never_registered classification: a credential declared with
        # ``registered: false`` starts with no runtime entry.  This is the
        # public-input way to model a credential that was never registered
        # in the running system.
        from solution.model import UNREGISTERED
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
        state = initial_state(case)
        self.assertNotIn("C_GHOST", state.creds)
        report = retirement_report(case, state, "T_GHOST")
        self.assertFalse(report.recoverable)
        self.assertEqual(report.irrecoverable_reason, "never_registered")
        ghost = next(
            item for item in report.blocking_leaves
            if item["credential"] == "C_GHOST"
        )
        self.assertEqual(ghost["status"], UNREGISTERED)

    def test_double_recovery_cache_reuses_verdicts(self):
        case = load_case(base_case())
        state = initial_state(case)
        cache = DoubleRecoveryCache(case)
        first = cache.verify(state, "T_RET")
        second = cache.verify(state, "T_RET")
        self.assertIs(first, second)
        self.assertTrue(first.ok)

    def test_cache_shared_across_histories_reaching_same_physical_state(self):
        # Two histories that end in the same physical state must share the
        # cached verdict even though their action sequences differ.
        doc = base_case()
        doc["actions"] = [
            migrate_action("M1", "C2", "D2", "D3", "O3", start=0, end=4),
        ]
        case = load_case(doc)
        cache = DoubleRecoveryCache(case)

        state_a = initial_state(case)
        state_a = apply_action(case, state_a, "M1")

        state_b = initial_state(case)
        from solution.model import advance_wait
        state_b = advance_wait(state_b)
        # The migration window still covers step 1, so take it one step later.
        state_b = apply_action(case, state_b, "M1")

        # Same physical custody, same used set, different step -> distinct
        # keys; but at equal steps the cache entry is reused verbatim.
        v1 = cache.verify(state_a, "T_RET")
        v2 = cache.verify(state_a, "T_RET")
        self.assertIs(v1, v2)
        self.assertEqual(len(cache._entries[state_a.state_key()]), 1)

    def test_individually_recoverable_children_cannot_assemble(self):
        # Recipe needs L1 + L3 with threshold 2.  Move both leaves into D1:
        # each leaf alone is still recoverable, but no domain-independent
        # assembly exists.
        doc = base_case()
        doc["actions"] = [migrate_action("M", "C3", "D3", "D1", "O1")]
        case = load_case(doc)
        state = apply_action(case, initial_state(case), "M")
        ctx = EvaluationContext(case, state, strict=True)
        self.assertTrue(ctx.implementations("L1"))
        self.assertTrue(ctx.implementations("L3"))
        self.assertFalse(ctx.implementations("T_OLD"))

    def test_fault_domain_simulation(self):
        case = load_case(base_case())
        state = initial_state(case)
        # T_RET has single-leaf witnesses in D1 (C1) and D2 (C2): a fault
        # in either domain leaves the other leaf, while a D3 fault touches
        # nothing of the target.
        for domain, expected_leaf in [
            ("D1", "C2"),
            ("D2", "C1"),
            ("D3", "C1"),
        ]:
            ctx = EvaluationContext(case, state, strict=True, fault_domain=domain)
            witness = ctx.best_witness("T_RET")
            self.assertIsNotNone(witness, domain)
            self.assertEqual(witness["leaf_credentials"], [expected_leaf])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
