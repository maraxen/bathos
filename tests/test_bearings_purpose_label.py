"""The sidecar [charter] purpose label (spec 261104 T7, owner 261109): written at the time of work, never seen by a
reader. Covers the shape rules, the fail-closed strip, the packet builder's use of it, and sidecar validation."""

from __future__ import annotations

import textwrap
import tomllib
from pathlib import Path

import pytest

from bathos.bearings.purpose_label import PurposeLabelError, read_purpose_label, strip_purpose_label
from tests.test_bearings_packet import (  # noqa: F401  (env is a fixture)
    DATE,
    build,
    commit_files,
    confirmed_repo,
    env,
    excluded,
    insert_runs,
    manifest,
    reader_files,
)

BODY = '[experiment]\nhypothesis = "lagoon washout at 1.5 volumes per hour"\n\n[result_schema]\nx = "float"\n'


# ── shape ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "toml,expected",
    [
        ("", None),
        ('[charter]\npurpose = "lagoon-simulation"\n', ["lagoon-simulation"]),
        (
            '[charter]\npurpose = ["size-selections", "assembly-method"]\n',
            ["assembly-method", "size-selections"],
        ),
        ('[charter]\npurpose = "l7"\n', ["l7"]),
    ],
    ids=["absent", "one", "two-sorted", "short"],
)
def test_valid_labels_read_as_sorted_slugs(toml, expected):
    assert read_purpose_label(tomllib.loads(toml)) == expected


@pytest.mark.parametrize(
    "toml",
    [
        '[charter]\npurpose = "L5"\n',  # not lowercase
        '[charter]\npurpose = "lagoon simulation"\n',  # space
        '[charter]\npurpose = "-lagoon"\n',  # leading hyphen
        "[charter]\npurpose = []\n",  # empty list
        '[charter]\npurpose = ["a", "a"]\n',  # repeat
        "[charter]\npurpose = 5\n",  # not a string
        '[charter]\nother = "x"\npurpose = "a"\n',  # unknown key
        "[charter]\n",  # no purpose
        'charter = "lagoon"\n',  # not a table
    ],
    ids=[
        "uppercase",
        "space",
        "leading-hyphen",
        "empty",
        "repeat",
        "number",
        "unknown-key",
        "missing",
        "scalar",
    ],
)
def test_malformed_labels_are_refused(toml):
    with pytest.raises(PurposeLabelError):
        read_purpose_label(tomllib.loads(toml))


# ── strip ──────────────────────────────────────────────────────────────────────


def test_text_without_a_label_is_returned_byte_for_byte():
    assert strip_purpose_label(BODY) == (BODY, False)


@pytest.mark.parametrize("where", ["top", "middle", "bottom"])
def test_the_label_table_is_removed_wherever_it_sits_and_nothing_else_changes(where):
    label = '[charter]\npurpose = "lagoon-simulation"  # set when written\n\n'
    head, tail = BODY.split("\n\n")
    text = {
        "top": label + BODY,
        "middle": head + "\n\n" + label + tail,
        "bottom": BODY + "\n" + label,
    }[where]
    stripped, had = strip_purpose_label(text)
    assert had is True
    assert "lagoon-simulation" not in stripped and "charter" not in stripped
    assert tomllib.loads(stripped) == tomllib.loads(BODY)


def test_a_charter_subtable_is_removed_too():
    text = BODY + '[charter]\npurpose = "a"\n[charter.note]\nwhy = "lagoon-simulation"\n'
    stripped, had = strip_purpose_label(text)
    assert had and "lagoon-simulation" not in stripped


@pytest.mark.parametrize(
    "text",
    [
        'charter = { purpose = "lagoon-simulation" }\n' + BODY,
        'charter.purpose = "lagoon-simulation"\n' + BODY,
        '[charter]\npurpose = """\n[experiment]\nlagoon-simulation"""\n' + BODY,
    ],
    ids=["inline-table", "dotted-key", "string-with-a-fake-header"],
)
def test_a_label_that_cannot_be_removed_cleanly_raises(text):
    with pytest.raises(PurposeLabelError):
        strip_purpose_label(text)


