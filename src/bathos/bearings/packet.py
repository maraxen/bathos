"""Deterministic blind packet for a bearings reader (spec 261104, Design item 4; slice 2 plan C2).

The packet is built from the git object store at one resolved commit, never from the working
tree, so a dirty tree cannot change its hash. Everything under ``reader/`` is what the blind
reader may see; ``manifest.json`` (exclusions, hash) sits beside it and is never shown.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import tomllib
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

# Owner decision 261108 (spec T6, AC-4b): about 30% under the largest inline size that worked.
DEFAULT_BUDGET_BYTES = 600_000

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


_LINE_MARKER_RE = re.compile(r"^[ \t]*(?:[>#][ \t]*)+", re.MULTILINE)
# An absolute path token in a run's command or tags, not part of a URL or a relative path.
# Covers `-I/x` (a one-letter flag glued to its path) and `A=/a:/b`. The `(?!/)` keeps a URL's
# `//` out, and the path after it is then preceded by a word character. A path containing a
# space is only scrubbed up to the space.
_ABS_PATH_RE = re.compile(r"(?:(?<=\s-[A-Za-z])|(?<![\w./~]))/(?!/)[^\s'\",;:\]\[]+")


def _norm(text: str) -> str:
    """Collapse whitespace after dropping line-leading quote and comment markers.

    A purpose quoted in a wrapped markdown blockquote or in TOML ``#`` comments has a marker
    inside it at each line break, which would otherwise defeat the verbatim match.
    """
    return " ".join(_LINE_MARKER_RE.sub("", text).split())


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; a naive value is taken as UTC."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    # Pin the config that changes output, so the user's git settings cannot move the hash.
    pins = ("-c", "log.showSignature=false", "-c", "i18n.logOutputEncoding=UTF-8", "-c", "core.quotepath=off")
    return subprocess.run(  # noqa: S603 -- explicit argv, no shell
        ["git", *pins, "-C", str(root), *args], check=check, capture_output=True
    )


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _json_bytes(obj: Any) -> bytes:
    return (canonical_json(obj) + "\n").encode("utf-8")


def _needles(
    root: Path,
    repo: Path,
    ref_sha: str,
    slug: str,
    charter_files: Sequence[Path],
    cat_dir: Path,
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """Return ``([(source, normalised text)], short_needles_skipped)``.

    Sources: the current charter file, every ``--charter-file``, the charter committed at the
    ref, and every ``charter_confirmations`` row, so no older purpose can leak.
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

    at_ref = _git(repo, "cat-file", "blob", f"{ref_sha}:.bth/charter.toml", check=False)
    if at_ref.returncode == 0:
        try:
            doc = tomllib.loads(at_ref.stdout.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            logger.warning("charter at %s is unreadable, not used for blindness: %s", ref_sha[:12], e)
        else:
            raw.append(("ref:mandate", str(doc.get("mandate", ""))))
            ref_purposes = doc.get("purpose", {})
            if isinstance(ref_purposes, dict):
                for pid in sorted(ref_purposes):
                    entry = ref_purposes[pid]
                    if isinstance(entry, dict):
                        raw.append((f"ref:purpose:{pid}", str(entry.get("text", ""))))

    if catalog_readable(cat_dir):
        con = connect_read(cat_dir)
        try:
            rows = con.execute(
                "SELECT mandate_text, purposes_json FROM charter_confirmations WHERE project = ? "
                "ORDER BY ts, eid",
                [slug],
            ).fetchall()
        finally:
            con.close()
        for row in rows:
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
        # A needle whose own line starts with a marker can be quoted inline with the marker kept.
        for form in (n, " ".join(text.split())):
            if form not in seen:
                seen.add(form)
                needles.append((source, form))
    return needles, short


def _quotes(text: str, needles: list[tuple[str, str]]) -> bool:
    hay = _norm(text)
    return any(n in hay for _, n in needles)


def _scrub_abs_paths(text: str) -> str:
    """Reduce absolute paths to their basename.

    The checkout a run or build happened in must not reach the reader or move the hash.
    """
    unschemed = text.replace("file:///", "/")
    return _ABS_PATH_RE.sub(lambda m: "<abs>/" + m.group(0).rstrip("/").rsplit("/", 1)[-1], unschemed)


def _run_quotes(value: str, needles: list[tuple[str, str]]) -> bool:
    """Match a run field raw, without shell escapes, and decoded if it holds JSON (``tags``)."""
    forms = [value, value.replace("\\", "")]
    if value[:1] in ("[", "{"):
        with contextlib.suppress(ValueError):
            forms.append(json.dumps(json.loads(value), ensure_ascii=False))
    return any(_quotes(f, needles) for f in forms)


def _sidecar_candidates(path_str: str) -> list[str]:
    """Repo-relative guesses for a recorded sidecar path, longest first.

    Runs are recorded with whatever absolute path they ran from: a linked worktree
    (``<repo>/.claude/worktrees/<name>/scripts/x.bth.toml``), another checkout, or a cluster
    copy (``/orcd/.../tev_design/scripts/x.bth.toml``). No prefix can be assumed, so try each
    suffix against the commit the run recorded and take the longest that exists.
    """
    parts = [p for p in path_str.split("/") if p not in ("", ".")]
    return ["/".join(parts[i:]) for i in range(len(parts))]


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

    slug = project or _resolve_project_slug(root)
    cat_dir = _resolve_catalog_dir(catalog_dir)
    if charter_file is None:
        charter_files: list[Path] = []
    elif isinstance(charter_file, (str, Path)):
        charter_files = [Path(charter_file)]
    else:
        charter_files = [Path(c) for c in charter_file]
    needles, short_skipped = _needles(root, repo, ref_sha, slug, charter_files, cat_dir)

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
            if rec.get("status") == "running":
                # Its status and outcome will change, which would move the hash of a later build.
                exclusions.append({"path": f"run:{rid}", "reason": "not_terminal"})
                continue
            gh = rec.get("git_hash")
            if reachable_only and not (gh and _reachable(repo, str(gh), ref_sha, reach_cache)):
                exclusions.append({"path": f"run:{rid}", "reason": "unreachable_from_ref"})
                continue
            if any(isinstance(v, str) and _run_quotes(v, needles) for v in rec.values()):
                exclusions.append({"path": f"run:{rid}", "reason": "quotes_charter"})
                continue
            for field in ("command", "tags"):
                if isinstance(rec.get(field), str):
                    rec[field] = _scrub_abs_paths(rec[field])
            runs.append((ts, rec))  # type: ignore[arg-type]
    runs_sorted = [rec for _, rec in sorted(runs, key=lambda t: (t[0], str(t[1]["id"])))]  # type: ignore[index]
    # Many runs share a sidecar and commit, and a miss tries every suffix of the path.
    blob_cache: dict[str, subprocess.CompletedProcess[bytes]] = {}

    def _cat_blob(spec: str) -> subprocess.CompletedProcess[bytes]:
        if spec not in blob_cache:
            blob_cache[spec] = _git(repo, "cat-file", "blob", spec, check=False)
        return blob_cache[spec]

    for rec in runs_sorted:
        recorded = rec.get("sidecar_path")
        if not recorded:
            continue
        # Only a path proven to exist in the repo reaches the reader; never the recorded one.
        rec["sidecar_path"] = None
        found = None
        for rev in (str(rec.get("git_hash") or ""), ref_sha):
            if not rev or found:
                continue
            candidates = _sidecar_candidates(str(recorded))
            for cand in candidates:
                proc = _cat_blob(f"{rev}:{cand}")
                if proc.returncode != 0:
                    continue
                # A bare filename could be an unrelated file, so it must match the recorded hash.
                if (
                    "/" not in cand
                    and len(candidates) > 1
                    and hashlib.sha256(proc.stdout).hexdigest() != rec.get("sidecar_sha256")
                ):
                    continue
                found = (rev, cand, proc)
                break
        if not found:
            sidecar_unresolved.append({"run": str(rec["id"]), "path": str(recorded)})
            continue
        rev, rel, proc = found
        rec["sidecar_path"] = rel
        blob = _git(repo, "rev-parse", f"{rev}:{rel}").stdout.decode().strip()
        if (rel, blob) in sidecars or any(s["path"] == f"sidecar:{rel}@{blob}" for s in exclusions):
            continue
        try:
            text = proc.stdout.decode("utf-8")
        except UnicodeDecodeError:
            exclusions.append({"path": f"sidecar:{rel}@{blob}", "reason": "not_text"})
            continue
        if _quotes(text, needles):
            exclusions.append({"path": f"sidecar:{rel}@{blob}", "reason": "quotes_charter"})
        else:
            sidecars[(rel, blob)] = proc.stdout

    # ── Git subjects in the window ──────────────────────────────────────────────
    git_log: list[dict[str, str]] = []
    log_out = _git(repo, "log", ref_sha, "--format=%H%x00%cI%x00%s").stdout.decode("utf-8", errors="replace")
    parsed_log: list[tuple[datetime, dict[str, str]]] = []
    # split("\n"), not splitlines(): a subject may hold \x0b, \x85 or \u2028.
    for line in log_out.split("\n"):
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


def _reader_bytes(packet_dir: Path) -> int:
    """The size the reader sees: the sum of the files under ``reader/``."""
    return sum(p.stat().st_size for p in (packet_dir / "reader").rglob("*") if p.is_file())


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line]


