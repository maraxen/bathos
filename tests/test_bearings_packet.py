"""Acceptance tests for the blind bearings packet (spec 261104 slice 2, AC-4)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import duckdb
import pytest

from bathos.bearings.packet import _scrub_abs_paths, build_packet, verify_packet
from bathos.bearings.reading import _PacketView
from bathos.charter import Charter, Purpose, confirm, dump_charter
from bathos.errors import BearingsPacketInvalidError, BearingsPacketMismatchError
from bathos.index import RUNS_COLUMNS, index_db_path, init_index_schema
from bathos.stepback.escalation import AGENT_ENV_VARS

MANDATE = "Build tools that let one researcher trust what ran"
P1 = "Validate binding affinity across the designed library"
P2 = "Verify thermostability of the lead scaffolds under load"
P3 = "Retired idea about screening an old fragment collection"
OLD_PURPOSE = "An earlier purpose that was committed but never confirmed"
SINCE = "2026-01-01T00:00:00+00:00"
UNTIL = "2026-12-31T00:00:00+00:00"
DATE = "2026-06-01T12:00:00+00:00"
TOKEN = "secret_chair_token_xyz"


def git(repo: Path, *args: str, date: str = DATE) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    res = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return res.stdout.strip()


def commit_files(repo: Path, files: dict[str, str], msg: str = "add files", date: str = DATE) -> str:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(repo, "add", "-A", date=date)
    git(repo, "commit", "-q", "-m", msg, date=date)
    return git(repo, "rev-parse", "HEAD")


def write_charter(repo: Path, purposes: dict[str, Purpose] | None = None, mandate: str = MANDATE) -> Path:
    if purposes is None:
        purposes = {
            "P1": Purpose(id="P1", text=P1, state="live"),
            "P2": Purpose(id="P2", text=P2, state="live"),
            "P3": Purpose(id="P3", text=P3, state="retired", retired_reason="superseded"),
        }
    p = repo / ".bth" / "charter.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_charter(Charter(mandate=mandate, purposes=purposes)), encoding="utf-8")
    return p


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".bth").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    from bathos.runlog.writer import reset_writers_for_test

    reset_writers_for_test()
    cat = tmp_path / "catalog"
    cat.mkdir()
    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat))
    monkeypatch.setenv("BTH_LOG_MODE", "1")
    monkeypatch.setenv("BTH_PROJECT_SLUG", "pkt")
    token = home / ".bth" / "chair_token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    monkeypatch.setenv("BTH_CHAIR_TOKEN_PATH", str(token))
    for var in AGENT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".bth.toml").write_text(
        f'[project]\nslug = "pkt"\nroot = "{repo}"\nid = "12345678-1234-5678-1234-567812345678"\n'
    )
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(repo))
    yield {"repo": repo, "cat": cat, "tmp": tmp_path}
    reset_writers_for_test()


def build(env, name: str = "out", **kw):
    kw.setdefault("since", SINCE)
    kw.setdefault("until", UNTIL)
    out = env["tmp"] / name
    res = build_packet(env["repo"], out=out, catalog_dir=env["cat"], **kw)
    return out, res


def reader_files(out: Path) -> dict[str, bytes]:
    r = out / "reader"
    return {p.relative_to(r).as_posix(): p.read_bytes() for p in sorted(r.rglob("*")) if p.is_file()}


def manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text())


def excluded(out: Path) -> dict[str, str]:
    return {e["path"]: e["reason"] for e in manifest(out)["exclusions"]}


def confirmed_repo(env):
    repo = env["repo"]
    write_charter(repo)
    confirm(repo, chair_token=TOKEN, interactive_tty=False, catalog_dir=env["cat"])
    return repo


def insert_runs(env, rows: list[dict]) -> None:
    con = duckdb.connect(str(index_db_path(env["cat"])))
    try:
        init_index_schema(con)
        for r in rows:
            cols = [c for c in RUNS_COLUMNS if c in r]
            con.execute(
                f"INSERT INTO runs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [r[c] for c in cols],
            )
    finally:
        con.close()


def run_ids(out: Path) -> list[str]:
    return [json.loads(x)["id"] for x in (out / "reader/runs.jsonl").read_text().splitlines()]


# ── AC-4 ─────────────────────────────────────────────────────────────────────


def test_narrative_docs_excluded_with_reason(env):
    repo = env["repo"]
    commit_files(
        repo,
        {
            ".praxia/docs/handoffs/260101_h.md": "# handoff\nwe did things\n",
            ".praxia/docs/daily/260101_d.md": "# daily\nnotes\n",
            ".praxia/docs/specs/260101_s.md": "# spec\nA plain spec.\n",
        },
    )
    out, _ = build(env)
    files = reader_files(out)
    assert not any("handoffs" in k or "daily" in k for k in files)
    ex = excluded(out)
    assert ex[".praxia/docs/handoffs/260101_h.md"] == "narrative"
    assert ex[".praxia/docs/daily/260101_d.md"] == "narrative"
    assert "docs/.praxia/docs/specs/260101_s.md" in files


def test_non_primary_category_and_index_excluded(env):
    commit_files(
        env["repo"],
        {
            ".praxia/docs/INDEX.md": "# index\n",
            ".praxia/docs/plans/260101_p.md": "# plan\n",
            ".praxia/docs/decisions/260101_a.md": "# adr\n",
        },
    )
    out, _ = build(env)
    ex = excluded(out)
    assert ex[".praxia/docs/INDEX.md"] == "not_primary_category"
    assert ex[".praxia/docs/plans/260101_p.md"] == "not_primary_category"
    assert "docs/.praxia/docs/decisions/260101_a.md" in reader_files(out)


def test_deterministic_and_byte_identical(env):
    confirmed_repo(env)
    sha = commit_files(
        env["repo"],
        {
            ".praxia/docs/specs/s.md": "# spec\nunrelated text\n",
            "scripts/a.bth.toml": '[experiment]\nhypothesis = "fine"\n',
        },
        msg="spec",
    )
    # Inserted out of timestamp order, so a build that kept row order would differ from a sorted one.
    insert_runs(
        env,
        [
            {"id": "bbbbbbbb00000002", "project_slug": "pkt", "timestamp": "2026-06-02T00:00:00+00:00",
             "git_hash": sha, "sidecar_path": "scripts/a.bth.toml", "command": "python a", "status": "completed"},
            {"id": "aaaaaaaa00000001", "project_slug": "pkt", "timestamp": DATE,
             "git_hash": sha, "sidecar_path": "scripts/a.bth.toml", "command": "python a", "status": "completed"},
        ],
    )
    a, ra = build(env, "a")
    b, rb = build(env, "b")
    assert ra["packet_sha256"] == rb["packet_sha256"]
    assert reader_files(a) == reader_files(b)
    assert verify_packet(a) == ra["packet_sha256"]
    assert run_ids(a) == ["aaaaaaaa00000001", "bbbbbbbb00000002"]
    assert ra["n_sidecars"] == 1

    # An unrelated run outside the window must not move the hash.
    insert_runs(
        env,
        [{"id": "cccccccc00000003", "project_slug": "pkt", "timestamp": "2025-01-01T00:00:00+00:00",
          "git_hash": sha, "command": "python old", "status": "completed"}],
    )
    assert build(env, "c")[1]["packet_sha256"] == ra["packet_sha256"]


def test_absolute_paths_do_not_reach_the_reader_or_move_the_hash(env):
    """The same work recorded from two checkouts must give the same packet."""
    confirmed_repo(env)
    sha = commit_files(env["repo"], {"scripts/a.py": "print(1)\n"})

    def run(rid, command):
        return {"id": rid, "project_slug": "pkt", "timestamp": DATE, "git_hash": sha,
                "command": command, "status": "completed"}

    insert_runs(env, [run("aaaaaaaa00000001", "python /home/me/projects/pkt/scripts/a.py --out=/orcd/host9/x.json")])
    a, ra = build(env, "a")
    text = (a / "reader/runs.jsonl").read_text()
    assert "/home/me" not in text and "/orcd" not in text and "host9" not in text
    assert "<abs>/a.py" in text and "<abs>/x.json" in text

    con = duckdb.connect(str(index_db_path(env["cat"])))
    con.execute("DELETE FROM runs")
    con.close()
    insert_runs(env, [run("aaaaaaaa00000001", "python /scratch/wt/pkt/scripts/a.py --out=/tmp/other/x.json")])
    assert build(env, "b")[1]["packet_sha256"] == ra["packet_sha256"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("gcc -I/home/u/proj/inc -o a", "gcc -I<abs>/inc -o a"),
        ("PATH=/a/b:/c/d run", "PATH=<abs>/b:<abs>/d run"),
        ("open file:///home/u/x.txt", "open <abs>/x.txt"),
        ("cat /home/u/a=b", "cat <abs>/a=b"),
        ("--flag=/x/y", "--flag=<abs>/y"),
        ("a/b ./c 1/2 ../d", "a/b ./c 1/2 ../d"),
        ("curl https://example.org/a/b", "curl https://example.org/a/b"),
    ],
)
def test_scrub_abs_paths(raw, expected):
    assert _scrub_abs_paths(raw) == expected


def test_needle_with_a_line_leading_marker_matches_when_quoted_inline(env):
    repo = env["repo"]
    text = "close the long-standing issue\n#12 about affinity drift"
    write_charter(repo, {"P1": Purpose(id="P1", text=text, state="live")})
    commit_files(repo, {".praxia/docs/specs/q.md": "We will close the long-standing issue #12 about affinity drift.\n"})
    out, _ = build(env)
    assert excluded(out)[".praxia/docs/specs/q.md"] == "quotes_charter"


def test_url_in_a_command_is_left_alone(env):
    confirmed_repo(env)
    sha = commit_files(env["repo"], {"scripts/a.py": "print(1)\n"})
    insert_runs(env, [{"id": "aaaaaaaa00000001", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha,
                       "command": "curl https://example.org/a/b.json -o rel/c.json", "status": "completed"}])
    out, _ = build(env)
    assert "https://example.org/a/b.json -o rel/c.json" in (out / "reader/runs.jsonl").read_text()


def test_running_run_is_excluded_so_a_later_build_cannot_differ(env):
    confirmed_repo(env)
    sha = commit_files(env["repo"], {"scripts/a.py": "print(1)\n"})
    insert_runs(env, [{"id": "aaaaaaaa00000001", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha,
                       "command": "python a", "status": "running"}])
    out, _ = build(env)
    assert run_ids(out) == []
    assert excluded(out)["run:aaaaaaaa00000001"] == "not_terminal"


def test_commit_subject_with_a_unicode_line_separator_is_kept_whole(env):
    """splitlines() would truncate this subject going in, and break the record coming back."""
    confirmed_repo(env)
    sha = commit_files(env["repo"], {"scripts/a.py": "print(1)\n"}, msg="ordinary subject")
    out, _ = build(env)
    log = {
        e["sha"]: e["subject"]
        for e in (json.loads(line) for line in (out / "reader/git_log.jsonl").read_text().split("\n") if line)
    }
    assert log[sha] == "ordinary subject"
    assert sha in _PacketView(out).git_shas


def test_working_tree_edit_does_not_move_hash_but_commit_does(env):
    repo = env["repo"]
    commit_files(repo, {".praxia/docs/specs/s.md": "# spec\nversion one\n"}, msg="v1")
    _, rb = build(env, "base")
    (repo / ".praxia/docs/specs/s.md").write_text("# spec\nversion two uncommitted\n")
    dirty, rd = build(env, "dirty")
    assert rd["packet_sha256"] == rb["packet_sha256"]
    assert b"uncommitted" not in b"".join(reader_files(dirty).values())
    commit_files(repo, {".praxia/docs/specs/s.md": "# spec\nversion two uncommitted\n"}, msg="v2")
    _, rn = build(env, "new")
    assert rn["packet_sha256"] != rb["packet_sha256"]


def test_adr_quoting_purpose_with_different_wrapping_excluded(env):
    repo = confirmed_repo(env)
    wrapped = P1.replace(" ", "\n   ", 3)
    assert wrapped != P1
    commit_files(
        repo,
        {
            ".praxia/docs/decisions/quote.md": f"# ADR\nWe decided: {wrapped} because reasons.\n",
            ".praxia/docs/specs/clean.md": "# spec\nNo quote here at all.\n",
        },
    )
    out, _ = build(env)
    files = reader_files(out)
    assert excluded(out)[".praxia/docs/decisions/quote.md"] == "quotes_charter"
    assert "docs/.praxia/docs/specs/clean.md" in files
    assert not any(k.endswith("quote.md") for k in files)


def test_adr_quoting_purpose_in_wrapped_blockquote_excluded(env):
    """A wrapped markdown blockquote puts '>' inside the quote at each line break."""
    repo = confirmed_repo(env)
    quoted = P1.replace(" ", "\n> ", 2)
    assert quoted != P1
    commit_files(repo, {".praxia/docs/decisions/bq.md": f"# ADR\n\n> {quoted}\n\nDecided.\n"})
    out, _ = build(env)
    assert excluded(out)[".praxia/docs/decisions/bq.md"] == "quotes_charter"
    assert not any(k.endswith("bq.md") for k in reader_files(out))


def test_sidecar_quoting_purpose_in_wrapped_toml_comments_excluded(env):
    repo = confirmed_repo(env)
    commented = P1.replace(" ", "\n# ", 2)
    sha = commit_files(repo, {"scripts/c.bth.toml": f'# {commented}\n[experiment]\nhypothesis = "x"\n'})
    insert_runs(env, [{"id": "aaaaaaaa00000001", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha,
                       "sidecar_path": "scripts/c.bth.toml", "command": "python c", "status": "completed"}])
    out, _ = build(env)
    assert any(k.startswith("sidecar:scripts/c.bth.toml@") and v == "quotes_charter" for k, v in excluded(out).items())
    assert not any(k.endswith("c.bth.toml") for k in reader_files(out))


@pytest.mark.parametrize(
    "field,value",
    [
        ("tags", json.dumps([f"note:{P1}"])),
        ("command", "python x --note " + P1.replace(" ", "\\ ")),
    ],
)
def test_run_field_quoting_purpose_through_an_escape_excluded(env, field, value):
    confirmed_repo(env)
    sha = commit_files(env["repo"], {"scripts/a.py": "print(1)\n"})
    row = {"id": "aaaaaaaa00000001", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha,
           "command": "python x", "status": "completed"}
    row[field] = value
    insert_runs(env, [row])
    out, _ = build(env)
    assert excluded(out)["run:aaaaaaaa00000001"] == "quotes_charter"


def test_tag_with_json_unicode_escape_quoting_purpose_excluded(env):
    """ensure_ascii tags store non-ASCII as \\\\uXXXX, which a raw match cannot see."""
    purpose = "Validate café binding across the library"
    repo = confirmed_repo(env)
    write_charter(repo, {"P1": Purpose(id="P1", text=purpose, state="live")})
    sha = commit_files(repo, {"scripts/a.py": "print(1)\n"})
    insert_runs(env, [{"id": "aaaaaaaa00000001", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha,
                       "command": "python x", "status": "completed",
                       "tags": json.dumps([purpose], ensure_ascii=True)}])
    out, _ = build(env)
    assert excluded(out)["run:aaaaaaaa00000001"] == "quotes_charter"


def test_charter_committed_at_the_ref_is_a_needle(env):
    """A purpose that exists only in the charter at --ref still must not reach the reader."""
    repo = confirmed_repo(env)
    commit_files(
        repo,
        {
            ".bth/charter.toml": f'mandate = "m"\n[purpose.old]\ntext = "{OLD_PURPOSE}"\nstate = "live"\n',
            ".praxia/docs/specs/old.md": f"see {OLD_PURPOSE} here\n",
        },
    )
    ref = git(repo, "rev-parse", "HEAD")
    write_charter(repo)  # the working file no longer has it
    out, _ = build(env, ref=ref)
    assert excluded(out)[".praxia/docs/specs/old.md"] == "quotes_charter"


def test_every_confirmed_charter_is_a_needle_not_only_the_latest(env):
    repo = confirmed_repo(env)
    write_charter(repo, {"P1": Purpose(id="P1", text=P1, state="live")})
    confirm(repo, chair_token=TOKEN, interactive_tty=False, catalog_dir=env["cat"])  # P2 gone from latest row
    write_charter(repo, {"P1": Purpose(id="P1", text=P1, state="live")})
    commit_files(repo, {".praxia/docs/specs/p2.md": f"see {P2} here\n"})
    out, _ = build(env)
    assert excluded(out)[".praxia/docs/specs/p2.md"] == "quotes_charter"


def test_bare_filename_needs_the_recorded_hash(env):
    """A same-named file at the root is not the sidecar unless its bytes match."""
    repo = confirmed_repo(env)
    body = b'[experiment]\nhypothesis = "unrelated root file"\n'
    sha = commit_files(repo, {"w.bth.toml": body.decode()})

    def run(rid, recorded_sha):
        return {"id": rid, "project_slug": "pkt", "timestamp": DATE, "git_hash": sha, "command": "python w",
                "status": "completed", "sidecar_path": "/orcd/host/pkt/scripts/w.bth.toml",
                "sidecar_sha256": recorded_sha}

    insert_runs(env, [run("aaaaaaaa00000001", "0" * 64)])
    wrong, _ = build(env, "wrong")
    assert json.loads((wrong / "reader/index.json").read_text())["sidecars"] == []
    assert manifest(wrong)["sidecar_unresolved"][0]["run"] == "aaaaaaaa00000001"

    insert_runs(env, [run("bbbbbbbb00000002", hashlib.sha256(body).hexdigest())])
    right, _ = build(env, "right")
    assert [s["path"] for s in json.loads((right / "reader/index.json").read_text())["sidecars"]] == ["w.bth.toml"]


@pytest.mark.parametrize(
    "recorded",
    [
        "{repo}/.claude/worktrees/some-branch/scripts/sub/w.bth.toml",
        "/orcd/pool/008/so3_shared/marielle/projects/pkt/scripts/sub/w.bth.toml",
        "/home/someone/projects/.worktrees/pkt/loop-e1/scripts/sub/w.bth.toml",
        "scripts/sub/w.bth.toml",
    ],
)
def test_sidecar_recorded_from_a_worktree_or_cluster_path_is_found(env, recorded):
    """Most real runs record an absolute path from a worktree or cluster copy, not this checkout."""
    repo = confirmed_repo(env)
    sha = commit_files(repo, {"scripts/sub/w.bth.toml": '[experiment]\nhypothesis = "fine"\n'})
    insert_runs(
        env,
        [{"id": "cccccccc33333333", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha,
          "sidecar_path": recorded.format(repo=repo), "command": "python w"}],
    )
    out, _ = build(env)
    idx = json.loads((out / "reader/index.json").read_text())
    assert [s["path"] for s in idx["sidecars"]] == ["scripts/sub/w.bth.toml"]
    run = json.loads((out / "reader/runs.jsonl").read_text().splitlines()[0])
    assert run["sidecar_path"] == "scripts/sub/w.bth.toml"
    assert manifest(out)["sidecar_unresolved"] == []


def test_unfindable_sidecar_is_reported_and_its_path_never_reaches_the_reader(env):
    repo = confirmed_repo(env)
    sha = commit_files(repo, {"scripts/other.py": "print(1)\n"})
    gone = "/orcd/pool/secret-host/projects/pkt/scripts/gone.bth.toml"
    insert_runs(
        env,
        [{"id": "dddddddd44444444", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha,
          "sidecar_path": gone, "command": "python g"}],
    )
    out, _ = build(env)
    assert manifest(out)["sidecar_unresolved"] == [{"run": "dddddddd44444444", "path": gone}]
    assert json.loads((out / "reader/runs.jsonl").read_text().splitlines()[0])["sidecar_path"] is None
    assert not any(b"secret-host" in data for data in reader_files(out).values())


@pytest.mark.parametrize("which,text", [("mandate", MANDATE), ("retired", P3)])
def test_mandate_and_retired_purpose_quotes_excluded(env, which, text):
    repo = confirmed_repo(env)
    commit_files(repo, {f".praxia/docs/specs/{which}.md": f"intro\n{text}\noutro\n"})
    out, _ = build(env)
    assert excluded(out)[f".praxia/docs/specs/{which}.md"] == "quotes_charter"


def test_purpose_only_in_confirmed_row_excluded(env):
    """A purpose dropped from the file after confirmation is still a needle via the confirmed row."""
    repo = confirmed_repo(env)
    write_charter(repo, {"P1": Purpose(id="P1", text=P1, state="live")})
    commit_files(repo, {".praxia/docs/specs/p2.md": f"see {P2} here\n"})
    out, _ = build(env)
    assert excluded(out)[".praxia/docs/specs/p2.md"] == "quotes_charter"


def test_git_subject_and_sidecar_quoting_purpose_excluded(env):
    repo = confirmed_repo(env)
    commit_files(repo, {"scripts/x.py": "print(1)\n"}, msg=f"feat: {P1}")
    sha_q = git(repo, "rev-parse", "HEAD")
    sha_ok = commit_files(
        repo,
        {
            "scripts/y.py": "print(2)\n",
            "scripts/y.bth.toml": f'[experiment]\nhypothesis = "{P2}"\n',
            "scripts/z.bth.toml": '[experiment]\nhypothesis = "harmless thing"\n',
        },
        msg="ordinary subject",
    )
    insert_runs(
        env,
        [
            {"id": "aaaaaaaa11111111", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha_ok,
             "sidecar_path": "scripts/y.bth.toml", "command": "python y"},
            {"id": "bbbbbbbb22222222", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha_ok,
             "sidecar_path": "scripts/z.bth.toml", "command": "python z"},
        ],
    )
    out, _ = build(env)
    ex = excluded(out)
    assert ex[f"git:{sha_q}"] == "quotes_charter"
    assert any(k.startswith("sidecar:scripts/y.bth.toml@") and v == "quotes_charter" for k, v in ex.items())
    idx = json.loads((out / "reader/index.json").read_text())
    assert [s["path"] for s in idx["sidecars"]] == ["scripts/z.bth.toml"]
    log = [json.loads(line) for line in (out / "reader/git_log.jsonl").read_text().splitlines()]
    assert sha_q not in {r["sha"] for r in log}


def test_run_record_quoting_purpose_excluded(env):
    repo = confirmed_repo(env)
    sha = commit_files(repo, {"a.txt": "a\n"})
    insert_runs(
        env,
        [
            {"id": "12121212aaaaaaaa", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha,
             "command": f"python go.py --note '{P1}'"},
            {"id": "34343434bbbbbbbb", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha,
             "command": "python go.py"},
        ],
    )
    out, _ = build(env)
    assert run_ids(out) == ["34343434bbbbbbbb"]
    assert excluded(out)["run:12121212aaaaaaaa"] == "quotes_charter"


def test_charter_file_option_covers_no_committed_charter(env):
    repo = env["repo"]
    extra = env["tmp"] / "old_charter.toml"
    extra.write_text(dump_charter(Charter(mandate=MANDATE, purposes={"X": Purpose(id="X", text=P1, state="live")})))
    commit_files(repo, {".praxia/docs/decisions/adr.md": f"We chose: {P1}\n"})
    without, _ = build(env, "no")
    assert "docs/.praxia/docs/decisions/adr.md" in reader_files(without)  # control: no charter => included
    out, _ = build(env, "yes", charter_file=extra)
    assert excluded(out)[".praxia/docs/decisions/adr.md"] == "quotes_charter"


def test_empty_mandate_does_not_exclude_everything_and_short_needle_listed(env):
    repo = env["repo"]
    write_charter(repo, {"S": Purpose(id="S", text="short", state="live")}, mandate="")
    commit_files(repo, {".praxia/docs/specs/s.md": "# spec\nshort and sweet\n"})
    out, _ = build(env)
    assert "docs/.praxia/docs/specs/s.md" in reader_files(out)
    skipped = manifest(out)["short_needles_skipped"]
    assert [s["source"] for s in skipped] == ["charter:purpose:S", "ref:purpose:S"]


def test_harness_and_charter_files_absent(env):
    repo = confirmed_repo(env)
    commit_files(
        repo,
        {
            "AGENTS.md": "agents text\n",
            ".praxia/docs/specs/CLAUDE.md": "nested harness\n",
            ".praxia/docs/specs/ok.md": "# ok\n",
        },
    )
    out, _ = build(env)
    files = reader_files(out)
    blob = b"\n".join(files.values()).decode()
    for k in files:
        assert "AGENTS.md" not in k
        assert "CLAUDE.md" not in k
        assert "charter" not in k
    for text in (MANDATE, P1, P2, P3):
        assert text not in blob
    ex = excluded(out)
    assert ex[".praxia/docs/specs/CLAUDE.md"] == "harness_file"
    assert ex["AGENTS.md"] == "harness_file"
    assert ex[".bth/charter.toml"] == "charter"


def test_manifest_outside_reader_and_reader_has_no_exclusion_paths(env):
    commit_files(
        env["repo"],
        {
            ".praxia/docs/handoffs/260101_secret-handoff.md": "narrative\n",
            ".praxia/docs/specs/a.md": "# a\n",
        },
    )
    out, _ = build(env)
    assert (out / "manifest.json").is_file()
    assert not (out / "reader" / "manifest.json").exists()
    files = reader_files(out)
    blob = b"\n".join(files.values()).decode()
    names = "\n".join(files)
    assert excluded(out)
    for path in excluded(out):
        assert path not in blob
        assert path not in names


def test_no_absolute_or_out_paths_in_reader(env):
    repo = env["repo"]
    sha = commit_files(
        repo,
        {".praxia/docs/specs/a.md": "# a\n", "scripts/s.bth.toml": "[experiment]\nhypothesis='h'\n"},
    )
    insert_runs(
        env,
        [{"id": "cccccccc33333333", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha,
          "sidecar_path": str(repo / "scripts/s.bth.toml"), "command": f"python {repo}/scripts/s.py"}],
    )
    out, _ = build(env)
    blob = b"\n".join(reader_files(out).values()).decode()
    assert str(repo) not in blob
    assert str(env["tmp"]) not in blob
    run = json.loads((out / "reader/runs.jsonl").read_text().splitlines()[0])
    assert run["sidecar_path"] == "scripts/s.bth.toml"
    assert run["command"] == "python <abs>/s.py"
    idx = json.loads((out / "reader/index.json").read_text())
    assert [s["path"] for s in idx["sidecars"]] == ["scripts/s.bth.toml"]


def test_reachable_only_drops_unmerged_branch_run(env):
    repo = env["repo"]
    main_sha = commit_files(repo, {"a.txt": "a\n"}, msg="main work")
    git(repo, "checkout", "-q", "-b", "feature")
    feat_sha = commit_files(repo, {"b.txt": "b\n"}, msg="feature work")
    git(repo, "checkout", "-q", "main")
    insert_runs(
        env,
        [
            {"id": "dddddddd44444444", "project_slug": "pkt", "timestamp": DATE, "git_hash": main_sha},
            {"id": "eeeeeeee55555555", "project_slug": "pkt", "timestamp": DATE, "git_hash": feat_sha},
        ],
    )
    allruns, _ = build(env, "all")
    assert set(run_ids(allruns)) == {"dddddddd44444444", "eeeeeeee55555555"}  # control: default keeps it
    out, _ = build(env, "reach", reachable_only=True)
    assert run_ids(out) == ["dddddddd44444444"]
    assert excluded(out)["run:eeeeeeee55555555"] == "unreachable_from_ref"


def test_window_filters_runs_and_commits(env):
    repo = env["repo"]
    commit_files(repo, {"a.txt": "a\n"}, msg="old", date="2020-01-01T00:00:00+00:00")
    sha = commit_files(repo, {"b.txt": "b\n"}, msg="in window")
    insert_runs(
        env,
        [
            {"id": "ffffffff66666666", "project_slug": "pkt", "timestamp": "2020-01-01T00:00:00+00:00",
             "git_hash": sha},
            {"id": "99999999aaaaaaaa", "project_slug": "pkt", "timestamp": DATE, "git_hash": sha},
            {"id": "88888888bbbbbbbb", "project_slug": "other", "timestamp": DATE, "git_hash": sha},
        ],
    )
    out, _ = build(env)
    assert run_ids(out) == ["99999999aaaaaaaa"]
    subjects = [json.loads(x)["subject"] for x in (out / "reader/git_log.jsonl").read_text().splitlines()]
    assert subjects == ["in window"]


def test_non_empty_out_refused(env):
    commit_files(env["repo"], {"a.txt": "a\n"})
    out = env["tmp"] / "busy"
    out.mkdir()
    (out / "leftover").write_text("x")
    with pytest.raises(BearingsPacketInvalidError):
        build_packet(env["repo"], out=out, since=SINCE, until=UNTIL, catalog_dir=env["cat"])
    assert (out / "leftover").read_text() == "x"
    assert not (out / "reader").exists()


def test_tampered_packet_fails_verify(env):
    commit_files(env["repo"], {".praxia/docs/specs/a.md": "# a\nbody\n"})
    out, _ = build(env)
    verify_packet(out)
    f = out / "reader/docs/.praxia/docs/specs/a.md"
    f.write_bytes(f.read_bytes().replace(b"body", b"b0dy"))
    with pytest.raises(BearingsPacketMismatchError):
        verify_packet(out)


def test_backlog_titles_optional(env):
    commit_files(env["repo"], {"a.txt": "a\n"})
    none_out, _ = build(env, "n")
    assert manifest(none_out)["backlog"] == "not_provided"
    assert not (none_out / "reader/backlog_titles.txt").exists()
    titles = env["tmp"] / "titles.txt"
    titles.write_text("Zeta task\nAlpha task\n\n")
    out, _ = build(env, "t", backlog_titles=titles)
    assert (out / "reader/backlog_titles.txt").read_text() == "Alpha task\nZeta task\n"
    assert manifest(out)["backlog"] == "provided"
