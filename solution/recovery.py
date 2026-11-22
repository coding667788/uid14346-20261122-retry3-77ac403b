"""Recovery evaluation over the DAG of abstract custody nodes.

Two availability views are supported on the same engine:

* ``strict``    -- only *usable* current replicas count; used for retention
  guarantees, where a replica awaiting destruction must never count.
* ``potential`` -- usable *and* pending replicas count; used when proving
  that a retired target truly cannot be rebuilt any more, because a pending
  replica is still a potentially recoverable witness until a valid receipt
  lands.

For every node the engine enumerates its concrete *implementations*, each
identified by the set of leaf credentials actually consumed.  Because the
same leaf can be shared by several paths, a shared leaf is charged once:
the union of leaf sets is taken at every merge.  The custody-domain set of
an implementation is the union of the *actual leaf* domains, never the
label of an intermediate node, so migration of a leaf across domains is
seen correctly through arbitrarily many internal layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .errors import CaseError, err_value
from .model import (
    UNREGISTERED,
    Case,
    CredState,
    RuntimeState,
)

# Safety bound on the number of distinct implementations enumerated for a
# single node.  With the mandated problem size (30 nodes, 6 domains) well
# formed cases stay far below it; it exists to fail loudly on pathologically
# combinatorial graphs instead of burning unbounded CPU.
IMPL_CAP = 200_000

NO_FAULT = None  # sentinel for the no-failure scenario


@dataclass(frozen=True)
class Impl:
    """One concrete recovery implementation of a node."""

    leaves: frozenset[str]
    domains: frozenset[str]
    # For internal nodes: recipe id plus the chosen child implementation
    # (node id -> leaf set).  Leaf nodes leave this empty.
    recipe_id: Optional[str] = None
    child_leaf_sets: Optional[tuple[tuple[str, frozenset[str]], ...]] = None


class EvaluationContext:
    """Memoized evaluator for one runtime state and one availability view."""

    def __init__(
        self,
        case: Case,
        state: RuntimeState,
        strict: bool,
        fault_domain: Optional[str] = NO_FAULT,
    ):
        self.case = case
        self.state = state
        self.strict = strict
        self.fault_domain = fault_domain
        self._cache: dict[str, tuple[Impl, ...]] = {}
        self._leaf_domain: dict[str, Optional[str]] = self._build_leaf_domains()

    # -- leaf availability --------------------------------------------------
    def _build_leaf_domains(self) -> dict[str, Optional[str]]:
        out: dict[str, Optional[str]] = {}
        for node_id in self.case.node_order:
            node = self.case.nodes[node_id]
            if not node.is_leaf:
                continue
            cred = self.state.creds.get(node.credential)
            if cred is None:
                out[node_id] = None
                continue
            if self.strict:
                alive = cred.is_strict_alive()
            else:
                alive = cred.is_potential_alive()
            if not alive:
                out[node_id] = None
                continue
            if self.fault_domain is not NO_FAULT and cred.domain == self.fault_domain:
                out[node_id] = None
                continue
            out[node_id] = cred.domain
        return out

    # -- implementation enumeration ----------------------------------------
    def implementations(self, node_id: str) -> tuple[Impl, ...]:
        if node_id in self._cache:
            return self._cache[node_id]
        node = self.case.nodes[node_id]
        if node.is_leaf:
            dom = self._leaf_domain.get(node_id)
            result: tuple[Impl, ...] = (
                (Impl(frozenset({node.credential}), frozenset({dom})),)
                if dom is not None
                else ()
            )
        else:
            result = self._internal_implementations(node)
        if len(result) > IMPL_CAP:
            raise err_value(
                f"node[{node_id}]", "ENUMERATION_LIMIT",
                f"node {node_id} has more than {IMPL_CAP} distinct "
                "implementations; simplify the recovery graph",
            )
        self._cache[node_id] = result
        return result

    def _internal_implementations(self, node) -> tuple[Impl, ...]:
        distinct: dict[frozenset[str], Impl] = {}
        for recipe in node.recipes:
            child_options: list[tuple[str, tuple[Impl, ...]]] = []
            ok = True
            for child in recipe.children:
                opts = self.implementations(child)
                if not opts:
                    ok = False
                    break
                child_options.append((child, opts))
            if not ok:
                continue
            self._combine(recipe, child_options, 0, {}, frozenset(), distinct)
        ordered = sorted(distinct.values(), key=_impl_sort_key)
        return tuple(ordered)

    def _combine(
        self,
        recipe,
        child_options: list[tuple[str, tuple[Impl, ...]]],
        index: int,
        chosen: dict[str, frozenset[str]],
        used_domains: frozenset[str],
        out: dict[frozenset[str], Impl],
    ) -> None:
        if len(out) > IMPL_CAP:
            return
        if index == len(child_options):
            leaves: set[str] = set()
            child_leaf_sets: list[tuple[str, frozenset[str]]] = []
            for child, leaf_set in chosen.items():
                leaves |= leaf_set
                child_leaf_sets.append((child, frozenset(leaf_set)))
            leaf_set = frozenset(leaves)
            domains = self._domains_of(leaf_set)
            if recipe.threshold > 0 and len(domains) < recipe.threshold:
                return
            impl = Impl(
                leaves=leaf_set,
                domains=domains,
                recipe_id=recipe.recipe_id,
                child_leaf_sets=tuple(sorted(child_leaf_sets)),
            )
            # Same leaf set == same implementation regardless of recipe.
            out.setdefault(leaf_set, impl)
            return
        child, options = child_options[index]
        for candidate in options:
            if recipe.threshold > 0 and candidate.domains & used_domains:
                # Domain sets of the specified children must be pairwise
                # disjoint for a thresholded recipe.
                continue
            chosen[child] = candidate.leaves
            self._combine(
                recipe,
                child_options,
                index + 1,
                chosen,
                used_domains | candidate.domains,
                out,
            )
        chosen.pop(child, None)

    def _domains_of(self, leaf_set: frozenset[str]) -> frozenset[str]:
        domains: set[str] = set()
        for cid in leaf_set:
            cred = self.state.creds.get(cid)
            if cred is not None and cred.domain is not None:
                domains.add(cred.domain)
        return frozenset(domains)

    # -- witnesses -----------------------------------------------------------
    def best_witness(self, node_id: str) -> Optional[dict]:
        impls = self.implementations(node_id)
        if not impls:
            return None
        return self.witness_json(node_id, impls[0])

    def witness_json(self, node_id: str, impl: Impl) -> dict:
        node = self.case.nodes[node_id]
        if node.is_leaf:
            return {
                "node": node_id,
                "recipe": None,
                "leaf_credentials": sorted(impl.leaves),
                "custody_domains": sorted(impl.domains),
                "children": [],
            }
        children: list[dict] = []
        for child, leaf_set in impl.child_leaf_sets:
            child_impl = self._impl_for_leaves(child, leaf_set)
            children.append(self.witness_json(child, child_impl))
        return {
            "node": node_id,
            "recipe": impl.recipe_id,
            "leaf_credentials": sorted(impl.leaves),
            "custody_domains": sorted(impl.domains),
            "children": children,
        }

    def _impl_for_leaves(self, node_id: str, leaves: frozenset[str]) -> Impl:
        for impl in self.implementations(node_id):
            if impl.leaves == leaves:
                return impl
        raise CaseError(
            "INTERNAL",
            f"stored implementation of {node_id} vanished from the cache",
        )


def _impl_sort_key(impl: Impl) -> tuple:
    # Primary objective: minimize the number of distinct leaves actually
    # used.  Tie-break: lexicographic leaf id list (fully reproducible).
    return len(impl.leaves), sorted(impl.leaves), sorted(impl.domains)


# ---------------------------------------------------------------------------
# Double-recoverability: no-fault witness plus one witness per fault domain
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PrefixVerdict:
    ok: bool
    step: int
    target: str
    fault_domain: Optional[str]
    witness: Optional[dict]
    no_fault_witness: Optional[dict] = None


class DoubleRecoveryCache:
    """Caches double-recoverability verdicts keyed by runtime state.

    A cache entry covers one state and one target; the entry itself holds
    both the no-fault result and every single-domain-fault result so the
    planner reuses them across branches that reach the same custody state
    (the same state can be reached with different histories because an
    explicit wait changes nothing physical).
    """

    def __init__(self, case: Case, strict: bool = True):
        self.case = case
        self.strict = strict
        self._entries: dict[tuple, dict[str, PrefixVerdict]] = {}

    def verify(self, state: RuntimeState, target: str) -> PrefixVerdict:
        key = state.state_key()
        entry = self._entries.get(key)
        if entry is None:
            entry = {}
            self._entries[key] = entry
        if target in entry:
            return entry[target]

        no_fault = EvaluationContext(self.case, state, self.strict, NO_FAULT)
        no_fault_witness = no_fault.best_witness(target)
        if no_fault_witness is None:
            verdict = PrefixVerdict(
                False, state.step, target, NO_FAULT, None, None
            )
        else:
            verdict = None
            for domain in self.case.domains:
                ctx = EvaluationContext(self.case, state, self.strict, domain)
                fault_witness = ctx.best_witness(target)
                if fault_witness is None:
                    verdict = PrefixVerdict(
                        False, state.step, target, domain, None,
                        no_fault_witness,
                    )
                    break
            if verdict is None:
                verdict = PrefixVerdict(
                    True, state.step, target, NO_FAULT, no_fault_witness,
                    no_fault_witness,
                )
        entry[target] = verdict
        return verdict


# ---------------------------------------------------------------------------
# Retired-target reporting
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RetirementReport:
    target: str
    recoverable: bool
    best_witness: Optional[dict]
    irrecoverable_reason: Optional[str]
    blocking_leaves: list[dict]


def retirement_report(
    case: Case, state: RuntimeState, target: str
) -> RetirementReport:
    """Final report for a retired target under the *potential* view."""
    ctx = EvaluationContext(case, state, strict=False, fault_domain=NO_FAULT)
    witness = ctx.best_witness(target)
    if witness is not None:
        return RetirementReport(target, True, witness, None, [])

    blocking: list[dict] = []
    for cid in sorted(case.leaves_of[target]):
        cred: Optional[CredState] = state.creds.get(cid)
        if cred is None:
            status = UNREGISTERED
            evidence = None
        else:
            status = cred.status
            evidence = cred.evidence
        blocking.append(
            {
                "credential": cid,
                "status": status,
                "domain": None if cred is None else cred.domain,
                "generation": None if cred is None else cred.generation,
                "evidence": evidence,
            }
        )
    if any(item["status"] == UNREGISTERED for item in blocking):
        reason = "never_registered"
    else:
        reason = "destroyed_evidence"
    return RetirementReport(target, False, None, reason, blocking)


def cache_stats(cache: DoubleRecoveryCache) -> dict:
    """Instrumentation for the cached double-recoverability checks."""
    states = len(cache._entries)
    verdicts = sum(len(entry) for entry in cache._entries.values())
    return {"cached_states": states, "cached_verdicts": verdicts}
