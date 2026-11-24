"""Campaign formulation validation (#3315).

Requires three pre-registration planning artifacts before a claim-bound campaign is created:
1. A ``hypothesis -> arm -> readout -> confound/control`` matrix.
2. A decision tree with bounded fallbacks (finite bound, non-empty fallback paths, acyclic).
3. An ``'already done -> used by'`` reuse table.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from bathos.campaigns import CampaignError

MAX_FORMULATION_FALLBACK_BOUND = 10


@dataclass(frozen=True)
class MatrixRow:
    """Single row of the hypothesis -> arm -> readout -> confound/control matrix."""

    hypothesis: str
    arm: str
    readout: str
    confound_control: str


@dataclass(frozen=True)
class DecisionBranch:
    """Single branch/node in a bounded-fallback decision tree."""

    branch_id: str
    condition: str
    action: str
    fallback: str = ""
    next_branch: str | None = None
    fallback_to: str | None = None
    max_fallbacks: int | None = None
    terminal: bool = False


@dataclass(frozen=True)
class ReuseEntry:
    """Single row of the 'already done -> used by' reuse table."""

    already_done: str
    used_by: str
    artifact_ref: str = ""
    notes: str = ""


@dataclass(frozen=True)
class CampaignFormulation:
    """Structured campaign formulation declaration (#3315)."""

    matrix: tuple[MatrixRow, ...]
    decision_tree: tuple[DecisionBranch, ...]
    reuse_table: tuple[ReuseEntry, ...]
    max_fallbacks: int | None = None
    max_depth: int | None = None


@dataclass(frozen=True)
class FormulationValidationResult:
    """Validation result returned by :func:`validate_formulation`."""

    ok: bool
    errors: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    formulation: CampaignFormulation | None = None


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CampaignError(f"{label} must be a positive integer, got {value!r}")
    if value < 1:
        raise CampaignError(f"{label} must be >= 1, got {value}")
    return value


def _parse_matrix(raw_matrix: Any) -> tuple[MatrixRow, ...]:
    if raw_matrix is None:
        return ()
    if not isinstance(raw_matrix, list | tuple):
        raise CampaignError("formulation.matrix must be a list of table rows")

    rows: list[MatrixRow] = []
    for idx, item in enumerate(raw_matrix):
        if isinstance(item, MatrixRow):
            rows.append(item)
            continue
        if not isinstance(item, Mapping):
            raise CampaignError(f"formulation.matrix[{idx}] must be a mapping/table")
        confound_control = (
            _clean_str(item.get("confound_control"))
            or _clean_str(item.get("confound"))
            or _clean_str(item.get("control"))
        )
        rows.append(
            MatrixRow(
                hypothesis=_clean_str(item.get("hypothesis")),
                arm=_clean_str(item.get("arm")),
                readout=_clean_str(item.get("readout")),
                confound_control=confound_control,
            )
        )
    return tuple(rows)


def _parse_decision_tree(
    raw_tree: Any,
    top_max_fallbacks: Any = None,
    top_max_depth: Any = None,
) -> tuple[tuple[DecisionBranch, ...], int | None, int | None]:
    max_fallbacks = (
        _parse_positive_int(top_max_fallbacks, "formulation.max_fallbacks")
        if top_max_fallbacks is not None
        else None
    )
    max_depth = (
        _parse_positive_int(top_max_depth, "formulation.max_depth")
        if top_max_depth is not None
        else None
    )

    if raw_tree is None:
        return (), max_fallbacks, max_depth

    raw_branches: Any
    if isinstance(raw_tree, Mapping):
        if "max_fallbacks" in raw_tree and raw_tree["max_fallbacks"] is not None:
            max_fallbacks = _parse_positive_int(
                raw_tree["max_fallbacks"], "formulation.decision_tree.max_fallbacks"
            )
        if "max_depth" in raw_tree and raw_tree["max_depth"] is not None:
            max_depth = _parse_positive_int(
                raw_tree["max_depth"], "formulation.decision_tree.max_depth"
            )
        raw_branches = (
            raw_tree.get("branches")
            if "branches" in raw_tree
            else raw_tree.get("nodes", [])
        )
    elif isinstance(raw_tree, list | tuple):
        raw_branches = raw_tree
    else:
        raise CampaignError(
            "formulation.decision_tree must be a list of branches or a table with 'branches'"
        )

    if not isinstance(raw_branches, list | tuple):
        raise CampaignError("formulation.decision_tree.branches must be a list")

    branches: list[DecisionBranch] = []
    for idx, item in enumerate(raw_branches):
        if isinstance(item, DecisionBranch):
            branches.append(item)
            continue
        if not isinstance(item, Mapping):
            raise CampaignError(
                f"formulation.decision_tree[{idx}] must be a mapping/table"
            )
        branch_id = _clean_str(item.get("branch_id")) or _clean_str(item.get("id")) or f"branch_{idx + 1}"
        branch_max_fb = (
            _parse_positive_int(item["max_fallbacks"], f"decision_tree[{branch_id}].max_fallbacks")
            if item.get("max_fallbacks") is not None
            else None
        )
        next_branch = _clean_str(item.get("next_branch")) or None
        fallback_to = _clean_str(item.get("fallback_to")) or None
        branches.append(
            DecisionBranch(
                branch_id=branch_id,
                condition=_clean_str(item.get("condition")),
                action=_clean_str(item.get("action")),
                fallback=_clean_str(item.get("fallback")),
                next_branch=next_branch,
                fallback_to=fallback_to,
                max_fallbacks=branch_max_fb,
                terminal=bool(item.get("terminal", False)),
            )
        )
    return tuple(branches), max_fallbacks, max_depth


def _parse_reuse_table(raw_reuse: Any) -> tuple[ReuseEntry, ...]:
    if raw_reuse is None:
        return ()
    if not isinstance(raw_reuse, list | tuple):
        raise CampaignError("formulation.reuse_table must be a list of entries")

    entries: list[ReuseEntry] = []
    for idx, item in enumerate(raw_reuse):
        if isinstance(item, ReuseEntry):
            entries.append(item)
            continue
        if not isinstance(item, Mapping):
            raise CampaignError(f"formulation.reuse_table[{idx}] must be a mapping/table")
        entries.append(
            ReuseEntry(
                already_done=_clean_str(item.get("already_done")),
                used_by=_clean_str(item.get("used_by")),
                artifact_ref=_clean_str(item.get("artifact_ref")),
                notes=_clean_str(item.get("notes")),
            )
        )
    return tuple(entries)


def parse_formulation(raw: Mapping[str, Any] | CampaignFormulation) -> CampaignFormulation:
    """Parse a mapping or :class:`CampaignFormulation` into a validated structure."""
    if isinstance(raw, CampaignFormulation):
        return raw
    if not isinstance(raw, Mapping):
        raise CampaignError(
            f"formulation must be a mapping/table, got {type(raw).__name__}"
        )

    raw_matrix = raw.get("matrix") if "matrix" in raw else raw.get("hypothesis_matrix")
    raw_tree = raw.get("decision_tree")
    raw_reuse = raw.get("reuse_table") if "reuse_table" in raw else raw.get("reuse")

    matrix = _parse_matrix(raw_matrix)
    branches, max_fallbacks, max_depth = _parse_decision_tree(
        raw_tree,
        top_max_fallbacks=raw.get("max_fallbacks"),
        top_max_depth=raw.get("max_depth"),
    )
    reuse_table = _parse_reuse_table(raw_reuse)

    return CampaignFormulation(
        matrix=matrix,
        decision_tree=branches,
        reuse_table=reuse_table,
        max_fallbacks=max_fallbacks,
        max_depth=max_depth,
    )


def _check_decision_tree_cycles_and_depth(
    branches: tuple[DecisionBranch, ...],
    fallback_bound: int | None,
    max_depth: int | None,
    errors: list[str],
) -> None:
    by_id: dict[str, DecisionBranch] = {}
    for branch in branches:
        if branch.branch_id in by_id:
            errors.append(f"decision_tree contains duplicate branch_id {branch.branch_id!r}")
        by_id[branch.branch_id] = branch

    for branch in branches:
        if branch.next_branch is not None and branch.next_branch not in by_id:
            errors.append(
                f"decision_tree branch {branch.branch_id!r} next_branch references unknown "
                f"branch {branch.next_branch!r}"
            )
        if branch.fallback_to is not None and branch.fallback_to not in by_id:
            errors.append(
                f"decision_tree branch {branch.branch_id!r} fallback_to references unknown "
                f"branch {branch.fallback_to!r}"
            )

    # Detect cycles across next_branch and fallback_to transitions.
    visiting: set[str] = set()
    visited: set[str] = set()

    def _dfs_cycle(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited or node_id not in by_id:
            return False
        visiting.add(node_id)
        node = by_id[node_id]
        for nxt in (node.next_branch, node.fallback_to):
            if nxt is not None and _dfs_cycle(nxt):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    for branch_id in by_id:
        if _dfs_cycle(branch_id):
            errors.append(
                f"decision_tree contains an unbounded cycle involving branch {branch_id!r}"
            )
            return

    # Verify fallback chain lengths do not exceed bounded limits.
    def _fallback_chain_len(node_id: str) -> int:
        seen: set[str] = set()
        curr = node_id
        steps = 0
        while curr in by_id and curr not in seen:
            seen.add(curr)
            nxt = by_id[curr].fallback_to
            if nxt is None:
                break
            steps += 1
            curr = nxt
        return steps

    def _max_path_depth(node_id: str) -> int:
        node = by_id[node_id]
        children = [c for c in (node.next_branch, node.fallback_to) if c is not None and c in by_id]
        if not children:
            return 1
        return 1 + max(_max_path_depth(c) for c in children)

    for branch in branches:
        fb_len = _fallback_chain_len(branch.branch_id)
        effective_fb_bound = branch.max_fallbacks if branch.max_fallbacks is not None else fallback_bound
        if effective_fb_bound is not None and fb_len > effective_fb_bound:
            errors.append(
                f"decision_tree fallback chain from {branch.branch_id!r} has length {fb_len}, "
                f"exceeding max_fallbacks={effective_fb_bound}"
            )
        if max_depth is not None:
            depth = _max_path_depth(branch.branch_id)
            if depth > max_depth:
                errors.append(
                    f"decision_tree path depth from {branch.branch_id!r} is {depth}, "
                    f"exceeding max_depth={max_depth}"
                )


def validate_formulation(
    raw_or_formulation: Mapping[str, Any] | CampaignFormulation,
    *,
    known_hypotheses: Iterable[str] | None = None,
    max_allowed_depth: int = MAX_FORMULATION_FALLBACK_BOUND,
) -> FormulationValidationResult:
    """Validate campaign formulation requirements (#3315).

    Checks:
    1. ``hypothesis -> arm -> readout -> confound/control`` matrix is non-empty and complete.
    2. Decision tree has >= 1 branch, explicit finite fallback bound, non-empty fallback
       actions on non-terminal branches, and no cycles or bound overruns.
    3. ``'already done -> used by'`` reuse table is non-empty with filled entries.
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        formulation = parse_formulation(raw_or_formulation)
    except CampaignError as exc:
        return FormulationValidationResult(ok=False, errors=(str(exc),))

    # 1. Hypothesis -> arm -> readout -> confound/control matrix
    if not formulation.matrix:
        errors.append(
            "formulation.matrix is required and must declare at least one "
            "(hypothesis -> arm -> readout -> confound/control) row"
        )
    else:
        seen_rows: set[tuple[str, str, str]] = set()
        for idx, row in enumerate(formulation.matrix):
            if not row.hypothesis:
                errors.append(f"formulation.matrix[{idx}] missing non-empty 'hypothesis'")
            if not row.arm:
                errors.append(f"formulation.matrix[{idx}] missing non-empty 'arm'")
            if not row.readout:
                errors.append(f"formulation.matrix[{idx}] missing non-empty 'readout'")
            if not row.confound_control:
                errors.append(f"formulation.matrix[{idx}] missing non-empty 'confound_control'")
            key = (row.hypothesis, row.arm, row.readout)
            if all(key):
                if key in seen_rows:
                    warnings.append(
                        f"duplicate matrix row for hypothesis={row.hypothesis!r}, "
                        f"arm={row.arm!r}, readout={row.readout!r}"
                    )
                seen_rows.add(key)

        if known_hypotheses is not None:
            known_set = {str(h).strip() for h in known_hypotheses if str(h).strip()}
            if known_set:
                covered_hypotheses: set[str] = set()
                for idx, row in enumerate(formulation.matrix):
                    if row.hypothesis and row.hypothesis not in known_set:
                        errors.append(
                            f"formulation.matrix[{idx}] references unknown hypothesis "
                            f"{row.hypothesis!r} (declared: {sorted(known_set)})"
                        )
                    elif row.hypothesis:
                        covered_hypotheses.add(row.hypothesis)
                for h_id in sorted(known_set - covered_hypotheses):
                    if "null" not in h_id.lower() and "misspec" not in h_id.lower():
                        warnings.append(
                            f"declared hypothesis {h_id!r} has no row in formulation.matrix"
                        )

    # 2. Decision tree with bounded fallbacks
    if not formulation.decision_tree:
        errors.append(
            "formulation.decision_tree is required and must declare at least one branch "
            "with bounded fallbacks"
        )
    else:
        tree_bound = (
            formulation.max_fallbacks
            if formulation.max_fallbacks is not None
            else formulation.max_depth
        )
        if tree_bound is not None and tree_bound > max_allowed_depth:
            errors.append(
                f"formulation fallback bound ({tree_bound}) exceeds maximum allowed "
                f"({max_allowed_depth})"
            )

        any_fallback_declared = False
        for branch in formulation.decision_tree:
            if not branch.condition:
                errors.append(
                    f"decision_tree branch {branch.branch_id!r} missing non-empty 'condition'"
                )
            if not branch.action:
                errors.append(
                    f"decision_tree branch {branch.branch_id!r} missing non-empty 'action'"
                )
            branch_bound = branch.max_fallbacks if branch.max_fallbacks is not None else tree_bound
            if branch.max_fallbacks is not None and branch.max_fallbacks > max_allowed_depth:
                errors.append(
                    f"decision_tree branch {branch.branch_id!r} max_fallbacks "
                    f"({branch.max_fallbacks}) exceeds maximum allowed ({max_allowed_depth})"
                )
            has_fallback = bool(branch.fallback or branch.fallback_to)
            if has_fallback:
                any_fallback_declared = True
                if branch_bound is None:
                    errors.append(
                        f"decision_tree branch {branch.branch_id!r} declares a fallback without "
                        "an explicit finite bound ('max_fallbacks' or 'max_depth')"
                    )
            elif not branch.terminal:
                errors.append(
                    f"non-terminal decision_tree branch {branch.branch_id!r} must declare a "
                    "bounded 'fallback' or 'fallback_to' (or set terminal=true)"
                )

        if not any_fallback_declared and tree_bound is None:
            errors.append(
                "formulation.decision_tree must declare at least one bounded fallback path "
                "with 'max_fallbacks' or 'max_depth'"
            )

        _check_decision_tree_cycles_and_depth(
            formulation.decision_tree,
            fallback_bound=tree_bound,
            max_depth=formulation.max_depth,
            errors=errors,
        )

    # 3. 'already done -> used by' reuse table
    if not formulation.reuse_table:
        errors.append(
            "formulation.reuse_table is required and must declare at least one "
            "'already done -> used by' entry"
        )
    else:
        for idx, entry in enumerate(formulation.reuse_table):
            if not entry.already_done:
                errors.append(f"formulation.reuse_table[{idx}] missing non-empty 'already_done'")
            if not entry.used_by:
                errors.append(f"formulation.reuse_table[{idx}] missing non-empty 'used_by'")

    return FormulationValidationResult(
        ok=len(errors) == 0,
        errors=tuple(errors),
        warnings=tuple(warnings),
        formulation=formulation if not errors else None,
    )


def require_valid_formulation(
    raw_or_formulation: Mapping[str, Any] | CampaignFormulation,
    *,
    known_hypotheses: Iterable[str] | None = None,
    max_allowed_depth: int = MAX_FORMULATION_FALLBACK_BOUND,
) -> CampaignFormulation:
    """Validate formulation and raise :class:`CampaignError` on any error."""
    result = validate_formulation(
        raw_or_formulation,
        known_hypotheses=known_hypotheses,
        max_allowed_depth=max_allowed_depth,
    )
    if not result.ok or result.formulation is None:
        raise CampaignError("; ".join(result.errors))
    return result.formulation
