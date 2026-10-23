"""Output tiering step 3: `bth outputs hash` / `adopt` (+ `--retract`, `--dry-run`)
(spec 261023_output-tiering.md sections 3.3-3.4, 4, 9 row 3).

Built on a real log-mode catalog: events are written as run-log segments, folded by `run_ingest`, and the commands
under test go through the real tool functions (and, for two tests, the cyclopts CLI and the MCP wrapper), so a
fold or wiring gap fails here and not only in isolation. Each test names the broken implementation it guards
against in its docstring or at the assertion.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from bathos.cli_cyclopts import app
from bathos.index import connect_read
from bathos.mcp import mcp_outputs_adopt_tool, outputs_adopt_tool, outputs_hash_tool
from bathos.output_tiers.reference import read_adopted_outputs, reference_hashes
from bathos.output_tiers.run_context import read_run_events
from bathos.runlog.envelope import build_envelope
from bathos.runlog.ingest import run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root
from tests._cyclopts_runner import CyclopticRunner

T0 = "2026-01-01T00:00:00.000000Z"
T1 = "2026-01-01T01:00:00.000000Z"
T2 = "2026-01-01T02:00:00.000000Z"
RID = "11111111-aaaa-4aaa-8aaa-111111111111"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    (path / ".bth.toml").write_text(f'[project]\nslug = "repo"\nroot = "{path}"\n')
    return path


class World:
    """A git repo, a log-mode catalog, and helpers that append hand-built run events and fold them."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = _git_repo(tmp_path / "repo")
        assign_project_id(self.repo)
        register_main_root(self.repo)
        self.pid = read_project_id(self.repo / ".bth.toml")
        self.catalog = tmp_path / "catalog"
        self.catalog.mkdir()
        cutover_marker_path(self.catalog).write_text(
            json.dumps({"at": T0, "bathos": "test", "attempt": "1", "segments": []})
        )
        self.root = self.repo  # the run's worktree; a test may point it at a linked worktree
        self.elsewhere = tmp_path / "elsewhere"
        self.elsewhere.mkdir()
        self._seq = 0
        self._lines: list[str] = []

    # --- files ---
    def write(self, rel: str, content: bytes) -> str:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return sha(content)

    def entry(self, rel: str, *, with_sha: bool = True, phase: str | None = None) -> dict:
        p = self.root / rel
        e = {
            "path": str(p),
            "status": "present",
            "size_bytes": p.stat().st_size,
            "mtime_unix": 1.0,
            "sha256": sha(p.read_bytes()) if with_sha else None,
        }
        if phase:
            e |= {"relpath": rel, "phase": phase}
        return e

    # --- events ---
    def add(self, kind: str, data: dict, ts: str, eid: str, run_id: str = RID, wroot=None) -> None:
        self._seq += 1
        env = build_envelope(
            kind=kind,
            entity=[run_id],
            data=data,
            main_root=self.repo,
            worktree_root=wroot or self.root,
            project="repo",
            project_id=self.pid,
            writer="w1",
            seq=self._seq,
            ts=ts,
            eid=eid,
        )
        self._lines.append(json.dumps(env, sort_keys=True) + "\n")

    def started(self, run_id: str = RID, *, declared: bool = False) -> None:
        data = {
            "id": run_id,
            "project_slug": "repo",
            "command": "python x.py",
            "argv": ["python", "x.py"],
            "git_hash": "aaa",
            "git_branch": "main",
            "git_dirty": False,
            "timestamp": T0,
            "agent_mode": "manual",
        }
        if declared:
            data["outputs_declared"] = True
        self.add("run.started", data, T0, f"e-start-{run_id[9:13]}", run_id)

    def finished(self, run_id: str = RID, status: str = "completed") -> None:
        self.add(
            "run.finished",
            {
                "id": run_id,
                "status": status,
                "exit_code": 0,
                "duration_s": 1.0,
                "output_paths": [],
                "outcome": "pass",
                "outcome_error_reason": "",
                "outcome_is_residual": False,
                "adversarial_check_status": "",
                "timestamp": T2,
            },
            T2,
            f"e-fin-{run_id[9:13]}",
            run_id,
        )

    def run_end(
        self, entries: list[dict], run_id: str = RID, *, phase: str | None = "run_end"
    ) -> None:
        data: dict = {"output_metadata": entries}
        if phase:
            data["phase"] = phase
        self.add("run.outputs_hashed", data, T1, f"e-hash-{run_id[9:13]}", run_id)

    def imported(self, entries: list[dict], run_id: str = RID, status: str = "completed") -> None:
        self.add(
            "run.imported",
            {
                "id": run_id,
                "project_slug": "repo",
                "command": "python x.py",
                "argv": ["python", "x.py"],
                "status": status,
                "timestamp": T0,
                "output_metadata": json.dumps(entries),
            },
            T0,
            f"e-imp-{run_id[9:13]}",
            run_id,
            wroot=self.catalog,  # the importer's default: not a real tree
        )

    def ingest(self) -> None:
        log = self.repo / ".bth" / "log"
        log.mkdir(parents=True, exist_ok=True)
        (log / "seg-fixture.jsonl").write_text("".join(self._lines))
        run_ingest(self.catalog)

    # --- reads ---
    def events(self, run_id: str = RID) -> list[dict]:
        con = connect_read(self.catalog)
        try:
            return read_run_events(con, run_id)
        finally:
            con.close()

    def adopted_outputs(self, run_id: str = RID) -> list[dict]:
        con = connect_read(self.catalog)
        try:
            return read_adopted_outputs(con, run_id)
        finally:
            con.close()

    def log_bytes(self) -> int:
        """Total size of every run-log segment the commands could write: the project log and the mirror."""
        total = 0
        for root in (self.repo / ".bth" / "log", Path.home() / ".bth" / "log-mirror"):
            if root.exists():
                total += sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        return total

    # --- tool calls (adopter cwd is NOT the run's worktree) ---
    def adopt(self, *roots: str, run_id: str = RID, reason: str = "test adopt", **kw) -> dict:
        return outputs_adopt_tool(
            run_id=run_id, root=list(roots), reason=reason, catalog_dir=str(self.catalog), **kw
        )

    def hash(self, run_id: str = RID, **kw) -> dict:
        return outputs_hash_tool(run_id=run_id, catalog_dir=str(self.catalog), **kw)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    w = World(tmp_path)
    monkeypatch.chdir(w.elsewhere)  # never the run's worktree: a cwd-derived root would be wrong
    return w


