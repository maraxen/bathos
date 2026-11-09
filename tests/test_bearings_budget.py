"""Acceptance tests for the packet byte budget (spec 261104, AC-4b, owner decision 261108 on T6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bathos.bearings.packet import build_packet, build_packet_within_budget
from bathos.errors import BearingsPacketInvalidError
from tests.test_bearings_packet import (  # noqa: F401  (env is a fixture)
    SINCE,
    UNTIL,
    commit_files,
    env,
    insert_runs,
)

N_RUNS = 12
PAD = (
    "x" * 1500
)  # makes each run record weigh about the same, so bytes grow with the number of runs


def day(i: int) -> str:
    return f"2026-06-{i + 1:02d}T12:00:00+00:00"


@pytest.fixture
def world(env):  # noqa: F811
    """A repo with one commit and N_RUNS runs on N_RUNS different days, each with a long command."""
    sha = commit_files(env["repo"], {"scripts/a.py": "print(1)\n"}, msg="initial work")
    insert_runs(
        env,
        [
            {"id": f"{i:02d}aaaaaaaabbbb", "project_slug": "pkt", "timestamp": day(i), "git_hash": sha,
             "command": f"python scripts/a.py {PAD} --seed {i}"}
            for i in range(N_RUNS)
        ],
    )  # fmt: skip
    return env


def reader_bytes(packet: Path) -> int:
    return sum(p.stat().st_size for p in (packet / "reader").rglob("*") if p.is_file())


def plain(w, name: str, since: str = SINCE) -> tuple[dict, Path]:
    out = w["tmp"] / name
    return build_packet(w["repo"], out=out, since=since, until=UNTIL, catalog_dir=w["cat"]), out


def budgeted(w, name: str, max_bytes, since: str = SINCE) -> tuple[dict, Path]:
    out = w["tmp"] / name
    res = build_packet_within_budget(
        w["repo"], out=out, since=since, until=UNTIL, max_bytes=max_bytes, catalog_dir=w["cat"]
    )
    return res, out


def manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text())


def run_ids(out: Path) -> list[str]:
    return [json.loads(x)["id"] for x in (out / "reader/runs.jsonl").read_text().split("\n") if x]


def test_a_window_that_fits_is_byte_identical_to_the_unbudgeted_packet(world):
    full, _ = plain(world, "full")
    res, out = budgeted(world, "b", max_bytes=10_000_000)
    assert res["packet_sha256"] == full["packet_sha256"]
    b = manifest(out)["budget"]
    assert b["window_shortened"] is False
    assert b["effective_since"] == b["requested_since"] == SINCE
    assert b["dropped"] == {"reason": "budget", "runs": [], "commits": []}
    assert res["out"] == str(out)


def test_over_budget_shortens_the_window_to_the_earliest_start_that_fits(world):
    _, full_out = plain(world, "full")
    full_bytes = reader_bytes(full_out)
    budget = int(full_bytes * 0.6)
    res, out = budgeted(world, "b", max_bytes=budget)
    b = manifest(out)["budget"]
    assert b["window_shortened"] is True
    assert reader_bytes(out) <= budget and b["bytes"] == reader_bytes(out)
    assert b["full_window_bytes"] == full_bytes and b["max_bytes"] == budget
    assert b["requested_since"] == SINCE and b["effective_since"] > SINCE
    kept = run_ids(out)
    assert 0 < len(kept) < N_RUNS
    assert kept == sorted(kept)  # the newest runs, in order
    assert kept == [f"{i:02d}aaaaaaaabbbb" for i in range(N_RUNS - len(kept), N_RUNS)]
    # Maximal: starting one run earlier would not fit.
    earlier_since = day(N_RUNS - len(kept) - 1)
    _, earlier = plain(world, "earlier", since=earlier_since)
    assert reader_bytes(earlier) > budget
    assert res["window_shortened"] is True and res["n_runs"] == len(kept)


def test_every_dropped_run_is_listed_by_id_with_reason_budget(world):
    _, full_out = plain(world, "full")
    res, out = budgeted(world, "b", max_bytes=int(reader_bytes(full_out) * 0.6))
    b = manifest(out)["budget"]
    kept = set(run_ids(out))
    dropped = b["dropped"]
    assert dropped["reason"] == "budget"
    assert dropped["runs"] == sorted(set(run_ids(full_out)) - kept)
    assert len(dropped["runs"]) + len(kept) == N_RUNS
    assert res["n_dropped_runs"] == len(dropped["runs"])


def test_two_budgeted_builds_have_the_same_hash_and_budget_block(world):
    _, full_out = plain(world, "full")
    budget = int(reader_bytes(full_out) * 0.5)
    first, a = budgeted(world, "a", max_bytes=budget)
    second, b = budgeted(world, "b", max_bytes=budget)
    assert first["packet_sha256"] == second["packet_sha256"]
    assert manifest(a)["budget"] == manifest(b)["budget"]


def test_content_that_does_not_shrink_with_the_window_refuses_and_writes_nothing(world):
    out = world["tmp"] / "b"
    with pytest.raises(BearingsPacketInvalidError):
        build_packet_within_budget(
            world["repo"],
            out=out,
            since=SINCE,
            until=UNTIL,
            max_bytes=200,
            catalog_dir=world["cat"],
        )
    assert not out.exists() or not any(out.iterdir())


@pytest.mark.parametrize("off", [0, None])
def test_budget_off_behaves_like_the_unbudgeted_build(world, off):
    full, _ = plain(world, "full")
    res, out = budgeted(world, "b", max_bytes=off)
    assert res["packet_sha256"] == full["packet_sha256"]
    assert manifest(out)["budget"]["max_bytes"] is None


def test_a_non_empty_out_directory_is_refused(world):
    out = world["tmp"] / "b"
    out.mkdir()
    (out / "stray.txt").write_text("x")
    with pytest.raises(BearingsPacketInvalidError):
        build_packet_within_budget(
            world["repo"],
            out=out,
            since=SINCE,
            until=UNTIL,
            max_bytes=10_000_000,
            catalog_dir=world["cat"],
        )
    assert [p.name for p in out.iterdir()] == ["stray.txt"]
