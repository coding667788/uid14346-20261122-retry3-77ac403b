"""Helpers for building small synthetic case documents in tests."""

from __future__ import annotations

import copy
from typing import Any


def base_case(**overrides: Any) -> dict:
    """A tiny but complete case with one retained and one retiring target."""
    doc: dict[str, Any] = {
        "case_id": "unit-case",
        "horizon": 6,
        "domains": ["D1", "D2", "D3"],
        "organizations": ["O1", "O2", "O3"],
        "domain_organization": {"D1": "O1", "D2": "O2", "D3": "O3"},
        "nodes": [
            {"id": "L1", "kind": "leaf", "credential": "C1"},
            {"id": "L2", "kind": "leaf", "credential": "C2"},
            {"id": "L3", "kind": "leaf", "credential": "C3"},
            {
                "id": "T_RET",
                "kind": "internal",
                "recipes": [
                    {"id": "R_L1", "children": ["L1"], "distinct_domains": 1},
                    {"id": "R_L2", "children": ["L2"], "distinct_domains": 1},
                ],
            },
            {
                "id": "T_OLD",
                "kind": "internal",
                "recipes": [
                    {
                        "id": "R_JOIN",
                        "children": ["L1", "L3"],
                        "distinct_domains": 2,
                    }
                ],
            },
        ],
        "credentials": [
            {"id": "C1", "initial_generation": 1, "domain": "D1", "custodian": "O1"},
            {"id": "C2", "initial_generation": 1, "domain": "D2", "custodian": "O2"},
            {"id": "C3", "initial_generation": 1, "domain": "D3", "custodian": "O3"},
        ],
        "actions": [],
        "policy_segments": [
            {"target": "T_RET", "retain": True, "start": 0, "end": 7}
        ],
        "objectives": [
            {"target": "T_RET", "goal": "retain"},
            {"target": "T_OLD", "goal": "retire"},
        ],
    }
    doc.update(overrides)
    return copy.deepcopy(doc)


def destroy_action(aid: str, cid: str, gen: int, org: str,
                   start: int = 0, end: int = 7, cost: int = 2) -> dict:
    return {
        "id": aid, "kind": "destroy", "credential": cid,
        "generation": gen, "custodian": org,
        "window_start": start, "window_end": end, "cost": cost,
    }


def receipt_action(aid: str, destroy_id: str, confirmer: str,
                   start: int = 0, end: int = 7, cost: int = 1) -> dict:
    return {
        "id": aid, "kind": "receipt", "destroy_action": destroy_id,
        "confirmer": confirmer,
        "window_start": start, "window_end": end, "cost": cost,
    }


def migrate_action(aid: str, cid: str, src: str, dst: str, org: str,
                   start: int = 0, end: int = 7, cost: int = 1) -> dict:
    return {
        "id": aid, "kind": "migrate", "credential": cid,
        "from_domain": src, "to_domain": dst, "custodian": org,
        "window_start": start, "window_end": end, "cost": cost,
    }