def legacy_run(w: World, *, with_cells: bool = True) -> dict[str, str]:
    """A pre-standard finished run: one phase-less run-end entry with a sha (`a.json`) and one with a null sha
    (`big.bin`, as a >=100 MiB legacy file would be), plus unreferenced `cells/` files on disk."""
    shas = {
        "a.json": w.write("out/a.json", b"alpha"),
        "big.bin": w.write("out/big.bin", b"BIG" * 9),
    }
    if with_cells:
        shas["cells/c1.npy"] = w.write("out/cells/c1.npy", b"cell-1")
        shas["cells/c2.npy"] = w.write("out/cells/c2.npy", b"cell-22")
    w.started()
    w.run_end(
        [w.entry("out/a.json"), w.entry("out/big.bin", with_sha=False)],
        phase=None,
    )
    w.finished()
    w.ingest()
    return shas


# --------------------------------------------------------------------------------------------------
# adopt: the happy path and what the event records


def test_adopt_records_only_unreferenced_paths_and_stamps_the_runs_root(world: World):
    """Adopt covers `cells/` and the null-sha `big.bin`, leaves the sha-bearing legacy `a.json` alone, and the event
    carries the RUN's worktree_root (the adopter ran from elsewhere): a cwd-stamped event would fail this."""
    legacy_run(world)

    result = world.adopt("out/cells", "out/big.bin", "out/a.json")

    assert "error" not in result, result
    assert sorted(result["files"]) == ["out/big.bin", "out/cells/c1.npy", "out/cells/c2.npy"]
    assert result["skipped"] == [{"relpath": "out/a.json", "reason": "already_run_end_legacy"}]
    assert result["total_bytes"] == 27 + 6 + 7
    adopted = [
        e
        for e in world.events()
        if e["kind"] == "run.outputs_hashed" and e["data"].get("phase") == "adopted"
    ]
    assert len(adopted) == 1
    assert adopted[0]["eid"] == result["eid"]
    assert adopted[0]["worktree_root"] == str(world.repo)
    assert adopted[0]["data"]["reason"] == "test adopt"
    assert {e["relpath"] for e in adopted[0]["data"]["output_metadata"]} == set(result["files"])
    ref = reference_hashes(world.events())
    assert {k: v.phase for k, v in ref.items()} == {
        "out/a.json": "run_end_legacy",
        "out/big.bin": "adopted",
        "out/cells/c1.npy": "adopted",
        "out/cells/c2.npy": "adopted",
    }
    assert ref["out/big.bin"].sha256 == sha(b"BIG" * 9)


