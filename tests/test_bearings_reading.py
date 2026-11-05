"""Acceptance tests for the bearings reading record and comparison (spec 261104 slice 2: AC-5, AC-3 atom half)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bathos.bearings.packet import build_packet
from bathos.bearings.reading import compare_reading, record_reading
from bathos.charter import confirm
from bathos.errors import (
    BearingsMapperIsReaderError,
    BearingsMappingInvalidError,
    BearingsNoConfirmedCharterError,
    BearingsPacketMismatchError,
    BearingsReadingInvalidError,
    BearingsReadingNotFoundError,
    StepBackInsufficientEvidenceError,
)
from bathos.index import connect_read
from tests.test_bearings_packet import (  # noqa: F401  (env is a fixture)
    DATE,
    SINCE,
    TOKEN,
    UNTIL,
    commit_files,
    env,
    git,
    insert_runs,
    write_charter,
)

SPEC_PATH = ".praxia/docs/specs/s.md"
SPEC_TEXT = "# Spec\nline two\nline three\nline four\nline five\n"
HANDOFF_PATH = ".praxia/docs/handoffs/h.md"


@pytest.fixture
def world(env):  # noqa: F811
    """A confirmed charter (P1, P2 live; P3 retired), a committed repo with runs, and a built packet."""
    repo = env["repo"]
    write_charter(repo)
    confirm(repo, chair_token=TOKEN, interactive_tty=False, catalog_dir=env["cat"])
    sha = commit_files(
        repo,
        {
            SPEC_PATH: SPEC_TEXT,
            HANDOFF_PATH: "narrative that the reader must not cite\n",
            "scripts/a.py": "print(1)\n",
            "scripts/a.bth.toml": '[experiment]\nhypothesis = "plain"\n',
        },
        msg="initial work",
    )
    insert_runs(
        env,
        [
            {"id": "11111111aaaaaaaa", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha,
             "sidecar_path": "scripts/a.bth.toml"},
            {"id": "11111111bbbbbbbb", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha},
            {"id": "22222222cccccccc", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha},
        ],
    )
    out = env["tmp"] / "packet"
    res = build_packet(repo, out=out, since=SINCE, until=UNTIL, catalog_dir=env["cat"])
    blob = git(repo, "rev-parse", f"HEAD:{SPEC_PATH}")
    handoff_blob = git(repo, "rev-parse", f"HEAD:{HANDOFF_PATH}")
    return {
        **env,
        "packet": out,
        "packet_sha": res["packet_sha256"],
        "git_sha": sha,
        "doc_cite": f"doc:{SPEC_PATH}@{blob}#L2-L4",
        "blob": blob,
        "handoff_blob": handoff_blob,
        "good": ["run:22222222cccccccc", "git:" + sha[:10]],
    }


def write_reading(w, name: str, purposes, open_lines=None, reader_id="reader-A", packet_sha=None) -> Path:
    path = w["tmp"] / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "packet_sha256": packet_sha or w["packet_sha"],
                "reader_id": reader_id,
                "purposes": purposes,
                "open_lines": open_lines or [],
            }
        )
    )
    return path


def rec(w, reading: Path):
    return record_reading(w["repo"], reading_file=reading, packet_dir=w["packet"], catalog_dir=w["cat"])


def n_events(w, prefix: str = "bearings.") -> int:
    con = connect_read(w["cat"])
    try:
        return con.execute("SELECT count(*) FROM events WHERE kind LIKE ?", [prefix + "%"]).fetchone()[0]
    finally:
        con.close()


def rows(w, sql: str, params=()):
    con = connect_read(w["cat"])
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()


def purpose(pid: str, cites: list, text: str = "reads as some work") -> dict:
    return {"id": pid, "text": text, "citations": cites}


# ── citation resolution ──────────────────────────────────────────────────────


def test_every_resolvable_kind_counts(world):
    cites = [
        "run:22222222cccccccc",
        "run:2222222",  # 7 chars: below the 8-char prefix floor, must NOT count
        "sidecar:scripts/a.bth.toml#hypothesis",
        "git:" + world["git_sha"][:7],
        world["doc_cite"],
    ]
    res = rec(world, write_reading(world, "ok", [purpose("r1", cites)]))
    assert res["status"] == "recorded"
    stored = json.loads(rows(world, "SELECT purposes_json FROM bearings_readings")[0][0])
    by_cit = {c["citation"]: c["resolution"] for c in stored[0]["citations"]}
    assert by_cit["run:22222222cccccccc"] == "resolved"
    assert by_cit["run:2222222"] == "unresolvable"
    assert by_cit["sidecar:scripts/a.bth.toml#hypothesis"] == "resolved"
    assert by_cit["git:" + world["git_sha"][:7]] == "resolved"
    assert by_cit[world["doc_cite"]] == "resolved"


def test_unique_run_prefix_resolves(world):
    res = rec(world, write_reading(world, "p", [purpose("r1", ["run:22222222", world["doc_cite"]])]))
    assert res["status"] == "recorded"


@pytest.mark.parametrize(
    "bad",
    [
        "run:deadbeefdeadbeef",  # unknown run id
        "run:11111111",  # ambiguous prefix (two runs share it)
        "sidecar:scripts/missing.bth.toml",  # not in the packet
        "git:0000000",  # unknown commit
        "doc:.praxia/docs/specs/s.md@" + "0" * 40 + "#L1-L2",  # wrong blob
        "doc:.praxia/docs/handoffs/h.md@{handoff_blob}#L1-L1",  # excluded from the packet
        "doc:.praxia/docs/specs/s.md@{blob}#L4-L99",  # range past the end
        "doc:.praxia/docs/specs/s.md@{blob}#L3-L2",  # inverted range
        "doc:.praxia/docs/specs/s.md@{blob}#L0-L2",  # line numbers are 1-based
        "nonsense",
    ],
)
def test_citation_that_must_not_resolve_does_not_count(world, bad):
    bad = bad.format(blob=world["blob"], handoff_blob=world["handoff_blob"])
    # One good citation plus the bad one: if the bad one wrongly resolved, the purpose would pass.
    reading = write_reading(world, "bad", [purpose("r1", ["run:22222222cccccccc", bad])])
    before = n_events(world)
    with pytest.raises(StepBackInsufficientEvidenceError) as exc:
        rec(world, reading)
    assert "r1" in str(exc.value)
    assert n_events(world) == before


def test_duplicate_citations_count_once(world):
    reading = write_reading(world, "dup", [purpose("r1", ["run:22222222cccccccc", "run:22222222cccccccc"])])
    with pytest.raises(StepBackInsufficientEvidenceError):
        rec(world, reading)


# ── AC-5: evidence minimum, atomicity ────────────────────────────────────────


def test_one_citation_purpose_refused_and_nothing_appended_even_when_other_valid(world):
    reading = write_reading(
        world,
        "mixed",
        [purpose("good", world["good"]), purpose("thin", ["run:22222222cccccccc"])],
    )
    before = n_events(world)
    with pytest.raises(StepBackInsufficientEvidenceError) as exc:
        rec(world, reading)
    assert "thin" in str(exc.value)
    assert "good" not in str(exc.value)
    assert n_events(world) == before
    assert rows(world, "SELECT count(*) FROM bearings_readings")[0][0] == 0


def test_every_failing_purpose_is_named(world):
    reading = write_reading(
        world, "two", [purpose("t1", ["nonsense"]), purpose("t2", []), purpose("ok", world["good"])]
    )
    with pytest.raises(StepBackInsufficientEvidenceError) as exc:
        rec(world, reading)
    assert "t1" in str(exc.value)
    assert "t2" in str(exc.value)


def test_open_line_needs_one_resolvable_citation(world):
    bad = write_reading(world, "ol_bad", [purpose("r1", world["good"])], [{"text": "stray work", "citations": ["nonsense"]}])
    before = n_events(world)
    with pytest.raises(StepBackInsufficientEvidenceError):
        rec(world, bad)
    assert n_events(world) == before
    ok = write_reading(world, "ol_ok", [purpose("r1", world["good"])], [{"text": "stray work", "citations": ["git:" + world["git_sha"][:8]]}])
    assert rec(world, ok)["status"] == "recorded"


def test_packet_hash_mismatch_in_reading_refused(world):
    reading = write_reading(world, "mm", [purpose("r1", world["good"])], packet_sha="0" * 64)
    before = n_events(world)
    with pytest.raises(BearingsPacketMismatchError):
        rec(world, reading)
    assert n_events(world) == before


def test_tampered_packet_byte_refused(world):
    f = world["packet"] / "reader/docs" / SPEC_PATH
    f.write_bytes(f.read_bytes().replace(b"line two", b"line 2wo"))
    reading = write_reading(world, "t", [purpose("r1", world["good"])])
    before = n_events(world)
    with pytest.raises(BearingsPacketMismatchError):
        rec(world, reading)
    assert n_events(world) == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("reader_id"),
        lambda d: d.update(purposes="not a list"),
        lambda d: d["purposes"].append({"id": "r1", "text": "dup", "citations": []}),
        lambda d: d["purposes"][0].pop("text"),
    ],
)
def test_malformed_reading_refused_without_event(world, mutate):
    data = {
        "packet_sha256": world["packet_sha"],
        "reader_id": "reader-A",
        "purposes": [purpose("r1", world["good"])],
        "open_lines": [],
    }
    mutate(data)
    path = world["tmp"] / "malformed.json"
    path.write_text(json.dumps(data))
    before = n_events(world)
    with pytest.raises(BearingsReadingInvalidError):
        rec(world, path)
    assert n_events(world) == before


def test_identical_rerecord_is_already_recorded_with_one_event(world):
    reading = write_reading(world, "same", [purpose("r1", world["good"])])
    first = rec(world, reading)
    second = rec(world, reading)
    assert first["status"] == "recorded"
    assert second["status"] == "already_recorded"
    assert second["reading_id"] == first["reading_id"]
    assert n_events(world, "bearings.reading_recorded") == 1
    assert rows(world, "SELECT count(*) FROM bearings_readings")[0][0] == 1


def test_a_different_reading_gets_a_different_id(world):
    a = rec(world, write_reading(world, "a", [purpose("r1", world["good"])]))
    b = rec(world, write_reading(world, "b", [purpose("r1", world["good"], text="other words")]))
    assert a["reading_id"] != b["reading_id"]
    assert n_events(world, "bearings.reading_recorded") == 2


def test_record_needs_no_charter(env):  # noqa: F811
    """A reading never waits on charter confirmation (pre-mortem A10)."""
    repo = env["repo"]
    sha = commit_files(repo, {SPEC_PATH: SPEC_TEXT})
    insert_runs(env, [{"id": "22222222cccccccc", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha}])
    out = env["tmp"] / "pk"
    res = build_packet(repo, out=out, since=SINCE, until=UNTIL, catalog_dir=env["cat"])
    path = env["tmp"] / "r.json"
    blob = git(repo, "rev-parse", f"HEAD:{SPEC_PATH}")
    path.write_text(
        json.dumps(
            {
                "packet_sha256": res["packet_sha256"],
                "reader_id": "x",
                "purposes": [purpose("r1", ["run:22222222cccccccc", f"doc:{SPEC_PATH}@{blob}#L1-L2"])],
            }
        )
    )
    assert record_reading(repo, reading_file=path, packet_dir=out, catalog_dir=env["cat"])["status"] == "recorded"


# ── AC-5: compare ────────────────────────────────────────────────────────────


def write_mapping(w, mapping: dict, mapper_id: str = "mapper-B", name: str = "mapping") -> Path:
    path = w["tmp"] / f"{name}.json"
    path.write_text(json.dumps({"mapper_id": mapper_id, "mapping": mapping}))
    return path


def record_three(w) -> str:
    reading = write_reading(
        w,
        "three",
        [purpose("r1", w["good"]), purpose("r2", w["good"], text="second"), purpose("r3", w["good"], text="third")],
        [{"text": "stray", "citations": ["git:" + w["git_sha"][:8]]}],
    )
    return rec(w, reading)["reading_id"]


def cmp(w, rid: str, mapping: Path):
    return compare_reading(w["repo"], reading_id=rid, mapping_file=mapping, catalog_dir=w["cat"])


def test_compare_computes_mapped_unmapped_silent_and_folds(world):
    rid = record_three(world)
    res = cmp(world, rid, write_mapping(world, {"r1": "P1", "r2": None, "r3": "P3"}))
    by = {(it["kind"], it.get("reading_purpose_id", it.get("open_line_index", it.get("charter_purpose_id")))): it
          for it in res["items"]}
    assert set(by) == {("mapped", "r1"), ("unmapped", "r2"), ("unmapped", "r3"), ("unmapped", 0), ("silent", "P2")}
    assert by[("mapped", "r1")]["charter_purpose_id"] == "P1"
    assert by[("unmapped", "r3")]["matched_retired"] == "P3"
    assert by[("unmapped", "r2")]["matched_retired"] is None
    assert res["counts"] == {"mapped": 1, "unmapped": 3, "silent": 1}

    folded = rows(world, "SELECT item_id, kind, matched_retired, mapper_id FROM bearings_items ORDER BY kind, item_id")
    assert {(k, m) for _, k, m, _ in folded} == {("mapped", None), ("unmapped", None), ("unmapped", "P3"), ("silent", None)}
    assert len(folded) == 5
    assert {i for i, *_ in folded} == {it["item_id"] for it in res["items"]}
    assert {mp for *_, mp in folded} == {"mapper-B"}


def test_item_ids_are_stable_across_identical_runs_and_differ_across_items(world):
    rid = record_three(world)
    m = write_mapping(world, {"r1": "P1", "r2": None, "r3": "P3"})
    first = cmp(world, rid, m)
    second = cmp(world, rid, m)
    assert [i["item_id"] for i in first["items"]] == [i["item_id"] for i in second["items"]]
    assert len({i["item_id"] for i in first["items"]}) == len(first["items"])
    other = cmp(world, rid, write_mapping(world, {"r1": "P2", "r2": None, "r3": "P3"}, name="m2"))
    assert {i["item_id"] for i in other["items"]} != {i["item_id"] for i in first["items"]}


def test_item_id_differs_when_only_the_mapping_target_differs(world):
    """r1->P1 and r1->P2 are different claims, so a label on one must not attach to the other."""
    rid = record_three(world)
    first = cmp(world, rid, write_mapping(world, {"r1": "P1", "r2": None, "r3": None}))
    other = cmp(world, rid, write_mapping(world, {"r1": "P2", "r2": None, "r3": None}, name="m2"))
    id_of = lambda res: next(i["item_id"] for i in res["items"] if i.get("reading_purpose_id") == "r1")  # noqa: E731
    assert id_of(first) != id_of(other)


def test_repeating_a_comparison_is_a_noop_and_a_changed_one_supersedes(world):
    rid = record_three(world)
    m = write_mapping(world, {"r1": "P1", "r2": None, "r3": None})
    assert cmp(world, rid, m)["status"] == "compared"
    n_items = len(rows(world, "SELECT item_id FROM bearings_items"))

    assert cmp(world, rid, m)["status"] == "already_compared"
    assert n_events(world, "bearings.compared") == 1
    assert len(rows(world, "SELECT item_id FROM bearings_items")) == n_items

    changed = cmp(world, rid, write_mapping(world, {"r1": "P2", "r2": None, "r3": None}, name="m2"))
    assert changed["status"] == "compared"
    assert n_events(world, "bearings.compared") == 2
    folded = rows(world, "SELECT item_id FROM bearings_items")
    assert sorted(r[0] for r in folded) == sorted(i["item_id"] for i in changed["items"])


def test_a_mapped_live_purpose_is_not_silent(world):
    rid = record_three(world)
    res = cmp(world, rid, write_mapping(world, {"r1": "P1", "r2": "P2", "r3": None}))
    assert [i for i in res["items"] if i["kind"] == "silent"] == []


def test_compare_refuses_mapper_equal_reader_and_appends_nothing(world):
    rid = record_three(world)
    before = n_events(world, "bearings.compared")
    with pytest.raises(BearingsMapperIsReaderError):
        cmp(world, rid, write_mapping(world, {"r1": "P1", "r2": None, "r3": None}, mapper_id="reader-A"))
    assert n_events(world, "bearings.compared") == before == 0


def test_compare_refuses_unconfirmed_charter_and_appends_nothing(world):
    rid = record_three(world)
    charter = world["repo"] / ".bth" / "charter.toml"
    charter.write_text(charter.read_text() + "\n# edited after confirmation\n")
    with pytest.raises(BearingsNoConfirmedCharterError):
        cmp(world, rid, write_mapping(world, {"r1": "P1", "r2": None, "r3": None}))
    assert n_events(world, "bearings.compared") == 0
    # The reading is still stored and comparable once the owner re-confirms (Q17).
    assert rows(world, "SELECT count(*) FROM bearings_readings")[0][0] == 1
    confirm(world["repo"], chair_token=TOKEN, interactive_tty=False, catalog_dir=world["cat"])
    assert cmp(world, rid, write_mapping(world, {"r1": "P1", "r2": None, "r3": None}))["ok"]


@pytest.mark.parametrize(
    "mapping",
    [
        {"r1": "P1", "r2": None},  # misses r3
        {"r1": "P1", "r2": None, "r3": None, "r4": None},  # names a purpose the reading lacks
        {"r1": "P9", "r2": None, "r3": None},  # unknown charter id
        {"r1": 7, "r2": None, "r3": None},  # wrong type
    ],
)
def test_compare_refuses_bad_mapping_and_appends_nothing(world, mapping):
    rid = record_three(world)
    with pytest.raises(BearingsMappingInvalidError):
        cmp(world, rid, write_mapping(world, mapping))
    assert n_events(world, "bearings.compared") == 0


def test_compare_unknown_reading_refused(world):
    with pytest.raises(BearingsReadingNotFoundError):
        cmp(world, "deadbeefdeadbeef", write_mapping(world, {}))
    assert n_events(world, "bearings.compared") == 0


def test_compare_uses_confirmed_row_not_edited_file(world):
    """Purposes come from the confirmed row: the mapper may name P3 (retired in the row) even after the file moved on."""
    rid = record_three(world)
    res = cmp(world, rid, write_mapping(world, {"r1": "P3", "r2": None, "r3": None}))
    assert [i for i in res["items"] if i.get("reading_purpose_id") == "r1"][0]["matched_retired"] == "P3"


# ── AC-3, atom half ──────────────────────────────────────────────────────────


def test_atom_string_and_object_stored_verbatim_with_flag(world):
    atom_obj = {
        "kind": "atom",
        "slug": "allo",
        "seal_sha256": "ab" * 32,
        "context": [{"start_line": 3, "end_line": 9, "role": "premise"}, {"start_line": 12, "end_line": 14, "role": "claim"}],
    }
    atom_str = "atom:(allo, abcd, 10, 20, ef01)"
    reading = write_reading(world, "atoms", [purpose("r1", [*world["good"], atom_str, atom_obj])])
    rec(world, reading)
    stored = json.loads(rows(world, "SELECT purposes_json FROM bearings_readings")[0][0])[0]["citations"]
    by_flag = [c for c in stored if c["resolution"] == "flag_for_review"]
    assert [c["citation"] for c in by_flag] == [atom_str, atom_obj]  # verbatim, order kept
    assert by_flag[1]["citation"]["context"][1] == {"start_line": 12, "end_line": 14, "role": "claim"}


def test_atoms_do_not_count_toward_the_minimum(world):
    reading = write_reading(
        world,
        "atoms_only",
        [purpose("r1", ["run:22222222cccccccc", "atom:a", "atom:b", {"kind": "atom", "context": []}])],
    )
    before = n_events(world)
    with pytest.raises(StepBackInsufficientEvidenceError):
        rec(world, reading)
    assert n_events(world) == before


def test_malformed_atom_does_not_fail_reading(world):
    reading = write_reading(world, "bad_atom", [purpose("r1", [*world["good"], "atom:", "atom:((("])])
    assert rec(world, reading)["status"] == "recorded"
    stored = json.loads(rows(world, "SELECT purposes_json FROM bearings_readings")[0][0])[0]["citations"]
    assert [c["resolution"] for c in stored].count("flag_for_review") == 2


def test_object_citation_of_other_kind_is_unresolvable_not_atom(world):
    reading = write_reading(world, "obj", [purpose("r1", [*world["good"], {"kind": "run", "id": "x"}])])
    rec(world, reading)
    stored = json.loads(rows(world, "SELECT purposes_json FROM bearings_readings")[0][0])[0]["citations"]
    assert stored[-1]["resolution"] == "unresolvable"


# ── Wiring ───────────────────────────────────────────────────────────────────


def rows_for(e, sql: str):
    con = connect_read(e["cat"])
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_end_to_end_through_the_cli(env):  # noqa: F811
    from bathos.cli_cyclopts import app
    from tests._cyclopts_runner import CyclopticRunner

    runner = CyclopticRunner()
    repo, cat = env["repo"], env["cat"]
    write_charter(repo)
    confirm(repo, chair_token=TOKEN, interactive_tty=False, catalog_dir=cat)
    sha = commit_files(repo, {SPEC_PATH: SPEC_TEXT}, msg="spec work")
    insert_runs(env, [{"id": "22222222cccccccc", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha}])
    common = ["--project-root", str(repo), "--catalog-dir", str(cat)]

    out = env["tmp"] / "cli_packet"
    res = runner.invoke(app, ["bearings", "packet", "--out", str(out), "--since", SINCE, "--until", UNTIL, *common])
    assert res.exit_code == 0, res.output
    packet_sha = json.loads(res.output)["packet_sha256"]

    # A non-empty --out is refused with a non-zero exit.
    res = runner.invoke(app, ["bearings", "packet", "--out", str(out), "--since", SINCE, "--until", UNTIL, *common])
    assert res.exit_code != 0

    blob = git(repo, "rev-parse", f"HEAD:{SPEC_PATH}")
    reading = env["tmp"] / "cli_reading.json"
    reading.write_text(
        json.dumps(
            {
                "packet_sha256": packet_sha,
                "reader_id": "reader-A",
                "purposes": [purpose("r1", ["run:22222222cccccccc", f"doc:{SPEC_PATH}@{blob}#L1-L3"])],
                "open_lines": [],
            }
        )
    )
    res = runner.invoke(app, ["bearings", "record", "--reading", str(reading), "--packet", str(out), *common])
    assert res.exit_code == 0, res.output
    rid = json.loads(res.output)["reading_id"]

    mapping = env["tmp"] / "cli_mapping.json"
    mapping.write_text(json.dumps({"mapper_id": "reader-A", "mapping": {"r1": "P1"}}))
    res = runner.invoke(app, ["bearings", "compare", "--reading-id", rid, "--mapping", str(mapping), *common])
    assert res.exit_code != 0  # mapper is the reader
    assert rows_for(env, "SELECT count(*) FROM bearings_items")[0][0] == 0

    mapping.write_text(json.dumps({"mapper_id": "mapper-B", "mapping": {"r1": "P1"}}))
    res = runner.invoke(app, ["bearings", "compare", "--reading-id", rid, "--mapping", str(mapping), *common])
    assert res.exit_code == 0, res.output
    items = rows_for(env, "SELECT kind, subject_json FROM bearings_items ORDER BY kind")
    assert [k for k, _ in items] == ["mapped", "silent"]
    assert json.loads(items[1][1]) == {"charter_purpose_id": "P2"}


def test_old_index_db_without_bearings_tables_reads_empty(tmp_path, monkeypatch):
    import duckdb

    from bathos.index import index_db_path, init_index_schema

    cat = tmp_path / "catalog"
    cat.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat))
    monkeypatch.setenv("BTH_LOG_MODE", "1")
    con = duckdb.connect(str(index_db_path(cat)))
    init_index_schema(con)
    con.execute("DROP TABLE bearings_readings")
    con.execute("DROP TABLE bearings_items")
    con.close()
    ro = connect_read(cat)
    try:
        assert ro.execute("SELECT count(*) FROM bearings_items").fetchone()[0] == 0
        assert ro.execute("SELECT count(*) FROM bearings_readings").fetchone()[0] == 0
        assert "matched_retired" in [d[0] for d in ro.execute("SELECT * FROM bearings_items").description]
    finally:
        ro.close()


# ── AC-5b: typed links and the reader's uncertainty ──────────────────────────


def write_v2(w, name: str, links, purposes=None, open_lines=None) -> Path:
    path = w["tmp"] / f"{name}.json"
    good = w["good"]
    path.write_text(
        json.dumps(
            {
                "packet_sha256": w["packet_sha"],
                "reader_id": "reader-A",
                "purposes": purposes
                or [
                    {
                        **purpose("r1", good),
                        "uncertain": True,
                        "uncertain_reason": "could be two lines",
                    },
                    purpose("r2", [w["doc_cite"], "run:22222222cccccccc"]),
                ],
                "open_lines": open_lines
                if open_lines is not None
                else [{"text": "stray", "citations": good[:1]}],
                "links": links,
            }
        )
    )
    return path


def link(frm: str, to: str, kind: str = "feeds", cites=None, **extra) -> dict:
    return {
        "from": frm,
        "to": to,
        "type": kind,
        "citations": cites or ["run:22222222cccccccc"],
        **extra,
    }


def test_v2_reading_stores_links_and_uncertainty_verbatim(world):
    links = [
        link("r2", "r1", uncertain=True),
        link("open:0", "r1", "competes"),
        link("r1", "r2", "shares"),
    ]
    res = rec(world, write_v2(world, "v2", links))
    assert res["status"] == "recorded" and res["n_links"] == 3
    stored_links, stored_p = rows(world, "SELECT links_json, purposes_json FROM bearings_readings")[
        0
    ]
    got = json.loads(stored_links)
    assert [(lk["from"], lk["to"], lk["type"]) for lk in got] == [
        ("r2", "r1", "feeds"),
        ("open:0", "r1", "competes"),
        ("r1", "r2", "shares"),
    ]
    assert got[0]["uncertain"] is True
    assert got[0]["citations"] == [{"citation": "run:22222222cccccccc", "resolution": "resolved"}]
    p1 = json.loads(stored_p)[0]
    assert p1["uncertain"] is True and p1["uncertain_reason"] == "could be two lines"


def test_v1_reading_records_as_before_with_empty_links(world):
    res = rec(world, write_reading(world, "v1", [purpose("r1", world["good"])]))
    assert res["status"] == "recorded" and res["n_links"] == 0
    assert rows(world, "SELECT links_json FROM bearings_readings")[0][0] == "[]"


@pytest.mark.parametrize(
    "bad_link",
    [
        link("r9", "r1"),  # unknown purpose id
        link("r1", "open:1"),  # only open:0 exists
        link("r1", "open:x"),  # malformed open ref
        link("r1", "r1"),  # self-link
        link("r1", "r2", "relates"),  # unknown type
        {"from": "r1", "to": "r2", "type": "feeds"},  # no citations list
        link("r1", "r2", uncertain="yes"),  # uncertain not a bool
        "r1->r2",  # not an object
    ],
)
def test_malformed_link_refused_and_nothing_recorded(world, bad_link):
    before = n_events(world)
    with pytest.raises(BearingsReadingInvalidError):
        rec(world, write_v2(world, "badlink", [link("r2", "r1"), bad_link]))
    assert n_events(world) == before


def test_link_without_resolvable_citation_refused_and_named(world):
    before = n_events(world)
    with pytest.raises(BearingsReadingInvalidError) as exc:
        rec(
            world,
            write_v2(
                world, "unres", [link("r2", "r1"), link("r1", "r2", cites=["run:deadbeefdeadbeef"])]
            ),
        )
    assert "link(s) 1" in str(exc.value)
    assert n_events(world) == before


@pytest.mark.parametrize("field,value", [("uncertain", "maybe"), ("uncertain_reason", 3)])
def test_malformed_node_uncertainty_refused(world, field, value):
    purposes = [{**purpose("r1", world["good"]), field: value}]
    with pytest.raises(BearingsReadingInvalidError):
        rec(world, write_v2(world, "badunc", [], purposes=purposes))


def test_old_index_db_without_links_column_gains_it(tmp_path):
    import duckdb

    from bathos.index import BEARINGS_READINGS_DDL, index_db_path, init_index_schema

    cat = tmp_path / "catalog"
    cat.mkdir()
    con = duckdb.connect(str(index_db_path(cat)))
    try:
        con.execute(BEARINGS_READINGS_DDL.replace(",\n    links_json VARCHAR", ""))
        assert "links_json" not in [
            r[0] for r in con.execute("DESCRIBE bearings_readings").fetchall()
        ]
        init_index_schema(con)
        assert "links_json" in [r[0] for r in con.execute("DESCRIBE bearings_readings").fetchall()]
    finally:
        con.close()
