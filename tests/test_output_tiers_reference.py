"""Output tiering step 2: reference hashing (spec 261023_output-tiering.md sections 3.1-3.4, 9 row 2).

Every test is built so a plausible wrong implementation fails it; the one the test guards against is named
in its docstring or in a comment at the assertion.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from bathos.catalog import init_catalog
from bathos.checker import check_output_sha_drift
from bathos.compact import _collect_output_metadata
from bathos.output_tiers.reference import (
    OutputTierError,
    check_root_syntax,
    expand_roots,
    fold_adopted,
    hash_file,
    legacy_relpath,
    normalize_relpath,
    reference_hashes,
    resolve_run_id,
    run_end_payload,
)
from bathos.query import _filter_runs_by_output_file
from bathos.runlog.fold_runs import fold_run
from bathos.runner import run_script
from bathos.schema import Run
from bathos.sidecar import SidecarError, parse_sidecar

# --------------------------------------------------------------------------------------------------
# fixtures


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


T0 = "2026-01-01T00:00:00.000000Z"
T1 = "2026-01-01T01:00:00.000000Z"
T2 = "2026-01-01T02:00:00.000000Z"
T3 = "2026-01-01T03:00:00.000000Z"
T4 = "2026-01-01T04:00:00.000000Z"
ROOT = "/work/proj"


def ev(kind: str, data: dict, ts: str, eid: str, run_id: str = "r1", root: str = ROOT) -> dict:
    return {
        "v": 1,
        "eid": eid,
        "kind": kind,
        "entity": [run_id],
        "ts": ts,
        "project": "p",
        "project_id": None,
        "writer": "w",
        "seq": 1,
        "main_root": root,
        "worktree_root": root,
        "origin": "live",
        "data": data,
    }


def entry(rel: str, content: str | None, phase: str | None = None, **extra) -> dict:
    e = {
        "path": f"{ROOT}/{rel}",
        "status": "present",
        "size_bytes": 1,
        "mtime_unix": 1.0,
        "sha256": sha(content) if content is not None else None,
        "relpath": rel,
    }
    if phase:
        e["phase"] = phase
    e.update(extra)
    return e


def run_end(entries: list[dict], ts: str = T1, eid: str = "e_re", **data) -> dict:
    return ev(
        "run.outputs_hashed",
        {"phase": "run_end", "output_metadata": entries, **data},
        ts,
        eid,
    )


def adopted(entries: list[dict], ts: str = T3, eid: str = "e_ad") -> dict:
    return ev("run.outputs_hashed", {"phase": "adopted", "output_metadata": entries}, ts, eid)


def rebaseline(entries: list[dict], ts: str = T4, eid: str = "e_rb") -> dict:
    return ev("run.outputs_hashed", {"phase": "rebaseline", "output_metadata": entries}, ts, eid)


def phaseless(entries: list[dict], ts: str, eid: str) -> dict:
    return ev("run.outputs_hashed", {"output_metadata": entries}, ts, eid)


def legacy_entry(rel: str, content: str | None) -> dict:
    """What a pre-standard run recorded: no relpath, no phase."""
    e = entry(rel, content)
    del e["relpath"]
    return e


def finished(ts: str = T2, eid: str = "e_fin") -> dict:
    return ev("run.finished", {"id": "r1", "status": "completed", "exit_code": 0}, ts, eid)


# --------------------------------------------------------------------------------------------------
# hash_file and the uncapped re-hash


def test_hash_file_matches_hashlib_and_streams(tmp_path: Path):
    f = tmp_path / "f.bin"
    payload = os.urandom(3 * (1 << 20) + 17)  # spans several chunks, ragged tail
    f.write_bytes(payload)
    assert hash_file(f) == hashlib.sha256(payload).hexdigest()


def test_150_mib_file_gets_a_sha_and_drift_check_is_ok_not_unreadable(tmp_path: Path):
    """The old cap (100 MiB) left sha256 null, which the checker read back as UNREADABLE (= drift)."""
    big = tmp_path / "big.bin"
    size = 150 * (1 << 20)
    with open(big, "wb") as f:
        f.truncate(size)  # sparse: reads back as zeros, fast to create

    meta = _collect_output_metadata(str(big))
    assert meta["status"] == "present"
    assert meta["size_bytes"] == size
    expected = hashlib.sha256()
    zeros = bytes(1 << 20)
    for _ in range(150):
        expected.update(zeros)
    assert meta["sha256"] == expected.hexdigest()  # a capped implementation returns None here

    run = Run(
        project_slug="t",
        command="t",
        argv=["t"],
        git_hash="g",
        git_branch="main",
        git_dirty=False,
        output_metadata=json.dumps([{"path": str(big), **meta}]),
    )
    (result,) = check_output_sha_drift(run)
    assert result.status == "OK"


def test_directory_entry_keeps_null_sha(tmp_path: Path):
    d = tmp_path / "somedir"
    d.mkdir()
    meta = _collect_output_metadata(str(d))
    assert meta["status"] == "present"
    assert meta["sha256"] is None
    assert _collect_output_metadata(str(tmp_path / "nope")) == {
        "status": "missing",
        "size_bytes": 0,
    }


# --------------------------------------------------------------------------------------------------
# relpaths and root syntax


def test_normalize_relpath(tmp_path: Path):
    root = tmp_path / "wt"
    assert normalize_relpath(root / "a" / "b.txt", root) == "a/b.txt"
    assert normalize_relpath("a/../a/b.txt", root, base=root) == "a/b.txt"
    assert normalize_relpath("b.txt", root, base=root / "sub") == "sub/b.txt"
    assert normalize_relpath(tmp_path / "elsewhere" / "x", root) is None
    assert normalize_relpath(root, root) is None
    assert (
        normalize_relpath(str(root) + "-sibling/x", root) is None
    )  # prefix match must not fool it


@pytest.mark.parametrize("bad", ["", "/abs/path", "a/../b", "..", ".", "a\\b"])
def test_check_root_syntax_rejects(bad: str):
    assert check_root_syntax(bad) is not None


def test_check_root_syntax_accepts():
    assert check_root_syntax("outputs/validation") is None
    assert check_root_syntax("one.json") is None


# --------------------------------------------------------------------------------------------------
# sidecar [outputs]

_BASE = '[experiment]\nhypothesis = "h"\n[result_schema]\nx = "float"\n'


def _sidecar(tmp_path: Path, extra: str) -> Path:
    p = tmp_path / "s.bth.toml"
    p.write_text(_BASE + extra)
    return p


def test_sidecar_without_outputs_table_has_none(tmp_path: Path):
    assert parse_sidecar(_sidecar(tmp_path, "")).outputs is None


def test_sidecar_outputs_roots_parse(tmp_path: Path):
    sc = parse_sidecar(_sidecar(tmp_path, '[outputs]\nroots = ["outputs/validation", "r.json"]\n'))
    assert sc.outputs is not None
    assert sc.outputs.roots == ("outputs/validation", "r.json")


def test_sidecar_outputs_is_distinct_from_observe_outputs(tmp_path: Path):
    sc = parse_sidecar(_sidecar(tmp_path, '[observe]\noutputs = ["obs.json"]\n'))
    assert sc.outputs is None
    assert sc.observe is not None
    assert sc.observe.outputs == ["obs.json"]


@pytest.mark.parametrize(
    "extra, needle",
    [
        ('[outputs]\nroots = ["a"]\nroot = ["typo"]\n', "unknown key"),
        ('[outputs]\nroots = ["/abs"]\n', "absolute"),
        ('[outputs]\nroots = ["a/../../b"]\n', "'..'"),
        ('[outputs]\nroots = "a"\n', "list"),
        ("[outputs]\nroots = [1]\n", "string"),
        ('outputs = "x"\n', "table"),
    ],
)
def test_sidecar_outputs_refuses(tmp_path: Path, extra: str, needle: str):
    # `outputs = "x"` must sit before any table header to be top-level
    text = _BASE + extra if not extra.startswith("outputs =") else extra + _BASE
    p = tmp_path / "s.bth.toml"
    p.write_text(text)
    with pytest.raises(SidecarError, match=needle):
        parse_sidecar(p)


# --------------------------------------------------------------------------------------------------
# root expansion


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "out" / "cells").mkdir(parents=True)
    (root / "out" / "a.json").write_text("a")
    (root / "out" / "cells" / "c1.npy").write_text("c1")
    (root / "out" / "cells" / "c2.npy").write_text("c2")
    (root / "single.txt").write_text("s")
    return root


def test_expand_roots_files_and_dirs_sorted_and_deduped(tmp_path: Path):
    root = _tree(tmp_path)
    files, missing = expand_roots(["out", "single.txt", "out/cells"], root)
    assert [f.relpath for f in files] == [
        "out/a.json",
        "out/cells/c1.npy",
        "out/cells/c2.npy",
        "single.txt",
    ]
    assert missing == []
    assert all(f.path.is_file() for f in files)


def test_expand_roots_reports_missing_root_without_refusing(tmp_path: Path):
    root = _tree(tmp_path)
    files, missing = expand_roots(["out/a.json", "never_written"], root)
    assert [f.relpath for f in files] == ["out/a.json"]
    assert missing == ["never_written"]


def test_symlink_root_refuses(tmp_path: Path):
    root = _tree(tmp_path)
    (root / "link").symlink_to(root / "single.txt")
    with pytest.raises(OutputTierError) as exc:
        expand_roots(["link"], root)
    assert exc.value.error_code == "symlink_in_outputs"


def test_symlink_inside_a_root_refuses(tmp_path: Path):
    root = _tree(tmp_path)
    (root / "out" / "cells" / "alias.npy").symlink_to(root / "out" / "a.json")
    with pytest.raises(OutputTierError) as exc:
        expand_roots(["out"], root)
    assert exc.value.error_code == "symlink_in_outputs"
    assert "alias.npy" in exc.value.detail


def test_dangling_symlink_inside_a_root_refuses(tmp_path: Path):
    root = _tree(tmp_path)
    (root / "out" / "dangling").symlink_to(root / "does-not-exist")
    with pytest.raises(OutputTierError) as exc:
        expand_roots(["out"], root)
    assert exc.value.error_code == "symlink_in_outputs"


def test_symlinked_directory_inside_a_root_refuses(tmp_path: Path):
    root = _tree(tmp_path)
    (root / "out" / "cells_link").symlink_to(root / "out" / "cells", target_is_directory=True)
    with pytest.raises(OutputTierError) as exc:
        expand_roots(["out"], root)
    assert exc.value.error_code == "symlink_in_outputs"


def test_root_through_a_symlinked_ancestor_refuses(tmp_path: Path):
    """`via/a.json` is a regular file, but `via` points outside the worktree: it must not be hashed."""
    root = _tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.json").write_text("escaped")
    (root / "via").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OutputTierError) as exc:
        expand_roots(["via/a.json"], root)
    assert exc.value.error_code == "symlink_in_outputs"
    with pytest.raises(OutputTierError):
        expand_roots(["via"], root)


def test_fifo_under_a_root_refuses_as_special_file(tmp_path: Path):
    root = _tree(tmp_path)
    os.mkfifo(root / "out" / "pipe")
    with pytest.raises(OutputTierError) as exc:
        expand_roots(["out"], root)
    assert exc.value.error_code == "special_file_in_outputs"
    os.mkfifo(root / "toppipe")
    with pytest.raises(OutputTierError) as exc2:
        expand_roots(["toppipe"], root)
    assert exc2.value.error_code == "special_file_in_outputs"


# --------------------------------------------------------------------------------------------------
# run-end payload


def test_run_end_payload_keeps_legacy_keys_and_adds_relpath_and_phase(tmp_path: Path):
    root = _tree(tmp_path)
    payload = run_end_payload(
        [str(root / "single.txt"), "out/a.json"], ["out/cells"], fs_root=root, cwd=root
    )
    assert payload["phase"] == "run_end"
    assert "refused" not in payload
    by_rel = {e["relpath"]: e for e in payload["output_metadata"]}
    assert sorted(by_rel) == ["out/a.json", "out/cells/c1.npy", "out/cells/c2.npy", "single.txt"]
    for e in payload["output_metadata"]:
        assert {"path", "status", "size_bytes", "mtime_unix", "sha256", "relpath", "phase"} <= set(
            e
        )
        assert e["phase"] == "run_end"
        assert e["status"] == "present"
    assert by_rel["single.txt"]["sha256"] == sha("s")
    assert by_rel["single.txt"]["path"] == str(root / "single.txt")  # `path` stays as recorded
    assert by_rel["out/a.json"]["path"] == "out/a.json"
    assert by_rel["out/cells/c1.npy"]["sha256"] == sha("c1")


def test_run_end_payload_relative_output_paths_resolve_against_cwd_not_process_cwd(tmp_path: Path):
    root = _tree(tmp_path)
    payload = run_end_payload(["a.json"], [], fs_root=root, cwd=root / "out")
    (e,) = payload["output_metadata"]
    assert e["relpath"] == "out/a.json"
    assert e["sha256"] == sha("a")


def test_run_end_payload_path_outside_fs_root_has_no_relpath(tmp_path: Path):
    root = _tree(tmp_path)
    stray = tmp_path / "stray.txt"
    stray.write_text("z")
    payload = run_end_payload([str(stray)], [], fs_root=root, cwd=root)
    (e,) = payload["output_metadata"]
    assert e["relpath"] is None
    assert e["sha256"] == sha("z")


def test_run_end_payload_overlap_between_paths_and_roots_lists_a_file_once(tmp_path: Path):
    root = _tree(tmp_path)
    payload = run_end_payload([str(root / "out" / "a.json")], ["out"], fs_root=root, cwd=root)
    rels = [e["relpath"] for e in payload["output_metadata"]]
    assert rels.count("out/a.json") == 1


def test_run_end_payload_refusal_is_a_tombstone_with_no_entries(tmp_path: Path):
    root = _tree(tmp_path)
    (root / "out" / "bad").symlink_to(root / "single.txt")
    payload = run_end_payload([str(root / "single.txt")], ["out"], fs_root=root, cwd=root)
    assert payload["phase"] == "run_end"
    assert payload["refused"] == "symlink_in_outputs"
    assert payload["output_metadata"] == []  # not even the legitimately declared output_paths entry


def test_run_end_payload_missing_root_is_recorded_as_missing(tmp_path: Path):
    root = _tree(tmp_path)
    payload = run_end_payload([], ["not_written.json"], fs_root=root, cwd=root)
    (e,) = payload["output_metadata"]
    assert e["relpath"] == "not_written.json"
    assert e["status"] == "missing"
    assert not e.get("sha256")


# --------------------------------------------------------------------------------------------------
# reference_hashes: the section 3.3 precedence


def rel_shas(ref: dict) -> dict[str, str]:
    return {k: v.sha256 for k, v in ref.items()}


def test_run_end_entries_are_the_reference():
    ref = reference_hashes([run_end([entry("a", "A"), entry("b", "B")]), finished()])
    assert rel_shas(ref) == {"a": sha("A"), "b": sha("B")}
    assert {v.phase for v in ref.values()} == {"run_end"}
    assert ref["a"].origin_eid == "e_re"
    assert ref["a"].worktree_root == ROOT


def test_rebaseline_never_counts():
    # A rebaseline alone leaves the run with no reference at all.
    assert reference_hashes([finished(), rebaseline([entry("a", "TAMPERED")])]) == {}
    # Beside a run_end, it must not replace it, however late it is.
    ref = reference_hashes(
        [run_end([entry("a", "A")]), finished(), rebaseline([entry("a", "TAMPERED")])]
    )
    assert rel_shas(ref) == {"a": sha("A")}


def test_adopted_fills_only_paths_without_a_run_end_entry():
    ref = reference_hashes(
        [
            run_end([entry("a", "A")]),
            finished(),
            adopted([entry("a", "ADOPTED-A"), entry("cells/c1", "C1", phase="adopted")]),
        ]
    )
    assert rel_shas(ref) == {"a": sha("A"), "cells/c1": sha("C1")}
    assert ref["a"].phase == "run_end"
    assert ref["cells/c1"].phase == "adopted"


def test_run_end_beats_legacy_beats_adopted_per_path():
    events = [
        ev("run.imported", {"output_metadata": [legacy_entry("p", "IMPORTED-P")]}, T0, "e_imp"),
        run_end([entry("p", "RUN-END-P")]),
        adopted([entry("p", "ADOPTED-P"), entry("q", "ADOPTED-Q")]),
    ]
    ref = reference_hashes(events)
    assert ref["p"].sha256 == sha("RUN-END-P")
    assert ref["q"].sha256 == sha("ADOPTED-Q")
    assert ref["p"].phase == "run_end" and ref["q"].phase == "adopted"


def test_legacy_run_imported_wins_over_a_phaseless_event():
    events = [
        ev("run.imported", {"output_metadata": [legacy_entry("a", "IMPORTED")]}, T0, "e_imp"),
        phaseless([legacy_entry("a", "PHASELESS-BEFORE-FINISH")], T1, "e_pl"),
        finished(ts=T2),
    ]
    ref = reference_hashes(events)
    assert ref["a"].sha256 == sha("IMPORTED")
    assert ref["a"].phase == "run_end_legacy"


def test_legacy_run_imported_output_metadata_may_be_json_text():
    """The warm importer carries `output_metadata` as the VARCHAR it was stored as."""
    text = json.dumps([legacy_entry("a", "IMPORTED")])
    ref = reference_hashes([ev("run.imported", {"output_metadata": text}, T0, "e_imp")])
    assert rel_shas(ref) == {"a": sha("IMPORTED")}


def test_legacy_picks_the_earliest_phaseless_event_before_run_finished_never_a_later_rebaseline():
    events = [
        phaseless([legacy_entry("a", "RUN-END")], T1, "e_run_end"),
        finished(ts=T2),
        # a pre-Step-2 `bth check --rebaseline`: phase-less, after run.finished
        phaseless([legacy_entry("a", "REBASELINE")], T3, "e_old_rebaseline"),
    ]
    ref = reference_hashes(events)
    assert rel_shas(ref) == {
        "a": sha("RUN-END")
    }  # picking the latest phase-less event gives REBASELINE
    assert ref["a"].phase == "run_end_legacy"

    # Only a post-finish phase-less event exists: it is a rebaseline, not a reference.
    assert (
        reference_hashes(
            [finished(ts=T1), phaseless([legacy_entry("a", "REBASELINE")], T3, "e_rb")]
        )
        == {}
    )


def test_legacy_earliest_of_several_phaseless_events_before_finish():
    events = [
        phaseless([legacy_entry("a", "SECOND")], T1, "e2"),
        phaseless([legacy_entry("a", "FIRST")], T0, "e1"),
        finished(ts=T2),
    ]
    assert reference_hashes(events)["a"].sha256 == sha("FIRST")


def test_null_sha_legacy_entry_counts_as_absent_so_adopted_can_cover_it():
    events = [
        phaseless([legacy_entry("big", None), legacy_entry("small", "S")], T1, "e_pl"),
        finished(ts=T2),
        adopted(
            [entry("big", "BIG", phase="adopted"), entry("small", "OVERRIDE", phase="adopted")]
        ),
    ]
    ref = reference_hashes(events)
    assert ref["big"].sha256 == sha("BIG") and ref["big"].phase == "adopted"
    # a legacy entry that has a sha is never overridden
    assert ref["small"].sha256 == sha("S") and ref["small"].phase == "run_end_legacy"


def test_null_sha_run_end_entry_is_absent():
    assert reference_hashes([run_end([entry("d", None)])]) == {}


def test_tombstone_run_end_contributes_nothing():
    assert reference_hashes([run_end([], refused="symlink_in_outputs"), finished()]) == {}


def test_legacy_relpath_rules():
    assert legacy_relpath(f"{ROOT}/outputs/x.json", ROOT) == "outputs/x.json"
    assert legacy_relpath("outputs/x.json", ROOT) == "outputs/x.json"
    assert legacy_relpath("/elsewhere/x.json", ROOT) is None
    assert legacy_relpath("../x.json", ROOT) is None
    assert legacy_relpath("", ROOT) is None


def test_legacy_entries_get_their_relpath_from_the_event_worktree_root():
    ref = reference_hashes(
        [phaseless([legacy_entry("outputs/x.json", "X")], T1, "e_pl"), finished()]
    )
    assert set(ref) == {"outputs/x.json"}
    assert ref["outputs/x.json"].worktree_root == ROOT


def test_adopted_retraction_drops_entries_and_older_adopt_resurfaces():
    first = adopted([entry("c", "FIRST", phase="adopted")], ts=T2, eid="ad1")
    second = adopted([entry("c", "SECOND", phase="adopted")], ts=T3, eid="ad2")
    retract = ev("run.outputs_adopt_retracted", {"retracts": "ad2", "reason": "oops"}, T4, "e_ret")
    assert reference_hashes([first, second])["c"].sha256 == sha("SECOND")
    assert reference_hashes([first, second, retract])["c"].sha256 == sha("FIRST")
    retract_all = ev("run.outputs_adopt_retracted", {"retracts": "ad1"}, T4, "e_ret1")
    assert reference_hashes([first, second, retract, retract_all]) == {}


def test_reference_hashes_is_order_independent():
    events = [
        ev("run.imported", {"output_metadata": [legacy_entry("l", "L")]}, T0, "e_imp"),
        run_end([entry("a", "A")]),
        finished(),
        adopted([entry("c", "C", phase="adopted")]),
        rebaseline([entry("a", "TAMPERED")]),
        ev("run.outputs_adopt_retracted", {"retracts": "nothing"}, T4, "e_r"),
    ]
    expected = rel_shas(reference_hashes(events))
    assert expected == {"l": sha("L"), "a": sha("A"), "c": sha("C")}
    for perm in itertools.islice(itertools.permutations(events), 120):
        assert rel_shas(reference_hashes(list(perm))) == expected


# --------------------------------------------------------------------------------------------------
# prefix resolution


def test_resolve_run_id_prefix():
    ids = ["1e4596c6-aaaa", "1e4a0000-bbbb", "989c3174-cccc"]
    assert resolve_run_id("989c", ids) == "989c3174-cccc"
    assert resolve_run_id("1e4596", ids) == "1e4596c6-aaaa"
    assert resolve_run_id("1e4596c6-aaaa", ids + ["1e4596c6-aaaa"]) == "1e4596c6-aaaa"
    with pytest.raises(OutputTierError) as none_:
        resolve_run_id("ffff", ids)
    assert none_.value.error_code == "run_not_found"
    with pytest.raises(OutputTierError) as two:
        resolve_run_id("1e4", ids)
    assert two.value.error_code == "run_prefix_ambiguous"
    with pytest.raises(OutputTierError):
        resolve_run_id("", ids)


# --------------------------------------------------------------------------------------------------
# the fold: adopt events never reach output_metadata


def _fold(*events: dict) -> dict:
    started = ev(
        "run.started",
        {
            "project_slug": "proj",
            "command": "x",
            "argv": ["x"],
            "git_hash": "g",
            "git_branch": "main",
            "git_dirty": False,
            "campaign_id": None,
            "agent_mode": "manual",
        },
        T0,
        "e_start",
    )
    return fold_run([started, finished(), *events])


def test_adopt_after_run_end_leaves_output_metadata_holding_the_run_end_entries():
    run_end_entries = [entry("a", "A", phase="run_end"), entry("b", "B", phase="run_end")]
    row = _fold(
        run_end(run_end_entries),
        adopted([entry("cells/c1", "C1", phase="adopted")], ts=T3),
    )
    # Latest-wins over all outputs_hashed events would make this the adopted list alone.
    assert row["output_metadata"] == run_end_entries
    assert [e["relpath"] for e in json.loads(row["adopted_outputs"])] == ["cells/c1"]


def test_adopted_only_run_has_no_output_metadata():
    row = _fold(adopted([entry("c", "C", phase="adopted")]))
    assert row["output_metadata"] is None
    assert [e["sha256"] for e in json.loads(row["adopted_outputs"])] == [sha("C")]


def test_adopted_outputs_is_latest_non_retracted_per_relpath():
    first = adopted(
        [entry("c", "FIRST", phase="adopted"), entry("d", "D", phase="adopted")], ts=T2, eid="ad1"
    )
    second = adopted([entry("c", "SECOND", phase="adopted")], ts=T3, eid="ad2")
    got = {e["relpath"]: e["sha256"] for e in json.loads(_fold(first, second)["adopted_outputs"])}
    assert got == {"c": sha("SECOND"), "d": sha("D")}
    retract = ev("run.outputs_adopt_retracted", {"retracts": "ad2"}, T4, "e_ret")
    got = {
        e["relpath"]: e["sha256"]
        for e in json.loads(_fold(first, second, retract)["adopted_outputs"])
    }
    assert got == {"c": sha("FIRST"), "d": sha("D")}
    assert _fold(first, retract_of(first))["adopted_outputs"] is None


def retract_of(adopt_event: dict) -> dict:
    return ev("run.outputs_adopt_retracted", {"retracts": adopt_event["eid"]}, T4, "e_ret_of")


def test_rebaseline_keeps_its_latest_wins_effect_on_output_metadata():
    """Spec 3.3: rebaseline still replaces output_metadata (owner bug 12.3) but is not a reference."""
    run_end_entries = [entry("a", "A", phase="run_end")]
    new = [entry("a", "A2")]
    row = _fold(run_end(run_end_entries), rebaseline(new))
    assert row["output_metadata"] == new
    ref = reference_hashes([run_end(run_end_entries), rebaseline(new), finished()])
    assert ref["a"].sha256 == sha("A")


def test_fold_adopted_ignores_event_order():
    a = adopted([entry("c", "FIRST", phase="adopted")], ts=T2, eid="ad1")
    b = adopted([entry("c", "SECOND", phase="adopted")], ts=T3, eid="ad2")
    assert fold_adopted([a, b]) == fold_adopted([b, a])


# --------------------------------------------------------------------------------------------------
# new-shape record behaves like a legacy one for the checker and `bth find --output-file`


def _runs_with_both_shapes(tmp_path: Path) -> tuple[Run, Run, Path]:
    f = tmp_path / "result.json"
    f.write_text("payload")
    meta = _collect_output_metadata(str(f))
    legacy = [{"path": str(f), **meta}]
    new = run_end_payload([str(f)], [], fs_root=tmp_path, cwd=tmp_path)["output_metadata"]

    def mk(entries: list[dict]) -> Run:
        return Run(
            project_slug="t",
            command="t",
            argv=["t"],
            git_hash="g",
            git_branch="main",
            git_dirty=False,
            output_metadata=json.dumps(entries),
        )

    return mk(legacy), mk(new), f


def test_check_output_sha_drift_same_on_new_and_legacy_shape(tmp_path: Path):
    legacy_run, new_run, f = _runs_with_both_shapes(tmp_path)

    def summary(run: Run):
        return [
            (r.status, r.recorded_sha256, r.current_sha256) for r in check_output_sha_drift(run)
        ]

    assert summary(legacy_run) == summary(new_run)
    assert summary(new_run)[0][0] == "OK"

    f.write_text("mutated")
    assert summary(legacy_run) == summary(new_run)
    assert summary(new_run)[0][0] == "DRIFT"

    f.unlink()
    assert summary(legacy_run) == summary(new_run)
    assert summary(new_run)[0][0] == "MISSING"


def test_find_output_file_same_on_new_and_legacy_shape(tmp_path: Path):
    legacy_run, new_run, _ = _runs_with_both_shapes(tmp_path)
    for pattern in ("*result.json", "*nomatch*"):
        got_legacy = _filter_runs_by_output_file([legacy_run], pattern=pattern)
        got_new = _filter_runs_by_output_file([new_run], pattern=pattern)
        assert len(got_legacy) == len(got_new)
    assert len(_filter_runs_by_output_file([new_run], pattern="*result.json")) == 1
    assert _filter_runs_by_output_file([new_run], pattern="*nomatch*") == []


# --------------------------------------------------------------------------------------------------
# runner: the run-end emit, end to end in log mode

_SCRIPT = textwrap.dedent("""
    import json, os
    from pathlib import Path
    cwd = Path.cwd()
    (cwd / "outputs" / "data").mkdir(parents=True, exist_ok=True)
    (cwd / "outputs" / "data" / "a.bin").write_text("AAA")
    (cwd / "outputs" / "data" / "b.bin").write_text("BBB")
    (cwd / "extra.txt").write_text("EXTRA")
    if os.environ.get("MAKE_SYMLINK"):
        (cwd / "outputs" / "data" / "alias").symlink_to(cwd / "extra.txt")
    p = os.environ.get("BTH_RESULTS_PATH")
    if p:
        json.dump({"x": 1.0}, open(p, "w"))