def test_adopt_on_a_run_in_a_linked_worktree_stamps_that_worktree_and_lands_in_the_project_log(
    world: World, tmp_path: Path
):
    """The run lives in a linked worktree and the adopter is in neither it nor the main checkout. The event must
    carry the run's worktree_root (not the main checkout the log lives in, not the adopter's cwd) and must be
    written to the project's log, where the run's own events are."""
    linked = tmp_path / "wt-linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "wt-b1", str(linked)], cwd=world.repo, check=True
    )
    world.root = linked
    legacy_run(world)

    result = world.adopt("out/cells")

    assert "error" not in result, result
    assert result["worktree_root"] == str(linked)
    adopted = [
        e
        for e in world.events()
        if e["kind"] == "run.outputs_hashed" and e["data"].get("phase") == "adopted"
    ]
    assert [e["worktree_root"] for e in adopted] == [str(linked)]
    assert adopted[0]["main_root"] == str(world.repo)
    assert {e["relpath"] for e in world.adopted_outputs()} == {
        "out/cells/c1.npy",
        "out/cells/c2.npy",
    }
    world.adopt(retract=result["eid"], reason="r")
    retracted = [e for e in world.events() if e["kind"] == "run.outputs_adopt_retracted"]
    assert [e["worktree_root"] for e in retracted] == [str(linked)]


def test_adopt_never_covers_a_legacy_path_that_has_a_sha_even_when_the_bytes_changed(world: World):
    """A legacy entry with a sha is never overridden (spec 3.3): changed bytes under it refuse with
    run_end_conflict and nothing is written. An implementation that treated legacy like null would adopt it."""
    legacy_run(world)
    world.write("out/a.json", b"alpha-TAMPERED")
    before = world.log_bytes()

    result = world.adopt("out")

    assert result["error_code"] == "run_end_conflict"
    assert "out/a.json" in result["error"]
    assert world.log_bytes() == before
    assert world.adopted_outputs() == []


def test_adopt_over_a_run_end_path_with_a_different_hash_is_run_end_conflict(world: World):
    """The headline D5 case: a standard `run_end` entry, the file edited since, adopt over it refuses."""
    a = world.write("out/a.json", b"alpha")
    world.write("out/cells/c1.npy", b"cell-1")
    world.started()
    world.run_end([world.entry("out/a.json", phase="run_end")])
    world.finished()
    world.ingest()
    world.write("out/a.json", b"alpha, edited later")
    before = world.log_bytes()

    result = world.adopt("out/a.json", "out/cells")

    assert result["error_code"] == "run_end_conflict"
    assert world.log_bytes() == before
    # And with the bytes restored the same call succeeds and skips the run_end path:
    assert sha(b"alpha") == a
    world.write("out/a.json", b"alpha")
    ok = world.adopt("out/a.json", "out/cells")
    assert ok["files"] == ["out/cells/c1.npy"]
    assert ok["skipped"] == [{"relpath": "out/a.json", "reason": "already_run_end"}]


def test_adopting_the_same_bytes_twice_writes_one_event(world: World):
    legacy_run(world)
    first = world.adopt("out/cells")
    before = world.log_bytes()

    second = world.adopt("out/cells")

    assert second["eid"] is None and second["n_files"] == 0
    assert {s["reason"] for s in second["skipped"]} == {"already_adopted"}
    assert world.log_bytes() == before
    assert first["n_files"] == 2


# --------------------------------------------------------------------------------------------------
# adopt: refusals


