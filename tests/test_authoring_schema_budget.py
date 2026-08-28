"""Budget guards on the authoring tools' published input schemas.

Overly large or deeply nested tool schemas are a known failure mode for tool-calling
models. These bounds are deliberately stated as measured facts rather than aspirations,
so a future field addition that blows the budget fails here instead of degrading tool-call
accuracy silently.

An honest note on depth. The payload model itself is depth 2 (document -> list of flat
element objects). Passed as a *tool parameter* it is depth 3: `claim` -> `hypotheses` ->
element fields. Depth 3 is the floor for any document containing repeated sub-entities;
it cannot be flattened further without losing the list structure. What flattening buys is
that the leaves are flat -- `reference_parity_paper` rather than a nested
`reference_parity` object -- which is what keeps it at 3 rather than 4.

Computed from the pydantic model, never from FastMCP internals: cisternal pins
fastmcp==4.0.0a2, an alpha whose Tool introspection surface may move.
"""

from __future__ import annotations

import inspect
import json

import pytest

from bathos.authoring.models import ClaimPayload

# A tool with too many knobs is as hard to call correctly as one with too few.
MAX_TOP_LEVEL_PARAMS = 8

# Depth of the payload model itself, excluding the tool-parameter wrapper.
MAX_PAYLOAD_DEPTH = 2

# Generous headroom over the current ~6.5KB; catches a runaway, not ordinary growth.
MAX_SCHEMA_BYTES = 12_000


def _depth(schema: dict, defs: dict | None = None, seen: frozenset = frozenset()) -> int:
    """Nesting depth of a JSON schema, following each local $ref at most once."""
    defs = defs if defs is not None else schema.get("$defs", {})

    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        return 0 if name in seen else _depth(defs.get(name, {}), defs, seen | {name})

    props = schema.get("properties")
    if not props:
        items = schema.get("items")
        return _depth(items, defs, seen) if items else 0

    return 1 + max((_depth(p, defs, seen) for p in props.values()), default=0)


def test_claim_author_stays_within_the_parameter_budget():
    from bathos.mcp import claim_author

    params = list(inspect.signature(claim_author).parameters)
    assert len(params) <= MAX_TOP_LEVEL_PARAMS, (
        f"claim_author has {len(params)} params ({params}); keep it under "
        f"{MAX_TOP_LEVEL_PARAMS} so the tool stays callable"
    )


def test_claim_payload_schema_stays_shallow():
    depth = _depth(ClaimPayload.model_json_schema())
    assert depth <= MAX_PAYLOAD_DEPTH, (
        f"ClaimPayload is {depth} levels deep. Sub-blocks must stay flattened with "
        "prefixed keys (reference_parity_paper, not a nested reference_parity object) -- "
        "the renderer is what restores the real TOML nesting."
    )


def test_claim_payload_schema_stays_small():
    size = len(json.dumps(ClaimPayload.model_json_schema()))
    assert size <= MAX_SCHEMA_BYTES, (
        f"ClaimPayload schema is {size} bytes, over the {MAX_SCHEMA_BYTES} budget"
    )


def test_element_models_are_flat():
    """Every $def is a flat object -- this is what holds the payload at depth 2."""
    schema = ClaimPayload.model_json_schema()
    for name, definition in schema.get("$defs", {}).items():
        for field, spec in definition.get("properties", {}).items():
            assert _depth(spec, schema.get("$defs", {})) == 0, (
                f"{name}.{field} is a nested object; flatten it with a prefixed key so "
                "the published tool schema stays shallow"
            )


@pytest.mark.parametrize("required_field", ["headline", "kill_condition"])
def test_schema_marks_the_genuinely_required_fields(required_field):
    """An agent must be able to see what it has to supply without trial and error."""
    assert required_field in ClaimPayload.model_json_schema().get("required", [])


def test_kill_condition_satisfiable_by_null_is_required():
    """AC-23: it has no default precisely so an author cannot omit the question."""
    assert "kill_condition_satisfiable_by_null" in ClaimPayload.model_json_schema().get(
        "required", []
    )


def test_parity_run_id_is_absent_from_the_authoring_surface():
    """It is bound by attest-parity, not by the author -- see ConfoundPayload's docstring.

    Asserts on FIELD NAMES rather than the serialized schema: the string also appears in
    ConfoundPayload's docstring, which pydantic emits as the schema description.
    """
    schema = ClaimPayload.model_json_schema()

    field_names = set(schema.get("properties", {}))
    for definition in schema.get("$defs", {}).values():
        field_names |= set(definition.get("properties", {}))

    assert "parity_run_id" not in field_names, (
        "exposing parity_run_id to authors reintroduces the 'already set or TOML format "
        "mismatch' error class that removing it eliminated"
    )