def build_packet_within_budget(
    project_root: Path | str,
    *,
    out: Path | str,
    since: str,
    until: str,
    max_bytes: int | None = DEFAULT_BUDGET_BYTES,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build the packet, shortening the window from the old end until it fits ``max_bytes`` (spec AC-4b).

    A window that already fits gives the same packet as ``build_packet``. Otherwise ``since`` moves to the earliest run
    or commit time whose packet fits, which is well defined because size never grows as ``since`` moves later. Every
    run and commit dropped that way is listed in the manifest by id with reason ``budget``. Content that does not shrink
    with the window (docs are not windowed) can make a fit impossible: that refuses and writes nothing.
    ``max_bytes`` of ``None`` or ``0`` switches the budget off. ``kwargs`` go to ``build_packet``.
    """
    out_p = Path(out)
    if out_p.exists() and (not out_p.is_dir() or any(out_p.iterdir())):
        raise BearingsPacketInvalidError(f"--out must be empty or absent: {out_p}")
    budget = max_bytes if max_bytes and max_bytes > 0 else None
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        full_dir = work / "full"
        full_res = build_packet(project_root, out=full_dir, since=since, until=until, **kwargs)
        full_bytes = _reader_bytes(full_dir)
        full_runs = {str(r["id"]) for r in _jsonl(full_dir / "reader" / "runs.jsonl")}
        full_commits = {str(c["sha"]) for c in _jsonl(full_dir / "reader" / "git_log.jsonl")}
        chosen_dir, chosen_res, eff_since = full_dir, full_res, since
        if budget is not None and full_bytes > budget:
            stamps = sorted(
                {str(r["timestamp"]) for r in _jsonl(full_dir / "reader" / "runs.jsonl")}
                | {str(c["committed_at"]) for c in _jsonl(full_dir / "reader" / "git_log.jsonl")},
                key=parse_ts,
            )
            best: tuple[str, dict[str, Any], Path] | None = None
            lo, hi = 0, len(stamps)  # the smallest index that fits; size never grows as since moves later
            probes = 0
            while lo < hi:
                mid = (lo + hi) // 2
                probe_dir = work / f"probe{probes}"
                probes += 1
                res = build_packet(project_root, out=probe_dir, since=stamps[mid], until=until, **kwargs)
                if _reader_bytes(probe_dir) <= budget:
                    best, hi = (stamps[mid], res, probe_dir), mid
                else:
                    lo = mid + 1
            if best is None:
                raise BearingsPacketInvalidError(
                    f"Packet exceeds the {budget} byte budget even over the shortest window; content that does not "
                    "shrink with the window (docs) is too large"
                )
            eff_since, chosen_res, chosen_dir = best
        kept_runs = {str(r["id"]) for r in _jsonl(chosen_dir / "reader" / "runs.jsonl")}
        kept_commits = {str(c["sha"]) for c in _jsonl(chosen_dir / "reader" / "git_log.jsonl")}
        out_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(chosen_dir, out_p, dirs_exist_ok=True)
        dropped_runs, dropped_commits = sorted(full_runs - kept_runs), sorted(full_commits - kept_commits)
        manifest_path = out_p / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["budget"] = {
            "max_bytes": budget,
            "bytes": _reader_bytes(out_p),
            "full_window_bytes": full_bytes,
            "requested_since": since,
            "effective_since": eff_since,
            "window_shortened": eff_since != since,
            "dropped": {"reason": "budget", "runs": dropped_runs, "commits": dropped_commits},
        }
        _write(manifest_path, _json_bytes(manifest))
    return {
        **chosen_res,
        "out": str(out_p),
        "bytes": manifest["budget"]["bytes"],
        "window_shortened": manifest["budget"]["window_shortened"],
        "effective_since": eff_since,
        "n_dropped_runs": len(dropped_runs),
        "n_dropped_commits": len(dropped_commits),
    }