def test_adopt_on_a_non_terminal_run_refuses(world: World):
    """A running run (no run.finished) refuses; so does an abandoned (reaped) one, which may still be alive."""
    world.write("out/cells/c1.npy", b"cell-1")
    world.started()
    world.ingest()
    before = world.log_bytes()

    result = world.adopt("out/cells")

    assert result["error_code"] == "run_not_terminal"
    assert world.log_bytes() == before


def test_adopt_on_an_abandoned_run_refuses(world: World):
    world.write("out/cells/c1.npy", b"cell-1")
    world.started()
    world.add(
        "run.reaped",
        {
            "run_id": RID,
            "project_slug": "repo",
            "reaped_at": T1,
            "reason": "orphan",
            "window_h": 24,
            "prior_status": "running",
        },
        T1,
        "e-reaped",
    )
    world.ingest()

    assert world.adopt("out/cells")["error_code"] == "run_not_terminal"


@pytest.mark.parametrize("with_run_end", [False, True])
def test_adopt_on_an_outputs_declared_run_is_closed_even_without_a_run_end_event(
    world: World, with_run_end: bool
):
    """`outputs_declared` on run.started closes adopt whether or not the run-end event exists: a failed run-end
    hash must not let adopt vouch for files modified later."""
    world.write("out/cells/c1.npy", b"cell-1")
    world.started(declared=True)
    if with_run_end:
        world.run_end([world.entry("out/cells/c1.npy", phase="run_end")])
    world.finished()
    world.ingest()
    before = world.log_bytes()

    result = world.adopt("out/cells")

    assert result["error_code"] == "adopt_closed"
    assert world.log_bytes() == before


def test_adopt_root_outside_the_runs_worktree_refuses(world: World, tmp_path: Path):
    legacy_run(world)
    other = tmp_path / "somewhere_else"
    other.mkdir()
    (other / "f.bin").write_bytes(b"x")
    before = world.log_bytes()

    absolute = world.adopt(str(other))
    escaping = world.adopt("out/../../somewhere_else")

    assert absolute["error_code"] == "root_outside_worktree"
    assert escaping["error_code"] == "root_outside_worktree"
    assert world.log_bytes() == before


def test_adopt_refuses_a_symlink_under_a_root_and_a_symlink_root(world: World):
    """A link is never followed or adopted: the whole adopt refuses and writes nothing."""
    legacy_run(world)
    (world.repo / "out" / "cells" / "link.npy").symlink_to(world.repo / "out" / "a.json")
    (world.repo / "out" / "linkdir").symlink_to(world.repo / "out" / "cells")
    before = world.log_bytes()

    nested = world.adopt("out/cells")
    as_root = world.adopt("out/linkdir")

    assert nested["error_code"] == "symlink_in_outputs"
    assert "link.npy" in nested["error"]
    assert as_root["error_code"] == "symlink_in_outputs"
    assert world.log_bytes() == before


def test_adopt_refuses_a_missing_root(world: World):
    legacy_run(world)
    assert world.adopt("out/nope")["error_code"] == "root_missing"


def test_adopt_requires_a_root_and_a_reason(world: World):
    legacy_run(world)
    assert world.adopt(reason="r")["error_code"] == "invalid_param"
    assert world.adopt("out/cells", reason="  ")["error_code"] == "invalid_param"


def test_a_legacy_catalog_refuses_with_requires_log_mode(world: World):
    legacy_run(world)
    cutover_marker_path(world.catalog).unlink()
    assert world.adopt("out/cells")["error_code"] == "requires_log_mode"
    assert world.hash()["error_code"] == "requires_log_mode"


# --------------------------------------------------------------------------------------------------
# imported runs and worktree_root


def _imported_world(world: World) -> None:
    world.write("out/a.json", b"alpha")
    world.write("out/cells/c1.npy", b"cell-1")
    world.imported([world.entry("out/a.json")])
    world.ingest()


