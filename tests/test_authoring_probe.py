"""Executable record of the transport facts the authoring layer's design rests on.

These are not tests of bathos code. They pin behaviour of pydantic and FastMCP that
three design decisions depend on, so that a dependency upgrade which changes any of
them fails loudly here rather than silently degrading the authoring surface.

Design decisions pinned:

* Payload models use ``extra="allow"``, not ``extra="forbid"`` and not ``TypedDict``.
  ``forbid`` raises inside FastMCP's own coercion, *before* the wrapped function, so
  bathos's ``@traced_tool`` never sees it and the ``ok``/``error_code``/``error``/
  ``resolution_hint`` envelope is bypassed. ``TypedDict`` silently drops typo'd keys,
  recreating the class of bug the layer exists to eliminate. ``allow`` publishes the
  full per-field schema *and* preserves unknown keys in ``model_extra``, letting the
  tool body reject them through the normal envelope.

* MCP tool input schemas stay shallow, so nested TOML sub-blocks are flattened with
  prefixed keys in the payload model and re-nested by the renderer.
"""

from __future__ import annotations

from typing import TypedDict

import pytest
from pydantic import BaseModel, ConfigDict


class AllowModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    headline: str


class ForbidModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headline: str


class TypedDictPayload(TypedDict):
    headline: str


def test_extra_allow_preserves_unknown_keys_for_the_tool_body_to_reject():
    """The chosen configuration: a typo survives coercion and stays inspectable."""
    m = AllowModel.model_validate({"headline": "x", "headlien": "typo"})
    assert m.model_extra == {"headlien": "typo"}, (
        "extra='allow' must surface unknown keys in model_extra -- this is how the "
        "authoring tools reject typos through the normal error envelope"
    )


def test_extra_allow_still_publishes_the_real_field_schema():
    """Permissiveness must not cost the agent its field guidance."""
    schema = AllowModel.model_json_schema()
    assert "headline" in schema["properties"]
    assert schema["properties"]["headline"]["type"] == "string"


def test_extra_forbid_raises_during_coercion():
    """Why forbid was rejected: it fails before the wrapped function runs.

    Under FastMCP this happens inside Tool.run, so @traced_tool never observes it and
    the structured error envelope is bypassed.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        ForbidModel.model_validate({"headline": "x", "headlien": "typo"})
    assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())


def test_typeddict_silently_drops_unknown_keys():
    """Why TypedDict was rejected: the typo vanishes with no signal at all."""
    from pydantic import TypeAdapter

    coerced = TypeAdapter(TypedDictPayload).validate_python({"headline": "x", "headlien": "typo"})
    assert coerced == {"headline": "x"}, (
        "TypedDict drops unknown keys silently -- exactly the failure mode the "
        "authoring layer exists to eliminate, so it must not be used for payloads"
    )


def _max_depth(schema: dict, defs: dict | None = None, seen: frozenset = frozenset()) -> int:
    """Nesting depth of a JSON schema, following local $refs once each."""
    defs = defs if defs is not None else schema.get("$defs", {})

    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            return 0
        return _max_depth(defs.get(name, {}), defs, seen | {name})

    props = schema.get("properties")
    if not props:
        items = schema.get("items")
        return _max_depth(items, defs, seen) if items else 0

    return 1 + max((_max_depth(p, defs, seen) for p in props.values()), default=0)


class _Element(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    label: str


class _Doc(BaseModel):
    model_config = ConfigDict(extra="allow")
    headline: str
    hypotheses: list[_Element]


def test_flattened_payload_schema_stays_shallow():
    """A list-of-flat-objects payload stays at depth 2.

    Overly nested tool schemas are a known failure mode for tool-calling models. The
    authoring models keep sub-blocks flat (``reference_parity_paper`` rather than a
    nested ``reference_parity`` object) so this bound holds; the renderer restores the
    real TOML nesting.
    """
    assert _max_depth(_Doc.model_json_schema()) == 2