""")


def _sidecar_text(roots: list[str] | None) -> str:
    text = textwrap.dedent("""
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "x > 0"
        decision = "go"
        reasoning = "r"
        [outcomes.fail]
        condition = "x <= 0"
        decision = "stop"
        reasoning = "r"
        is_residual = true
        [result_schema]
        x = "float"
    """)
    if roots is not None:
        text += f"[outputs]\nroots = {json.dumps(roots)}\n"
    return text


def _read_events(home: Path) -> list[dict]:
    log_dir = home / ".bth" / "log" / "unaffiliated"
    out: list[dict] = []
    for f in sorted(log_dir.glob("*.jsonl")):
        out.extend(json.loads(line) for line in f.read_text().splitlines() if line.strip())
    return out


@pytest.fixture
def logmode(tmp_catalog: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BTH_LOG_MODE", "1")
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_catalog))
    monkeypatch.delenv("BTH_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    init_catalog(tmp_catalog)
    return tmp_catalog


def _run(
    tmp_path: Path, catalog: Path, roots: list[str] | None, output_paths: list[str] | None = None
) -> int:
    cwd = tmp_path / "wt"
    scripts = cwd / "scripts" / "explore"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "gen.py").write_text(_SCRIPT)
    (scripts / "gen.bth.toml").write_text(_sidecar_text(roots))
    return run_script(
        argv=[sys.executable, str(scripts / "gen.py")],
        project_slug="testproj",
        catalog_dir=catalog,
        output_paths=output_paths or [],
        tags=[],
        cwd=cwd,
    )


def test_run_end_event_has_legacy_keys_relpath_phase_and_expanded_roots(
    logmode: Path, tmp_path: Path, bathos_test_home: Path
):
    rc = _run(
        tmp_path, logmode, ["outputs/data"], output_paths=[str(tmp_path / "wt" / "extra.txt")]
    )
    assert rc == 0
    events = _read_events(bathos_test_home)
    started = next(e for e in events if e["kind"] == "run.started")
    assert started["data"]["outputs_declared"] is True

    (hashed,) = [e for e in events if e["kind"] == "run.outputs_hashed"]
    assert hashed["data"]["phase"] == "run_end"
    entries = {e["relpath"]: e for e in hashed["data"]["output_metadata"]}
    assert "refused" not in hashed["data"]
    assert {"extra.txt", "outputs/data/a.bin", "outputs/data/b.bin"} <= set(entries)
    for e in entries.values():
        assert {"path", "status", "size_bytes", "mtime_unix", "sha256", "relpath", "phase"} <= set(
            e
        )
        assert e["phase"] == "run_end"
    assert entries["outputs/data/a.bin"]["sha256"] == sha("AAA")
    assert entries["extra.txt"]["sha256"] == sha("EXTRA")

    # run.finished follows the run-end event
    finished_ev = next(e for e in events if e["kind"] == "run.finished")
    assert finished_ev["ts"] >= hashed["ts"]

    # the event is a usable reference set as it stands
    ref = reference_hashes([e for e in events if e["entity"] == hashed["entity"]])
    assert ref["outputs/data/b.bin"].sha256 == sha("BBB")
    assert ref["outputs/data/b.bin"].phase == "run_end"


def test_run_without_declared_roots_is_not_outputs_declared_but_still_tagged(
    logmode: Path, tmp_path: Path, bathos_test_home: Path
):
    rc = _run(tmp_path, logmode, None, output_paths=[str(tmp_path / "wt" / "extra.txt")])
    assert rc == 0
    events = _read_events(bathos_test_home)
    started = next(e for e in events if e["kind"] == "run.started")
    assert "outputs_declared" not in started["data"]
    (hashed,) = [e for e in events if e["kind"] == "run.outputs_hashed"]
    assert hashed["data"]["phase"] == "run_end"
    by_rel = {e["relpath"]: e for e in hashed["data"]["output_metadata"]}
    # `--output-paths` (absolute) and the auto-discovered BTH_OUTPUT_DIR files are normalised to relpath
    assert by_rel["extra.txt"]["path"] == str(tmp_path / "wt" / "extra.txt")
    assert by_rel["extra.txt"]["sha256"] == sha("EXTRA")
    # roots were not declared, so the files the script wrote outside --output-paths are not hashed
    assert not any(r.startswith("outputs/data") for r in by_rel)


def test_symlink_under_a_declared_root_records_a_tombstone_and_the_run_still_finishes(
    logmode: Path, tmp_path: Path, bathos_test_home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MAKE_SYMLINK", "1")
    rc = _run(
        tmp_path, logmode, ["outputs/data"], output_paths=[str(tmp_path / "wt" / "extra.txt")]
    )
    assert rc == 0
    events = _read_events(bathos_test_home)
    (hashed,) = [e for e in events if e["kind"] == "run.outputs_hashed"]
    assert hashed["data"]["phase"] == "run_end"
    assert hashed["data"]["refused"] == "symlink_in_outputs"
    assert "alias" in hashed["data"]["refused_detail"]
    assert hashed["data"]["output_metadata"] == []
    assert any(e["kind"] == "run.finished" for e in events)
    # outputs_declared + tombstone: no reference set
    assert reference_hashes([e for e in events if e["entity"] == hashed["entity"]]) == {}


def test_declared_roots_make_the_run_end_emit_hard_fail_and_failure_is_not_swallowed(
    logmode: Path, tmp_path: Path, bathos_test_home: Path, monkeypatch: pytest.MonkeyPatch
):
    import bathos.runner as runner_mod
    from bathos.runlog.emit import RunNotLaunchedError

    real = runner_mod.emit_event
    seen: dict[str, bool] = {}

    def spy(**kwargs):
        if kwargs["kind"] == "run.outputs_hashed":
            seen["hard_fail"] = kwargs.get("hard_fail", False)
            raise RunNotLaunchedError("simulated log failure")
        return real(**kwargs)

    monkeypatch.setattr(runner_mod, "emit_event", spy)
    rc = _run(tmp_path, logmode, ["outputs/data"])
    assert seen == {"hard_fail": True}
    assert rc != 0  # the script exited 0; the lost run-end event must not look like success
    events = _read_events(bathos_test_home)
    assert not any(e["kind"] == "run.outputs_hashed" for e in events)
    assert any(e["kind"] == "run.finished" for e in events)  # the run does not stay 'running'


def test_without_declared_roots_the_run_end_emit_stays_best_effort(
    logmode: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import bathos.runner as runner_mod

    real = runner_mod.emit_event
    seen: dict[str, bool] = {}

    def spy(**kwargs):
        if kwargs["kind"] == "run.outputs_hashed":
            seen["hard_fail"] = kwargs.get("hard_fail", False)
        return real(**kwargs)

    monkeypatch.setattr(runner_mod, "emit_event", spy)
    assert _run(tmp_path, logmode, None) == 0
    assert seen == {"hard_fail": False}


# --------------------------------------------------------------------------------------------------
# checker: rebaseline is tagged


def test_rebaseline_event_is_tagged_phase_rebaseline(
    tmp_path: Path, bathos_test_home: Path, monkeypatch
):
    from bathos.checker import rebaseline_run_outputs

    monkeypatch.setenv("BTH_LOG_MODE", "1")
    catalog = tmp_path / "cat"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    init_catalog(catalog)
    f = tmp_path / "o.txt"
    f.write_text("v1")
    run = Run(
        project_slug="t",
        command="t",
        argv=["t"],
        git_hash="g",
        git_branch="main",
        git_dirty=False,
        output_paths=[str(f)],
    )
    rebaseline_run_outputs(catalog, run, cwd=tmp_path)
    events = [e for e in _read_events(bathos_test_home) if e["kind"] == "run.outputs_hashed"]
    assert len(events) == 1
    assert events[0]["data"]["phase"] == "rebaseline"
    # and is therefore never a reference
    assert reference_hashes(events) == {}