def test_imported_run_without_worktree_root_is_worktree_unknown_with_it_the_root_is_recorded(
    world: World,
):
    _imported_world(world)
    before = world.log_bytes()
    assert world.adopt("out/cells")["error_code"] == "worktree_unknown"
    assert world.hash()["error_code"] == "worktree_unknown"
    assert world.log_bytes() == before

    result = world.adopt("out/cells", worktree_root=str(world.repo))

    assert "error" not in result, result
    assert result["worktree_root_source"] == "override"
    adopted = [
        e
        for e in world.events()
        if e["kind"] == "run.outputs_hashed" and e["data"].get("phase") == "adopted"
    ]
    # The importer's default root is the catalog dir; the event must carry the root the user vouched for.
    assert [e["worktree_root"] for e in adopted] == [str(world.repo)]
    # The legacy absolute path now resolves against the override too.
    ref = reference_hashes(world.events(), imported_root=str(world.repo))
    assert ref["out/a.json"].phase == "run_end_legacy"
    report = world.hash(worktree_root=str(world.repo))
    assert report["counts"] == {"match": 2, "drift": 0, "missing": 0}


def test_imported_run_with_a_legacy_path_outside_the_root_is_legacy_path_unresolved(
    world: World, tmp_path: Path
):
    stray = tmp_path / "stray.bin"
    stray.write_bytes(b"x")
    world.write("out/cells/c1.npy", b"cell-1")
    world.imported(
        [{"path": str(stray), "status": "present", "size_bytes": 1, "sha256": sha(b"x")}]
    )
    world.ingest()

    adopt = world.adopt("out/cells", worktree_root=str(world.repo))
    hashed = world.hash(worktree_root=str(world.repo))

    assert adopt["error_code"] == "legacy_path_unresolved"
    assert str(stray) in adopt["error"]
    assert hashed["error_code"] == "legacy_path_unresolved"


def test_a_worktree_root_override_on_a_standard_run_is_refused_when_it_disagrees(
    world: World, tmp_path: Path
):
    legacy_run(world)
    other = tmp_path / "other"
    other.mkdir()
    assert world.adopt("out/cells", worktree_root=str(other))["error_code"] == "invalid_param"
    assert "error" not in world.adopt("out/cells", worktree_root=str(world.repo), dry_run=True)


def test_a_recorded_worktree_root_that_is_gone_is_worktree_missing(world: World, tmp_path: Path):
    gone = tmp_path / "gone"
    gone.mkdir()
    world.add(
        "run.started",
        {
            "id": RID,
            "project_slug": "repo",
            "command": "x",
            "argv": ["x"],
            "git_hash": "a",
            "git_branch": "main",
            "git_dirty": False,
            "timestamp": T0,
            "agent_mode": "manual",
        },
        T0,
        "e-s",
        wroot=gone,
    )
    world.finished()
    world.ingest()
    gone.rmdir()
    assert world.adopt("out")["error_code"] == "worktree_missing"
    assert world.hash()["error_code"] == "worktree_missing"


# --------------------------------------------------------------------------------------------------
# retract


def test_retract_removes_the_entries_from_reference_hashes_and_adopted_outputs_after_ingest(
    world: World,
):
    """Needs `run.outputs_adopt_retracted` in ingest `_RUN_KINDS`, or the run is never refolded and
    `runs.adopted_outputs` keeps the entries."""
    legacy_run(world)
    adopt = world.adopt("out/cells")
    assert {e["relpath"] for e in world.adopted_outputs()} == {
        "out/cells/c1.npy",
        "out/cells/c2.npy",
    }

    result = world.adopt(retract=adopt["eid"], reason="adopted the wrong directory")

    assert "error" not in result, result
    assert result["files"] == ["out/cells/c1.npy", "out/cells/c2.npy"]
    assert world.adopted_outputs() == []  # the index fold, via the real refold path
    ref = reference_hashes(world.events())
    assert set(ref) == {"out/a.json"}
    retracted = [e for e in world.events() if e["kind"] == "run.outputs_adopt_retracted"]
    assert len(retracted) == 1
    assert retracted[0]["data"] == {
        "retracts": adopt["eid"],
        "reason": "adopted the wrong directory",
    }
    assert retracted[0]["worktree_root"] == str(world.repo)
    # Re-adopting after a retract works: the retracted event no longer counts as "already adopted".
    again = world.adopt("out/cells")
    assert again["n_files"] == 2