def test_unparseable_text_naming_the_table_raises_rather_than_passing_through():
    with pytest.raises(PurposeLabelError):
        strip_purpose_label('[charter]\npurpose = "lagoon-simulation\n')


# ── packet ─────────────────────────────────────────────────────────────────────


def _run_with_sidecar(env, content: str, name: str = "s") -> str:  # noqa: F811
    repo = confirmed_repo(env)
    path = f"scripts/{name}.bth.toml"
    sha = commit_files(repo, {path: content})
    insert_runs(
        env,
        [
            {
                "id": "aaaaaaaa00000009",
                "project_slug": "pkt",
                "timestamp": DATE,
                "git_hash": sha,
                "sidecar_path": path,
                "command": f"python {name}",
                "status": "completed",
            }
        ],
    )
    return path


def test_a_labelled_sidecar_reaches_the_reader_without_its_label(env):  # noqa: F811
    path = _run_with_sidecar(env, '[charter]\npurpose = "lagoon-simulation"\n\n' + BODY)
    out, _ = build(env)
    files = {k: v for k, v in reader_files(out).items() if k.endswith(path)}
    assert len(files) == 1
    text = next(iter(files.values())).decode()
    assert "lagoon-simulation" not in text and "[charter]" not in text
    assert "lagoon washout" in text  # the rest of the sidecar is still there
    assert [s.split("@")[0] for s in manifest(out)["labels_stripped"]] == [f"sidecar:{path}"]


def test_an_unlabelled_sidecar_is_copied_unchanged_and_nothing_is_listed(env):  # noqa: F811
    path = _run_with_sidecar(env, BODY)
    out, _ = build(env)
    assert [v for k, v in reader_files(out).items() if k.endswith(path)] == [BODY.encode()]
    assert manifest(out)["labels_stripped"] == []


def test_a_label_that_cannot_be_stripped_excludes_the_whole_sidecar(env):  # noqa: F811
    path = _run_with_sidecar(env, 'charter.purpose = "lagoon-simulation"\n' + BODY)
    out, _ = build(env)
    assert any(
        k.startswith(f"sidecar:{path}@") and v == "label_unstrippable"
        for k, v in excluded(out).items()
    )
    assert not any(k.endswith(path) for k in reader_files(out))
    assert not any(b"lagoon-simulation" in v for v in reader_files(out).values())


def test_two_builds_of_a_labelled_sidecar_have_the_same_packet_hash(env):  # noqa: F811
    _run_with_sidecar(env, '[charter]\npurpose = "lagoon-simulation"\n\n' + BODY)
    _, one = build(env, name="one")
    _, two = build(env, name="two")
    assert one["packet_sha256"] == two["packet_sha256"]


# ── validation ─────────────────────────────────────────────────────────────────

_VALID = """
    [experiment]
    hypothesis = "h"
    [outcomes.pass]
    condition = "x < 5"
    decision = "proceed"
    reasoning = "fine"
    [outcomes.fail]
    condition = "x >= 5"
    decision = "debug"
    reasoning = "bad"
    is_residual = true
    [result_schema]
    x = "float"
"""


def _validate(tmp_path: Path, label: str):
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    p = tmp_path / "s.bth.toml"
    p.write_text(textwrap.dedent(_VALID) + label)
    return validate_sidecar(parse_sidecar(p), sidecar_path=p)


def test_a_sidecar_with_a_valid_label_validates(tmp_path):
    res = _validate(tmp_path, '[charter]\npurpose = ["lagoon-simulation", "active-learning"]\n')
    assert res.ok and res.errors == []


def test_a_sidecar_with_a_malformed_label_fails_validation(tmp_path):
    res = _validate(tmp_path, '[charter]\npurpose = "L5"\n')
    assert not res.ok
    assert [e.field for e in res.errors] == ["charter.purpose"]


def test_a_sidecar_without_a_label_is_unaffected(tmp_path):
    assert _validate(tmp_path, "").ok
