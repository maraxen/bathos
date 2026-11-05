"""Deterministic blind packet for a bearings reader (spec 261104, Design item 4; slice 2 plan C2).

The packet is built from the git object store at one resolved commit, never from the working
tree, so a dirty tree cannot change its hash. Everything under ``reader/`` is what the blind
reader may see; ``manifest.json`` (exclusions, hash) sits beside it and is never shown.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bathos.errors import BearingsPacketInvalidError, BearingsPacketMismatchError

logger = logging.getLogger(__name__)

MIN_NEEDLE_CHARS = 12
HARNESS_BASENAMES = frozenset({"CLAUDE.md", "AGENTS.md", "GEMINI.md"})
ROOT_HARNESS_PATHS = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")
CHARTER_PATHS = (".bth/charter.toml", ".bth/charter.draft.toml")
DOCS_ROOT = ".praxia/docs"
PRIMARY_DOC_DIRS = (f"{DOCS_ROOT}/decisions/", f"{DOCS_ROOT}/specs/")
NARRATIVE_DOC_DIRS = (f"{DOCS_ROOT}/handoffs/", f"{DOCS_ROOT}/daily/")

RUN_FIELDS = (
    "id",
    "timestamp",
    "command",
    "git_hash",
    "git_branch",
    "status",
    "outcome",
    "exit_code",
    "tags",
    "campaign_id",
    "sidecar_path",
    "sidecar_sha256",
)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _norm(text: str) -> str:
    return " ".join(text.split())


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; a naive value is taken as UTC."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 -- explicit argv, no shell
        ["git", "-C", str(root), *args], check=check, capture_output=True
    )


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _json_bytes(obj: Any) -> bytes:
    return (canonical_json(obj) + "\n").encode("utf-8")


def _needles(
    root: Path,
    slug: str,
    charter_files: Sequence[Path],
    cat_dir: Path,
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """Return ``([(source, normalised text)], short_needles_skipped)``.

    Sources: the current charter file, every ``--charter-file``, and the latest
    ``charter_confirmations`` row (so a pivot cannot leak an older purpose).
    """
    from bathos.charter import parse_charter
    from bathos.index import catalog_readable, connect_read

    raw: list[tuple[str, str]] = []
    own = root / ".bth" / "charter.toml"
    files: list[tuple[str, Path]] = []
    if own.is_file():
        files.append(("charter", own))
    files.extend((f"charter_file:{p.name}", p) for p in charter_files)
    for label, p in files:
        ch = parse_charter(p)
        raw.append((f"{label}:mandate", ch.mandate))
        for pid in sorted(ch.purposes):
            raw.append((f"{label}:purpose:{pid}", ch.purposes[pid].text))

    if catalog_readable(cat_dir):
        con = connect_read(cat_dir)
        try:
            row = con.execute(
                "SELECT mandate_text, purposes_json FROM charter_confirmations WHERE project = ? "
                "ORDER BY ts DESC, eid DESC LIMIT 1",
                [slug],
            ).fetchone()
        finally:
            con.close()
        if row:
            raw.append(("confirmed:mandate", row[0] or ""))
            purposes = json.loads(row[1]) if row[1] else {}
            for pid in sorted(purposes):
                raw.append((f"confirmed:purpose:{pid}", str(purposes[pid].get("text", ""))))

    needles: list[tuple[str, str]] = []
    short: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, text in raw:
        n = _norm(text)
        if not n:
            continue
        if len(n) < MIN_NEEDLE_CHARS:
            short.append({"source": source, "length": len(n)})
            continue
        if n in seen:
            continue
        seen.add(n)
        needles.append((source, n))
    return needles, short


def _quotes(text: str, needles: list[tuple[str, str]]) -> bool:
    hay = _norm(text)
    return any(n in hay for _, n in needles)


def _scrub(value: Any, root_str: str) -> Any:
    if isinstance(value, str):
        return value.replace(root_str + "/", "").replace(root_str, "")
    return value


def _rel_sidecar(path_str: str | None, root: Path) -> str | None:
    if not path_str:
        return None
    p = Path(path_str)
    if p.is_absolute():
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            return None
    return p.as_posix().removeprefix("./")


def _packet_hash(reader_dir: Path) -> str:
    entries = []
    for f in sorted(p for p in reader_dir.rglob("*") if p.is_file()):
        rel = f.relative_to(reader_dir).as_posix()
        entries.append([rel, hashlib.sha256(f.read_bytes()).hexdigest()])
    entries.sort()
    return hashlib.sha256(canonical_json(entries).encode("utf-8")).hexdigest()


def verify_packet(out: Path | str) -> str:
    """Recompute the packet hash from ``reader/`` and compare it with ``manifest.json``."""
    out_p = Path(out)
    manifest_p = out_p / "manifest.json"
    reader = out_p / "reader"
    if not manifest_p.is_file() or not reader.is_dir():
        raise BearingsPacketInvalidError(f"Not a bearings packet (manifest.json or reader/ missing): {out_p}")
    try:
        recorded = json.loads(manifest_p.read_text(encoding="utf-8"))["packet_sha256"]
    except (ValueError, KeyError) as e:
        raise BearingsPacketInvalidError(f"Unreadable packet manifest at {manifest_p}: {e}") from e
    actual = _packet_hash(reader)
    if actual != recorded:
        raise BearingsPacketMismatchError(
            f"Packet bytes do not match the recorded hash ({actual[:12]} != {str(recorded)[:12]}); "
            "the packet was altered after it was built"
        )
    return actual


def _reachable(root: Path, sha: str, ref_sha: str, cache: dict[str, bool]) -> bool:
    if sha not in cache:
        proc = _git(root, "merge-base", "--is-ancestor", sha, ref_sha, check=False)
        cache[sha] = proc.returncode == 0
    return cache[sha]


def build_packet(
    project_root: Path | str,
    *,
    out: Path | str,
    since: str,
    until: str,
    ref: str = "HEAD",
    project: str | None = None,
    charter_file: Path | str | Sequence[Path | str] | None = None,
    backlog_titles: Path | str | None = None,
    reachable_only: bool = False,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the blind packet into ``out`` and return a summary including ``packet_sha256``."""
    from bathos.charter import _resolve_catalog_dir, _resolve_project_slug
    from bathos.index import catalog_readable, connect_read

    root = Path(project_root).resolve()
    out_p = Path(out)
    if out_p.exists() and (not out_p.is_dir() or any(out_p.iterdir())):
        raise BearingsPacketInvalidError(f"--out must be empty or absent: {out_p}")
    try:
        since_dt, until_dt = parse_ts(since), parse_ts(until)
    except ValueError as e:
        raise BearingsPacketInvalidError(f"--since/--until must be ISO-8601 timestamps: {e}") from e
    if since_dt >= until_dt:
        raise BearingsPacketInvalidError("--since must be earlier than --until")

    try:
        top = _git(root, "rev-parse", "--show-toplevel").stdout.decode().strip()
        ref_sha = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.decode().strip()
    except subprocess.CalledProcessError as e:
        raise BearingsPacketInvalidError(
            f"Cannot resolve --ref {ref!r} in {root}: {e.stderr.decode(errors='replace').strip()}"
        ) from e
    repo = Path(top).resolve()
    root_str = str(repo)

    slug = project or _resolve_project_slug(root)
    cat_dir = _resolve_catalog_dir(catalog_dir)
    if charter_file is None:
        charter_files: list[Path] = []
    elif isinstance(charter_file, (str, Path)):
        charter_files = [Path(charter_file)]
    else:
        charter_files = [Path(c) for c in charter_file]
    needles, short_skipped = _needles(root, slug, charter_files, cat_dir)

    exclusions: list[dict[str, str]] = []

    # ── Docs, from the object store at ref_sha ──────────────────────────────────
    docs: list[dict[str, str]] = []
    doc_bytes: dict[str, bytes] = {}
    listing = _git(repo, "ls-tree", "-r", "-z", "--full-tree", ref_sha, "--", DOCS_ROOT).stdout
    for rec in sorted(r for r in listing.split(b"\0") if r):
        meta, _, raw_path = rec.partition(b"\t")
        mode, otype, blob_sha = meta.decode().split()
        path = raw_path.decode("utf-8", errors="surrogateescape")
        base = path.rsplit("/", 1)[-1]
        if otype != "blob" or mode == "120000":
            exclusions.append({"path": path, "reason": "not_text"})
        elif base in HARNESS_BASENAMES:
            exclusions.append({"path": path, "reason": "harness_file"})
        elif path.startswith(NARRATIVE_DOC_DIRS):
            exclusions.append({"path": path, "reason": "narrative"})
        elif not path.startswith(PRIMARY_DOC_DIRS):
            exclusions.append({"path": path, "reason": "not_primary_category"})
        else:
            data = _git(repo, "cat-file", "blob", blob_sha).stdout
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                exclusions.append({"path": path, "reason": "not_text"})
                continue
            if _quotes(text, needles):
                exclusions.append({"path": path, "reason": "quotes_charter"})
                continue
            docs.append({"path": path, "blob_sha": blob_sha})
            doc_bytes[path] = data
    for path in (*ROOT_HARNESS_PATHS, *CHARTER_PATHS):
        proc = _git(repo, "cat-file", "-e", f"{ref_sha}:{path}", check=False)
        if proc.returncode == 0:
            reason = "harness_file" if path in ROOT_HARNESS_PATHS else "charter"
            exclusions.append({"path": path, "reason": reason})

    # ── Runs and their sidecars ─────────────────────────────────────────────────
    runs: list[dict[str, Any]] = []
    sidecars: dict[tuple[str, str], bytes] = {}
    sidecar_unresolved: list[dict[str, str]] = []
    if catalog_readable(cat_dir):
        con = connect_read(cat_dir)
        try:
            have = {r[0] for r in con.execute("SELECT column_name FROM (DESCRIBE runs)").fetchall()}
            cols = [c for c in RUN_FIELDS if c in have]
            rows = con.execute(
                f"SELECT {', '.join(cols)} FROM runs WHERE project_slug = ?",  # noqa: S608 -- cols are constants
                [slug],
            ).fetchall()
        finally:
            con.close()
        reach_cache: dict[str, bool] = {}
        for raw in rows:
            rec = dict(zip(cols, raw, strict=True))
            rid = str(rec.get("id"))
            try:
                ts = parse_ts(str(rec.get("timestamp")))
            except ValueError:
                exclusions.append({"path": f"run:{rid}", "reason": "bad_timestamp"})
                continue
            if not (since_dt <= ts < until_dt):
                continue
            gh = rec.get("git_hash")
            if reachable_only and not (gh and _reachable(repo, str(gh), ref_sha, reach_cache)):
                exclusions.append({"path": f"run:{rid}", "reason": "unreachable_from_ref"})
                continue
            rec = {k: _scrub(v, root_str) for k, v in rec.items()}
            if any(isinstance(v, str) and _quotes(v, needles) for v in rec.values()):
                exclusions.append({"path": f"run:{rid}", "reason": "quotes_charter"})
                continue
            rel = _rel_sidecar(rec.get("sidecar_path"), repo)
            if rec.get("sidecar_path") and rel is None:
                sidecar_unresolved.append({"run": rid, "path": "outside_repo"})
            rec["sidecar_path"] = rel
            runs.append((ts, rec))  # type: ignore[arg-type]
    runs_sorted = [rec for _, rec in sorted(runs, key=lambda t: (t[0], str(t[1]["id"])))]  # type: ignore[index]
    for rec in runs_sorted:
        rel = rec.get("sidecar_path")
        if not rel:
            continue
        found = False
        for rev in (str(rec.get("git_hash") or ""), ref_sha):
            if not rev:
                continue
            proc = _git(repo, "cat-file", "blob", f"{rev}:{rel}", check=False)
            if proc.returncode != 0:
                continue
            blob = _git(repo, "rev-parse", f"{rev}:{rel}").stdout.decode().strip()
            if (rel, blob) in sidecars or any(s["path"] == f"sidecar:{rel}@{blob}" for s in exclusions):
                found = True
                break
            try:
                text = proc.stdout.decode("utf-8")
            except UnicodeDecodeError:
                exclusions.append({"path": f"sidecar:{rel}@{blob}", "reason": "not_text"})
                found = True
                break
            if _quotes(text, needles):
                exclusions.append({"path": f"sidecar:{rel}@{blob}", "reason": "quotes_charter"})
            else:
                sidecars[(rel, blob)] = proc.stdout
            found = True
            break
        if not found:
            sidecar_unresolved.append({"run": str(rec["id"]), "path": rel})

    # ── Git subjects in the window ──────────────────────────────────────────────
    git_log: list[dict[str, str]] = []
    log_out = _git(repo, "log", ref_sha, "--format=%H%x00%cI%x00%s").stdout.decode("utf-8", errors="replace")
    parsed_log: list[tuple[datetime, dict[str, str]]] = []
    for line in log_out.splitlines():
        parts = line.split("\0", 2)
        if len(parts) != 3:
            continue
        sha, cts, subject = parts
        cdt = parse_ts(cts)
        if not (since_dt <= cdt < until_dt):
            continue
        if _quotes(subject, needles):
            exclusions.append({"path": f"git:{sha}", "reason": "quotes_charter"})
            continue
        parsed_log.append((cdt, {"sha": sha, "committed_at": cts, "subject": subject}))
    git_log = [rec for _, rec in sorted(parsed_log, key=lambda t: (t[0], t[1]["sha"]))]

    # ── Backlog titles (optional) ───────────────────────────────────────────────
    titles: list[str] | None = None
    if backlog_titles is not None:
        titles = []
        for n, line in enumerate(Path(backlog_titles).read_text(encoding="utf-8").splitlines()):
            t = line.strip()
            if not t:
                continue
            if _quotes(t, needles):
                exclusions.append({"path": f"backlog:line{n + 1}", "reason": "quotes_charter"})
                continue
            titles.append(t)
        titles.sort()

    # ── Write ───────────────────────────────────────────────────────────────────
    reader = out_p / "reader"
    reader.mkdir(parents=True, exist_ok=True)
    docs.sort(key=lambda d: d["path"])
    sidecar_index = [{"path": p, "blob_sha": b} for (p, b) in sorted(sidecars)]
    for d in docs:
        _write(reader / "docs" / d["path"], doc_bytes[d["path"]])
    for (p, b), data in sidecars.items():
        _write(reader / "sidecars" / b / p, data)
    _write(
        reader / "index.json",
        _json_bytes(
            {
                "project": slug,
                "ref_sha": ref_sha,
                "since": since,
                "until": until,
                "docs": docs,
                "sidecars": sidecar_index,
            }
        ),
    )
    _write(reader / "runs.jsonl", b"".join(_json_bytes(r) for r in runs_sorted))
    _write(reader / "git_log.jsonl", b"".join(_json_bytes(r) for r in git_log))
    if titles is not None:
        _write(reader / "backlog_titles.txt", "".join(t + "\n" for t in titles).encode("utf-8"))

    packet_sha = _packet_hash(reader)
    exclusions.sort(key=lambda e: (e["path"], e["reason"]))
    manifest = {
        "packet_sha256": packet_sha,
        "project": slug,
        "ref": ref,
        "ref_sha": ref_sha,
        "since": since,
        "until": until,
        "reachable_only": reachable_only,
        "backlog": "provided" if titles is not None else "not_provided",
        "exclusions": exclusions,
        "short_needles_skipped": sorted(short_skipped, key=lambda d: d["source"]),
        "sidecar_unresolved": sorted(sidecar_unresolved, key=lambda d: (d["run"], d["path"])),
    }
    _write(out_p / "manifest.json", _json_bytes(manifest))
    logger.info("built bearings packet %s (%d docs, %d runs)", packet_sha[:12], len(docs), len(runs_sorted))
    return {
        "ok": True,
        "packet_sha256": packet_sha,
        "out": str(out_p),
        "ref_sha": ref_sha,
        "n_docs": len(docs),
        "n_runs": len(runs_sorted),
        "n_git_commits": len(git_log),
        "n_sidecars": len(sidecar_index),
        "n_excluded": len(exclusions),
    }