def test_retract_of_an_unknown_foreign_or_already_retracted_event_refuses(world: World):
    legacy_run(world)
    adopt = world.adopt("out/cells")
    run_end_eid = next(
        e["eid"]
        for e in world.events()
        if e["data"].get("phase") is None and e["kind"] == "run.outputs_hashed"
    )

    assert world.adopt(retract="no-such-eid", reason="r")["error_code"] == "adopt_event_not_found"
    # A run-end (non-adopt) event cannot be retracted:
    assert world.adopt(retract=run_end_eid, reason="r")["error_code"] == "adopt_event_not_found"
    assert "error" not in world.adopt(retract=adopt["eid"], reason="r")
    assert world.adopt(retract=adopt["eid"], reason="r")["error_code"] == "adopt_event_not_found"
    assert (
        world.adopt("out/cells", retract=adopt["eid"], reason="r")["error_code"] == "invalid_param"
    )
    assert world.adopt(retract=adopt["eid"], reason="")["error_code"] == "invalid_param"


def test_retracting_one_adopt_leaves_an_earlier_live_adopt_of_the_same_path(world: World):
    legacy_run(world)
    first = world.adopt("out/cells/c1.npy")
    world.write("out/cells/c1.npy", b"cell-1-changed")
    second = world.adopt("out/cells/c1.npy")
    assert second["n_files"] == 1  # new bytes: a re-adopt, not "already adopted"
    assert reference_hashes(world.events())["out/cells/c1.npy"].sha256 == sha(b"cell-1-changed")

    world.adopt(retract=second["eid"], reason="oops")

    assert reference_hashes(world.events())["out/cells/c1.npy"].sha256 == sha(b"cell-1")
    assert first["eid"] != second["eid"]


# --------------------------------------------------------------------------------------------------
# --dry-run


def test_adopt_dry_run_writes_no_event_and_lists_files_and_bytes(world: World):
    legacy_run(world)
    before = world.log_bytes()
    n_events = len(world.events())

    result = world.adopt("out/cells", dry_run=True)

    assert result["dry_run"] is True and result["eid"] is None
    assert result["files"] == ["out/cells/c1.npy", "out/cells/c2.npy"]
    assert result["total_bytes"] == 6 + 7
    assert world.log_bytes() == before  # byte-for-byte: no segment grew, mirror included
    assert len(world.events()) == n_events
    assert world.adopted_outputs() == []


def test_retract_dry_run_writes_no_event(world: World):
    legacy_run(world)
    adopt = world.adopt("out/cells")
    before = world.log_bytes()

    result = world.adopt(retract=adopt["eid"], reason="r", dry_run=True)

    assert result["dry_run"] is True and result["eid"] is None and result["n_files"] == 2
    assert world.log_bytes() == before
    assert len(world.adopted_outputs()) == 2


def test_dry_run_still_refuses_what_a_real_run_would(world: World):
    legacy_run(world)
    world.write("out/a.json", b"alpha-TAMPERED")
    assert world.adopt("out", dry_run=True)["error_code"] == "run_end_conflict"


# --------------------------------------------------------------------------------------------------
# hash


def test_hash_reports_match_drift_and_missing_per_path_and_writes_nothing(world: World):
    legacy_run(world)
    world.adopt("out/cells")
    world.write("out/cells/c1.npy", b"cell-1 but different")  # drift
    (world.repo / "out" / "cells" / "c2.npy").unlink()  # missing
    before = world.log_bytes()

    report = world.hash()

    assert "error" not in report, report
    by_path = {f["relpath"]: f for f in report["files"]}
    assert by_path["out/a.json"]["status"] == "match"
    assert by_path["out/cells/c1.npy"]["status"] == "drift"
    assert by_path["out/cells/c1.npy"]["live_sha256"] == sha(b"cell-1 but different")
    assert by_path["out/cells/c1.npy"]["reference_sha256"] == sha(b"cell-1")
    assert by_path["out/cells/c2.npy"]["status"] == "missing"
    # `big.bin` has a null legacy sha: it is not in the reference set, so it is not reported at all.
    assert "out/big.bin" not in by_path
    assert report["counts"] == {"match": 1, "drift": 1, "missing": 1}
    assert report["all_match"] is False
    assert {f["relpath"]: f["phase"] for f in report["files"]} == {
        "out/a.json": "run_end_legacy",
        "out/cells/c1.npy": "adopted",
        "out/cells/c2.npy": "adopted",
    }
    assert world.log_bytes() == before


