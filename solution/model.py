"""Domain model: case document, custody state machine and authorized actions.

Only *custody state* is ever simulated.  Credentials, keys and shares are
abstract identifiers; nothing here can derive a real secret.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional

from .errors import (
    err_dup,
    err_missing,
    err_ref,
    err_type,
    err_value,
)

# ---------------------------------------------------------------------------
# Limits mandated by the task statement
# ---------------------------------------------------------------------------
MAX_ACTIONS = 20
MAX_STEPS = 12
MAX_NODES = 30
MAX_DOMAINS = 6

# Credential lifecycle states.
USABLE = "usable"        # current replica exists and can serve as a witness
PENDING = "pending"      # destroy command executed, receipt not yet confirmed
DESTROYED = "destroyed"  # valid cross-organisation receipt confirmed
UNREGISTERED = "unregistered"  # no registered replica (leaf not declared)


# ---------------------------------------------------------------------------
# Static case structure
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Recipe:
    recipe_id: str
    children: tuple[str, ...]
    # 0 means the recipe has no domain-independence threshold; otherwise the
    # concrete child witnesses must cover at least ``threshold`` distinct
    # custody domains with pairwise disjoint domain sets.
    threshold: int


@dataclass(frozen=True)
class Node:
    node_id: str
    is_leaf: bool
    credential: Optional[str] = None
    recipes: tuple[Recipe, ...] = ()


@dataclass(frozen=True)
class Action:
    action_id: str
    kind: str  # destroy | migrate | receipt
    window_start: int
    window_end: int
    cost: int
    # destroy
    credential: Optional[str] = None
    generation: Optional[int] = None
    custodian: Optional[str] = None
    # migrate
    from_domain: Optional[str] = None
    to_domain: Optional[str] = None
    # receipt
    destroy_action: Optional[str] = None
    confirmer: Optional[str] = None


@dataclass(frozen=True)
class PolicySegment:
    target: str
    retain: bool
    start: int  # inclusive step
    end: int    # exclusive step


@dataclass(frozen=True)
class Objective:
    target: str
    goal: str  # retain | retire


@dataclass(frozen=True)
class Case:
    case_id: str
    horizon: int
    domains: tuple[str, ...]
    orgs: tuple[str, ...]
    domain_org: dict[str, str]
    nodes: dict[str, Node]
    node_order: tuple[str, ...]
    leaves_of: dict[str, frozenset[str]]  # node -> credential ids under it
    credentials: dict[str, dict[str, Any]]
    actions: dict[str, Action]
    policy_segments: tuple[PolicySegment, ...]
    objectives: tuple[Objective, ...]
    initial_creds: dict[str, "CredState"]


# ---------------------------------------------------------------------------
# Runtime custody state
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CredState:
    status: str
    generation: int = 0
    domain: Optional[str] = None
    custodian: Optional[str] = None
    # id of the destroy action whose command is awaiting confirmation
    pending_action: Optional[str] = None
    # populated once a valid receipt confirms destruction
    evidence: Optional[dict[str, Any]] = None

    def is_strict_alive(self) -> bool:
        """Usable for retention guarantees (pending never counts)."""
        return self.status == USABLE

    def is_potential_alive(self) -> bool:
        """Potentially recoverable for retirement proofs (pending counts)."""
        return self.status in (USABLE, PENDING)

    def key(self) -> tuple:
        return (
            self.status,
            self.generation,
            self.domain,
            self.custodian,
            self.pending_action,
        )


@dataclass(frozen=True)
class RuntimeState:
    step: int
    creds: dict[str, CredState]
    used: frozenset[str]
    history: tuple[dict[str, Any], ...] = ()

    def state_key(self) -> tuple:
        cred_key = tuple(
            sorted((cid, c.key()) for cid, c in self.creds.items())
        )
        return (self.step, cred_key, tuple(sorted(self.used)))


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------
def _require(obj: dict, name: str, field_path: str) -> Any:
    if name not in obj:
        raise err_missing(f"{field_path}.{name}" if field_path else name)
    return obj[name]


def _as_str(value: Any, field_path: str) -> str:
    if not isinstance(value, str) or not value:
        raise err_type(field_path, "non-empty string", value)
    return value


def _as_int(value: Any, field_path: str) -> int:
    # bool is a subclass of int; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise err_type(field_path, "integer", value)
    return value


def _as_list(value: Any, field_path: str) -> list:
    if not isinstance(value, list):
        raise err_type(field_path, "array", value)
    return value


def _as_dict(value: Any, field_path: str) -> dict:
    if not isinstance(value, dict):
        raise err_type(field_path, "object", value)
    return value


def _unique(values: list, field_path: str) -> None:
    seen = set()
    for v in values:
        if v in seen:
            raise err_dup(field_path, v)
        seen.add(v)


# ---------------------------------------------------------------------------
# Case parsing and validation
# ---------------------------------------------------------------------------
def load_case(doc: Any) -> Case:
    root = _as_dict(doc, "")

    case_id = _as_str(_require(root, "case_id", ""), "case_id")
    horizon = _as_int(_require(root, "horizon", ""), "horizon")
    if not 1 <= horizon <= MAX_STEPS:
        raise err_value(
            "horizon",
            "VALUE_OUT_OF_RANGE",
            f"horizon must be in 1..{MAX_STEPS}, got {horizon}",
        )

    domains = tuple(
        _as_str(d, f"domains[{i}]")
        for i, d in enumerate(_as_list(_require(root, "domains", ""), "domains"))
    )
    if not 1 <= len(domains) <= MAX_DOMAINS:
        raise err_value(
            "domains",
            "VALUE_OUT_OF_RANGE",
            f"need 1..{MAX_DOMAINS} domains, got {len(domains)}",
        )
    _unique(list(domains), "domains")

    orgs = tuple(
        _as_str(o, f"organizations[{i}]")
        for i, o in enumerate(
            _as_list(_require(root, "organizations", ""), "organizations")
        )
    )
    if not 1 <= len(orgs) <= MAX_DOMAINS:
        raise err_value(
            "organizations",
            "VALUE_OUT_OF_RANGE",
            f"need 1..{MAX_DOMAINS} organizations, got {len(orgs)}",
        )
    _unique(list(orgs), "organizations")

    raw_map = _as_dict(_require(root, "domain_organization", ""), "domain_organization")
    domain_org: dict[str, str] = {}
    for dom in domains:
        if dom not in raw_map:
            raise err_missing(f"domain_organization.{dom}")
        org = _as_str(raw_map[dom], f"domain_organization.{dom}")
        if org not in orgs:
            raise err_ref(f"domain_organization.{dom}", org)
        domain_org[dom] = org

    nodes, node_order, leaves_of = _load_nodes(root, set(domains))
    credentials, initial_creds = _load_credentials(root, nodes, domain_org)
    actions = _load_actions(root, horizon, set(domains), set(orgs), credentials, domain_org)
    policy_segments = _load_policy(root, horizon, nodes)
    objectives = _load_objectives(root, horizon, nodes, policy_segments)

    return Case(
        case_id=case_id,
        horizon=horizon,
        domains=domains,
        orgs=orgs,
        domain_org=domain_org,
        nodes=nodes,
        node_order=node_order,
        leaves_of=leaves_of,
        credentials=credentials,
        actions=actions,
        policy_segments=policy_segments,
        objectives=objectives,
        initial_creds=initial_creds,
    )


def _load_nodes(root: dict, domains: set[str]) -> tuple[dict, tuple, dict]:
    raw_nodes = _as_list(_require(root, "nodes", ""), "nodes")
    if not 1 <= len(raw_nodes) <= MAX_NODES:
        raise err_value(
            "nodes", "VALUE_OUT_OF_RANGE",
            f"need 1..{MAX_NODES} nodes, got {len(raw_nodes)}",
        )
    nodes: dict[str, Node] = {}
    order: list[str] = []
    for i, raw in enumerate(raw_nodes):
        base = f"nodes[{i}]"
        rn = _as_dict(raw, base)
        nid = _as_str(_require(rn, "id", base), f"{base}.id")
        if nid in nodes:
            raise err_dup(f"{base}.id", nid)
        kind = _as_str(_require(rn, "kind", base), f"{base}.kind")
        if kind == "leaf":
            cred = _as_str(
                _require(rn, "credential", base), f"{base}.credential"
            )
            nodes[nid] = Node(nid, True, credential=cred)
        elif kind == "internal":
            raw_recipes = _as_list(
                _require(rn, "recipes", base), f"{base}.recipes"
            )
            if not raw_recipes:
                raise err_value(
                    f"{base}.recipes", "EMPTY_RECIPE_LIST",
                    "internal node needs at least one recipe",
                )
            recipes: list[Recipe] = []
            seen_recipe: set[str] = set()
            for j, rr in enumerate(raw_recipes):
                rb = f"{base}.recipes[{j}]"
                rdict = _as_dict(rr, rb)
                rid = _as_str(_require(rdict, "id", rb), f"{rb}.id")
                if rid in seen_recipe:
                    raise err_dup(f"{rb}.id", rid)
                seen_recipe.add(rid)
                children = tuple(
                    _as_str(c, f"{rb}.children[{k}]")
                    for k, c in enumerate(
                        _as_list(_require(rdict, "children", rb), f"{rb}.children")
                    )
                )
                if not children:
                    raise err_value(
                        f"{rb}.children", "EMPTY_CHILDREN",
                        "recipe needs at least one child",
                    )
                if len(set(children)) != len(children):
                    raise err_value(
                        f"{rb}.children", "DUPLICATE_CHILD",
                        "recipe children must be distinct",
                    )
                threshold = _as_int(
                    _require(rdict, "distinct_domains", rb),
                    f"{rb}.distinct_domains",
                )
                if threshold < 0:
                    raise err_value(
                        f"{rb}.distinct_domains", "VALUE_OUT_OF_RANGE",
                        "domain threshold must be >= 0 (0 disables it)",
                    )
                recipes.append(Recipe(rid, children, threshold))
            nodes[nid] = Node(nid, False, recipes=tuple(recipes))
        else:
            raise err_value(
                f"{base}.kind", "UNKNOWN_KIND",
                f"node kind must be 'leaf' or 'internal', got {kind!r}",
            )
        order.append(nid)

    # Resolve child references and reject cycles (DAG requirement).
    visiting: set[str] = set()
    done: set[str] = set()
    leaves_of: dict[str, frozenset[str]] = {}

    def visit(nid: str, path: tuple[str, ...]) -> frozenset[str]:
        if nid in done:
            return leaves_of[nid]
        if nid in visiting:
            cycle = " -> ".join(path + (nid,))
            raise err_value(
                "nodes", "CYCLE_DETECTED", f"recovery graph has a cycle: {cycle}"
            )
        visiting.add(nid)
        node = nodes[nid]
        if node.is_leaf:
            result = frozenset({node.credential})
        else:
            acc: set[str] = set()
            for recipe in node.recipes:
                for child in recipe.children:
                    if child not in nodes:
                        raise err_ref(
                            f"node[{nid}].recipe[{recipe.recipe_id}].children",
                            child,
                        )
                    acc |= visit(child, path + (nid,))
            result = frozenset(acc)
        visiting.discard(nid)
        done.add(nid)
        leaves_of[nid] = result
        return result

    for nid in order:
        visit(nid, ())

    # Internal domain thresholds cannot exceed the number of distinct leaf
    # credentials beneath the node.
    for nid in order:
        node = nodes[nid]
        if not node.is_leaf:
            for recipe in node.recipes:
                if recipe.threshold > len(leaves_of[nid]):
                    raise err_value(
                        f"node[{nid}].recipe[{recipe.recipe_id}].distinct_domains",
                        "THRESHOLD_UNREACHABLE",
                        f"threshold {recipe.threshold} exceeds "
                        f"{len(leaves_of[nid])} leaf credentials under {nid}",
                    )
    return nodes, tuple(order), leaves_of


def _load_credentials(
    root: dict, nodes: dict[str, Node], domain_org: dict[str, str]
) -> tuple[dict, dict]:
    raw_creds = _as_list(_require(root, "credentials", ""), "credentials")
    credentials: dict[str, dict] = {}
    initial: dict[str, CredState] = {}
    for i, raw in enumerate(raw_creds):
        base = f"credentials[{i}]"
        rc = _as_dict(raw, base)
        cid = _as_str(_require(rc, "id", base), f"{base}.id")
        if cid in credentials:
            raise err_dup(f"{base}.id", cid)
        # ``registered: false`` declares a credential that has *no* current
        # replica in the running system.  It still resolves leaf references
        # and participates in retirement classification as never_registered.
        registered = rc.get("registered", True)
        if not isinstance(registered, bool):
            raise err_type(f"{base}.registered", "boolean", registered)
        if not registered:
            credentials[cid] = {"registered": False}
            continue
        gen = _as_int(
            _require(rc, "initial_generation", base),
            f"{base}.initial_generation",
        )
        if gen < 1:
            raise err_value(
                f"{base}.initial_generation", "VALUE_OUT_OF_RANGE",
                "initial generation must be >= 1",
            )
        dom = _as_str(_require(rc, "domain", base), f"{base}.domain")
        if dom not in domain_org:
            raise err_ref(f"{base}.domain", dom)
        custodian = _as_str(
            _require(rc, "custodian", base), f"{base}.custodian"
        )
        if custodian != domain_org[dom]:
            raise err_value(
                f"{base}.custodian", "CUSTODIAN_MISMATCH",
                f"custodian of {cid} must be {domain_org[dom]} "
                f"(the organization registered at {dom})",
            )
        credentials[cid] = {
            "registered": True,
            "generation": gen,
            "domain": dom,
            "custodian": custodian,
        }
        initial[cid] = CredState(
            status=USABLE, generation=gen, domain=dom, custodian=custodian
        )

    # Every leaf credential must be declared.  A declaration with
    # ``registered: false`` is legal and means the leaf starts without a
    # replica; declarations must still be attached to a leaf.
    declared = set(credentials)
    used: set[str] = set()
    for nid, node in nodes.items():
        if node.is_leaf:
            if node.credential not in credentials:
                raise err_value(
                    f"node[{nid}].credential", "CREDENTIAL_UNDECLARED",
                    f"leaf {nid} names credential {node.credential!r} "
                    "which is absent from the credentials array",
                )
            used.add(node.credential)
    stray = declared - used
    if stray:
        raise err_value(
            "credentials", "DECLARED_UNUSED",
            f"declared credentials not attached to any leaf: "
            f"{sorted(stray)}",
        )
    return credentials, initial


def _load_actions(
    root: dict,
    horizon: int,
    domains: set[str],
    orgs: set[str],
    credentials: dict[str, dict],
    domain_org: dict[str, str],
) -> dict[str, Action]:
    raw_actions = _as_list(_require(root, "actions", ""), "actions")
    if len(raw_actions) > MAX_ACTIONS:
        raise err_value(
            "actions", "VALUE_OUT_OF_RANGE",
            f"at most {MAX_ACTIONS} actions allowed, got {len(raw_actions)}",
        )
    actions: dict[str, Action] = {}
    destroy_custodian: dict[str, str] = {}
    for i, raw in enumerate(raw_actions):
        base = f"actions[{i}]"
        ra = _as_dict(raw, base)
        aid = _as_str(_require(ra, "id", base), f"{base}.id")
        if aid in actions:
            raise err_dup(f"{base}.id", aid)
        kind = _as_str(_require(ra, "kind", base), f"{base}.kind")
        ws = _as_int(_require(ra, "window_start", base), f"{base}.window_start")
        we = _as_int(_require(ra, "window_end", base), f"{base}.window_end")
        # Windows live on step numbers and are half-open.  Step 0 is the
        # initial state, so a window may start there; an action can fire at
        # the last real step ``horizon``, so its window may end at
        # ``horizon + 1`` (mirroring the policy-segment convention).
        if not (0 <= ws <= we <= horizon + 1):
            raise err_value(
                f"{base}.window", "VALUE_OUT_OF_RANGE",
                f"window must satisfy 0 <= start <= end <= {horizon + 1}",
            )
        cost = _as_int(_require(ra, "cost", base), f"{base}.cost")
        if cost < 0:
            raise err_value(f"{base}.cost", "VALUE_OUT_OF_RANGE", "cost must be >= 0")
        kwargs: dict[str, Any] = dict(
            action_id=aid, kind=kind, window_start=ws, window_end=we, cost=cost
        )
        if kind == "destroy":
            cid = _as_str(_require(ra, "credential", base), f"{base}.credential")
            if cid not in credentials:
                raise err_ref(f"{base}.credential", cid)
            gen = _as_int(
                _require(ra, "generation", base), f"{base}.generation"
            )
            if gen < 1:
                raise err_value(f"{base}.generation", "VALUE_OUT_OF_RANGE", "generation >= 1")
            custodian = _as_str(
                _require(ra, "custodian", base), f"{base}.custodian"
            )
            if custodian not in orgs:
                raise err_ref(f"{base}.custodian", custodian)
            kwargs.update(
                credential=cid, generation=gen, custodian=custodian
            )
            destroy_custodian[aid] = custodian
        elif kind == "migrate":
            cid = _as_str(_require(ra, "credential", base), f"{base}.credential")
            if cid not in credentials:
                raise err_ref(f"{base}.credential", cid)
            frm = _as_str(
                _require(ra, "from_domain", base), f"{base}.from_domain"
            )
            to = _as_str(_require(ra, "to_domain", base), f"{base}.to_domain")
            if frm not in domains:
                raise err_ref(f"{base}.from_domain", frm)
            if to not in domains:
                raise err_ref(f"{base}.to_domain", to)
            if frm == to:
                raise err_value(
                    f"{base}.to_domain", "VALUE_OUT_OF_RANGE",
                    "migration source and target domains must differ",
                )
            custodian = _as_str(
                _require(ra, "custodian", base), f"{base}.custodian"
            )
            if custodian != domain_org[to]:
                raise err_value(
                    f"{base}.custodian", "CUSTODIAN_MISMATCH",
                    f"target custodian must be {domain_org[to]} (org of {to})",
                )
            kwargs.update(
                credential=cid, from_domain=frm, to_domain=to, custodian=custodian
            )
        elif kind == "receipt":
            destroy_id = _as_str(
                _require(ra, "destroy_action", base), f"{base}.destroy_action"
            )
            # forward references resolved after the loop
            confirmer = _as_str(
                _require(ra, "confirmer", base), f"{base}.confirmer"
            )
            if confirmer not in orgs:
                raise err_ref(f"{base}.confirmer", confirmer)
            kwargs.update(destroy_action=destroy_id, confirmer=confirmer)
        else:
            raise err_value(
                f"{base}.kind", "UNKNOWN_KIND",
                "action kind must be destroy, migrate or receipt",
            )
        actions[aid] = Action(**kwargs)

    # Resolve receipt -> destroy references and the "different organization"
    # authorization rule statically.
    for aid, action in actions.items():
        if action.kind == "receipt":
            target = action.destroy_action
            if target not in actions or actions[target].kind != "destroy":
                raise err_ref(f"action[{aid}].destroy_action", target)
            if action.confirmer == destroy_custodian[target]:
                raise err_value(
                    f"action[{aid}].confirmer", "CONFIRMER_SAME_ORG",
                    "receipt confirmer must be a different organization "
                    "than the destroying custodian",
                )
    return actions


def _load_policy(
    root: dict, horizon: int, nodes: dict[str, Node]
) -> tuple[PolicySegment, ...]:
    if "policy_segments" not in root:
        return ()
    raw_segments = _as_list(root["policy_segments"], "policy_segments")
    segments: list[PolicySegment] = []
    for i, raw in enumerate(raw_segments):
        base = f"policy_segments[{i}]"
        rs = _as_dict(raw, base)
        target = _as_str(_require(rs, "target", base), f"{base}.target")
        if target not in nodes:
            raise err_ref(f"{base}.target", target)
        retain = _require(rs, "retain", base)
        if not isinstance(retain, bool):
            raise err_type(f"{base}.retain", "boolean", retain)
        start = _as_int(_require(rs, "start", base), f"{base}.start")
        end = _as_int(_require(rs, "end", base), f"{base}.end")
        if not (0 <= start < end <= horizon + 1):
            raise err_value(
                f"{base}", "VALUE_OUT_OF_RANGE",
                f"segment bounds must satisfy 0 <= start < end <= {horizon + 1}",
            )
        segments.append(PolicySegment(target, retain, start, end))

    # Segments of the same target may not overlap; they need not be contiguous
    # (an uncovered step means no retention requirement at that step).
    by_target: dict[str, list[PolicySegment]] = {}
    for seg in segments:
        by_target.setdefault(seg.target, []).append(seg)
    for target, segs in by_target.items():
        ordered = sorted(segs, key=lambda s: (s.start, s.end))
        for a, b in zip(ordered, ordered[1:]):
            if b.start < a.end:
                raise err_value(
                    "policy_segments", "POLICY_OVERLAP",
                    f"overlapping segments for target {target}: "
                    f"[{a.start},{a.end}) and [{b.start},{b.end})",
                )
    return tuple(segments)


def _load_objectives(
    root: dict, horizon: int, nodes: dict[str, Node],
    policy_segments: tuple[PolicySegment, ...],
) -> tuple[Objective, ...]:
    raw_objectives = _as_list(_require(root, "objectives", ""), "objectives")
    if not raw_objectives:
        raise err_value("objectives", "EMPTY_OBJECTIVES", "at least one objective is required")
    objectives: list[Objective] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_objectives):
        base = f"objectives[{i}]"
        ro = _as_dict(raw, base)
        target = _as_str(_require(ro, "target", base), f"{base}.target")
        if target not in nodes:
            raise err_ref(f"{base}.target", target)
        if target in seen:
            raise err_dup(f"{base}.target", target)
        seen.add(target)
        goal = _as_str(_require(ro, "goal", base), f"{base}.goal")
        if goal not in ("retain", "retire"):
            raise err_value(
                f"{base}.goal", "UNKNOWN_GOAL",
                "goal must be 'retain' or 'retire'",
            )
        objectives.append(Objective(target, goal))

    # A retain objective without explicit segments is retained over the whole
    # finite horizon (steps 0..horizon inclusive, i.e. end = horizon + 1).
    covered = {seg.target for seg in policy_segments}
    extra: list[PolicySegment] = []
    for obj in objectives:
        if obj.goal == "retain" and obj.target not in covered:
            extra.append(PolicySegment(obj.target, True, 0, horizon + 1))
    if extra:
        policy_segments = tuple(policy_segments) + tuple(extra)
    return tuple(objectives)


# ---------------------------------------------------------------------------
# Initial state and action application
# ---------------------------------------------------------------------------
def initial_state(case: Case) -> RuntimeState:
    return RuntimeState(
        step=0,
        creds=dict(case.initial_creds),
        used=frozenset(),
        history=(),
    )


def effective_retain(case: Case, target: str, step: int) -> bool:
    """Retention requirement for ``target`` at ``step`` (left-closed/right-open)."""
    for seg in case.policy_segments:
        if seg.target == target and seg.start <= step < seg.end:
            return seg.retain
    return False


def apply_action(
    case: Case, state: RuntimeState, action_id: str
) -> RuntimeState:
    """Apply an authorized action at the state's current step.

    Raises :class:`CaseError` with a stable code on every illegal transition.
    """
    if action_id not in case.actions:
        raise err_ref("action_id", action_id)
    action = case.actions[action_id]
    if action_id in state.used:
        raise err_value(
            "action_id", "ACTION_ALREADY_USED",
            f"action {action_id} has already occurred (each action at most once)",
        )
    if not (action.window_start <= state.step < action.window_end):
        raise err_value(
            "action_id", "OUTSIDE_WINDOW",
            f"step {state.step} is outside window "
            f"[{action.window_start},{action.window_end}) of {action_id}",
        )

    creds = dict(state.creds)
    entry = {"step": state.step, "action_id": action_id, "kind": action.kind}

    if action.kind == "destroy":
        cred = creds.get(action.credential)
        if cred is None:
            raise err_value(
                action_id, "CREDENTIAL_UNREGISTERED",
                f"credential {action.credential} has no registered replica",
            )
        if cred.status == DESTROYED:
            raise err_value(
                action_id, "REPLICA_ALREADY_DESTROYED",
                f"credential {action.credential} is already destroyed",
            )
        if cred.status == PENDING:
            raise err_value(
                action_id, "DESTRUCTION_PENDING",
                f"credential {action.credential} already awaits a receipt",
            )
        if cred.generation != action.generation:
            raise err_value(
                action_id, "GENERATION_MISMATCH",
                f"authorized generation {action.generation} != current "
                f"generation {cred.generation}",
            )
        if cred.custodian != action.custodian:
            raise err_value(
                action_id, "CUSTODIAN_MISMATCH",
                f"authorized custodian {action.custodian} != current "
                f"custodian {cred.custodian}",
            )
        creds[action.credential] = replace(
            cred, status=PENDING, pending_action=action_id
        )

    elif action.kind == "migrate":
        cred = creds.get(action.credential)
        if cred is None:
            raise err_value(
                action_id, "CREDENTIAL_UNREGISTERED",
                f"credential {action.credential} has no registered replica",
            )
        if cred.status != USABLE:
            raise err_value(
                action_id, "NOT_USABLE",
                "only a usable current replica may be migrated; "
                f"status is {cred.status}",
            )
        if cred.domain != action.from_domain:
            raise err_value(
                action_id, "DOMAIN_MISMATCH",
                f"replica is at {cred.domain}, authorization names {action.from_domain}",
            )
        # Atomic move to an approved custody domain: the old generation dies
        # and a new generation becomes the sole current replica.
        creds[action.credential] = CredState(
            status=USABLE,
            generation=cred.generation + 1,
            domain=action.to_domain,
            custodian=action.custodian,
        )

    else:  # receipt
        destroy = case.actions[action.destroy_action]
        cred = creds.get(destroy.credential)
        if cred is None:
            raise err_value(
                action_id, "NO_PENDING_DESTRUCTION",
                f"no pending destruction for credential {destroy.credential}",
            )
        if cred.status == DESTROYED:
            raise err_value(
                action_id, "RECEIPT_DUPLICATE",
                "a valid receipt already confirmed this destruction; "
                "duplicate receipts change nothing",
            )
        if cred.status == USABLE:
            raise err_value(
                action_id, "RECEIPT_OLD_GENERATION",
                "the authorized generation is dead (a successor replica is "
                "usable); an old-generation receipt cannot remove it",
            )
        if cred.status != PENDING or cred.pending_action != destroy.action_id:
            raise err_value(
                action_id, "RECEIPT_MISMATCH",
                "receipt does not match the unique currently pending destruction",
            )
        if action.confirmer == cred.custodian:
            raise err_value(
                action_id, "CONFIRMER_SAME_ORG",
                "receipt must be confirmed by a different organization",
            )
        evidence = {
            "destroy_action": destroy.action_id,
            "receipt_action": action_id,
            "credential": destroy.credential,
            "generation": cred.generation,
            "domain": cred.domain,
            "custodian": cred.custodian,
            "confirmer": action.confirmer,
            "confirmed_step": state.step,
        }
        creds[destroy.credential] = CredState(
            status=DESTROYED,
            generation=cred.generation,
            domain=cred.domain,
            custodian=cred.custodian,
            evidence=evidence,
        )

    return RuntimeState(
        step=state.step + 1,
        creds=creds,
        used=state.used | {action_id},
        history=state.history + (entry,),
    )


def advance_wait(state: RuntimeState) -> RuntimeState:
    """Explicitly wait one step without changing custody state."""
    return replace(state, step=state.step + 1)