def test_hash_all_match_and_a_symlinked_replacement_is_drift_not_a_followed_read(world: World):
    legacy_run(world)
    assert world.hash()["all_match"] is True
    (world.repo / "out" / "a.json").unlink()
    (world.repo / "out" / "a.json").symlink_to(world.repo / "out" / "cells" / "c1.npy")
    state = {f["relpath"]: f for f in world.hash()["files"]}["out/a.json"]
    assert state["status"] == "drift" and state["detail"] == "symlink"


def test_hash_with_no_reference_set_refuses(world: World):
    world.started()
    world.finished()
    world.ingest()
    assert world.hash()["error_code"] == "no_reference_hashes"


def test_hash_ignores_a_rebaseline_event(world: World):
    """Rebaseline events never contribute (spec 3.3): after `bth check --rebaseline` rewrote the hash of an edited
    file, `hash` still reports drift against the run-end hash."""
    world.write("out/a.json", b"alpha")
    world.started()
    world.run_end([world.entry("out/a.json", phase="run_end")])
    world.finished()
    world.write("out/a.json", b"edited")
    world.add(
        "run.outputs_hashed",
        {"phase": "rebaseline", "output_metadata": [world.entry("out/a.json", phase="rebaseline")]},
        "2026-01-01T05:00:00.000000Z",
        "e-rebase",
    )
    world.ingest()

    report = world.hash()

    assert report["counts"] == {"match": 0, "drift": 1, "missing": 0}


# --------------------------------------------------------------------------------------------------
# run-id prefixes


def test_run_id_prefix_resolves_and_zero_or_two_matches_refuse(world: World):
    legacy_run(world)
    other = "11111111-bbbb-4bbb-8bbb-222222222222"
    world.started(other)
    world.finished(other)
    world.ingest()

    assert world.hash("11111111-a")["run_id"] == RID  # unique prefix resolves
    for fn in (lambda p: world.hash(p), lambda p: world.adopt("out/cells", run_id=p, dry_run=True)):
        assert fn("11111111")["error_code"] == "run_prefix_ambiguous"  # 2 matches
        assert fn("ffffffff")["error_code"] == "run_not_found"  # 0 matches
        assert fn("")["error"]  # empty is refused too


# --------------------------------------------------------------------------------------------------
# wiring: CLI and MCP


def test_cli_adopt_dry_run_then_real_and_error_exit(world: World):
    legacy_run(world)
    runner = CyclopticRunner()
    base = ["outputs", "adopt", "--run-id", RID, "--catalog-dir", str(world.catalog)]

    dry = runner.invoke(
        app, [*base, "--root", "out/cells", "--root", "out/big.bin", "--reason", "r", "--dry-run"]
    )
    assert dry.exit_code == 0, dry.output
    payload = json.loads(dry.stdout[dry.stdout.index("{") :])
    assert payload["files"] == [
        "out/big.bin",
        "out/cells/c1.npy",
        "out/cells/c2.npy",
    ]  # repeated --root

    real = runner.invoke(app, [*base, "--root", "out/cells", "--reason", "r"])
    assert real.exit_code == 0, real.output
    assert len(world.adopted_outputs()) == 2

    world.write("out/a.json", b"tampered")
    bad = runner.invoke(app, [*base, "--root", "out/a.json", "--reason", "r"])
    assert bad.exit_code == 1
    assert "run_end_conflict" in bad.stderr  # the CLI prints the code, callers branch on it

    hashed = runner.invoke(
        app, ["outputs", "hash", "--run-id", RID, "--catalog-dir", str(world.catalog)]
    )
    assert hashed.exit_code == 0, hashed.output
    assert json.loads(hashed.stdout[hashed.stdout.index("{") :])["counts"]["drift"] == 1


def test_mcp_wrapper_returns_the_error_envelope_with_code_and_hint(world: World):
    legacy_run(world)
    world.write("out/a.json", b"tampered")

    result = asyncio.run(
        mcp_outputs_adopt_tool(
            run_id=RID, root=["out/a.json"], reason="r", catalog_dir=str(world.catalog)
        )
    )

    assert result["ok"] is False
    assert result["error_code"] == "run_end_conflict"
    assert result["resolution_hint"]
