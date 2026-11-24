"""Session-wide tripwire: the test suite must never write into the REAL ``~/.bth``.

`bathos_test_home` redirects HOME per test, but twice (2026-09-25 and 2026-09-26) tests and
test harnesses still wrote into the real ``~/.bth/projects.toml`` and ``~/.bth/log-mirror/``:
once via module-level ``Path.home()`` constants bound at import time (before any fixture runs),
once via a harness driven from a script outside pytest. Migration step 0 walks every
registered root, so that debris would have polluted the real cut-over.

Two independent layers, both installed from ``pytest_configure`` (before test modules are
imported):

* an audit hook (PEP 578) that refuses, in-process, any open-for-write / mkdir / rename /
  remove / rmtree under the real ``~/.bth``. The offending call raises `RealHomeWriteError`
  with a traceback at the leak site, and the violation is recorded so the session fails even
  if some ``except OSError`` swallowed it;
* a fingerprint of the real registry and run-log paths taken at configure time and compared
  at session finish, which also catches writes from subprocesses (``bth`` CLI invocations)
  that the in-process hook cannot see. To avoid false positives when a real ``bth`` process
  or overlapping pytest session runs concurrently, changes to ``log``, ``log-mirror``,
  ``catalog/index.db``, and ``projects.toml`` are verified for test-process ownership
  (session PIDs, spawned subprocess PIDs/env, and session temp roots) before failing.

The real home comes from the passwd database, not ``$HOME``, since tests redirect ``$HOME``.
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import re
import subprocess
import sys
import threading
import tomllib
import traceback
from pathlib import Path
from typing import Any

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_BTH = REAL_HOME / ".bth"

# Real ~/.bth paths the run-log work writes. The legacy catalog (bathos.db) is excluded
# on purpose; `log`, `log-mirror`, `catalog/index.db`, and `projects.toml` are checked
# with test-process ownership attribution so concurrent real `bth` activity does not
# false-positive overlapping pytest sessions (#3323, #2820).
FINGERPRINTED = (
    "projects.toml",
    "log-mirror",
    "log",
    "catalog/cutover.json",
    "catalog/index.db",
    "catalog/bathos.db.frozen",
)

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
# audit event -> indices of the path arguments it mutates
_PATH_EVENTS = {
    "os.mkdir": (0,),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.truncate": (0,),
    "os.rename": (0, 1),
    "os.replace": (0, 1),
    "os.link": (0, 1),
    "os.symlink": (1,),
    "shutil.rmtree": (0,),
    "shutil.move": (0, 1),
    "shutil.copyfile": (1,),
    "shutil.copytree": (1,),
}

violations: list[str] = []
_roots = tuple({str(REAL_BTH), os.path.realpath(REAL_BTH)})
_installed = False
_popen_wrapped = False

_state_lock = threading.Lock()
_session_pids: set[int] = {os.getpid()}
_session_roots: set[str] = set()
_exposed_subprocesses: list[tuple[int, str, str]] = []

_SEGMENT_PID_RE = re.compile(
    r"^.+\.(?P<pid>\d+)\.(?P<start_ns>\d+)(?:\.slurm-[^/]+)?\.jsonl$"
)


# BTH_* env vars that name a filesystem path (see `bathos_test_home` in conftest.py).
BTH_PATH_ENV_VARS = (
    "BTH_CATALOG_DIR",
    "BTH_WORKSPACE_ROOT",
    "BTH_PROJECT_ROOT",
    "BTH_LOG_DIR",
    "BTH_MCP_TOKEN_PATH",
    "BTH_RESULTS_PATH",
    "BTH_OUTPUT_DIR",
    "BTH_LOCK_DIR",
)


class RealHomeWriteError(PermissionError):
    """A test tried to write under the real ``~/.bth``."""


def register_session_pid(pid: int) -> None:
    """Record a process ID owned by this pytest session."""
    if pid > 0:
        with _state_lock:
            _session_pids.add(pid)


def session_pids() -> frozenset[int]:
    """Return all PIDs owned by this pytest session (current process + spawned children)."""
    with _state_lock:
        return frozenset(_session_pids)


def register_session_root(path: Path | str) -> None:
    """Record a temporary directory root belonging to this pytest session."""
    s = os.path.abspath(os.fspath(path))
    real = os.path.realpath(s)
    with _state_lock:
        _session_roots.add(s)
        _session_roots.add(real)


def session_roots() -> frozenset[str]:
    """Return all temporary directory roots belonging to this pytest session."""
    with _state_lock:
        return frozenset(_session_roots)


def _is_under_roots(path: object, roots: frozenset[str] | tuple[str, ...]) -> bool:
    if isinstance(path, int) or path is None:
        return False
    try:
        raw = os.fsdecode(path)  # type: ignore[arg-type]
    except TypeError:
        return False
    if not raw:
        return False
    p = os.path.abspath(raw)
    rp = os.path.realpath(p)
    return any(
        cand == r or cand.startswith(r + os.sep) for r in roots for cand in (p, rp)
    )


def _is_in_session_roots(path: object) -> bool:
    roots = session_roots()
    return bool(roots) and _is_under_roots(path, roots)


def redirect_session_home() -> Path:
    """Point HOME at a throwaway dir for the whole session, before bathos is imported.

    `bathos_test_home` re-points HOME per test, but only once a test runs; anything bathos
    resolves at import or collection time would otherwise see the real home.
    """
    import atexit
    import shutil
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="bathos-test-session-home-"))
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    for var in BTH_PATH_ENV_VARS:
        os.environ.pop(var, None)
    register_session_root(home)
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    return home


def _is_real_bth(path: object) -> bool:
    return _is_under_roots(path, _roots)


def _is_real_home(path: object) -> bool:
    roots = tuple({str(REAL_HOME), os.path.realpath(REAL_HOME)})
    return _is_under_roots(path, roots)


def _is_write_open(mode: object, flags: object) -> bool:
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _maybe_record_test_home_root(path: object) -> None:
    try:
        s = os.fsdecode(path)  # type: ignore[arg-type]
    except TypeError:
        return
    if s.endswith(os.sep + "home"):
        parent = os.path.dirname(s)
        if parent and not _is_real_home(parent):
            register_session_root(parent)


def _hook(event: str, args: tuple) -> None:
    if event == "open":
        if len(args) >= 3 and _is_write_open(args[1], args[2]) and _is_real_bth(args[0]):
            _refuse(event, args[0])
        return
    idx = _PATH_EVENTS.get(event)
    if idx is None:
        return
    if event == "os.mkdir" and args:
        _maybe_record_test_home_root(args[0])
    for i in idx:
        if i < len(args) and _is_real_bth(args[i]):
            _refuse(event, args[i])


def _refuse(event: str, path: object) -> None:
    where = "".join(traceback.format_stack(limit=12)[:-2])
    violations.append(f"{event} {os.fsdecode(path)}\n{where}")  # type: ignore[arg-type]
    raise RealHomeWriteError(
        f"test tried to write the REAL {REAL_BTH} ({event} {os.fsdecode(path)}); "  # type: ignore[arg-type]
        "resolve HOME-derived paths per call and run under the bathos_test_home fixture"
    )


def _format_cmd(args: object) -> str:
    if isinstance(args, (list, tuple)):
        return " ".join(str(a) for a in args[:6])
    return str(args)


def _is_git_command(args: object) -> bool:
    if isinstance(args, (list, tuple)) and args:
        exe = os.path.basename(str(args[0]))
        return exe == "git"
    if isinstance(args, str):
        parts = args.strip().split()
        return bool(parts) and os.path.basename(parts[0]) == "git"
    return False


def _detect_env_exposure(env: object) -> str | None:
    if env is None:
        env_map: dict[str, str] = dict(os.environ)
    elif isinstance(env, dict):
        env_map = {str(k): str(v) for k, v in env.items()}
    else:
        return None
    home = env_map.get("HOME")
    if not home:
        return "HOME unset"
    if _is_real_home(home) and not _is_in_session_roots(home):
        return f"HOME={home}"
    for var in BTH_PATH_ENV_VARS:
        val = env_map.get(var)
        if val and _is_real_bth(val):
            return f"{var}={val}"
    return None


def _wrap_popen() -> None:
    global _popen_wrapped
    if _popen_wrapped:
        return
    orig_init = subprocess.Popen.__init__

    def _tracked_popen_init(self: subprocess.Popen, args: Any, *pargs: Any, **kwargs: Any) -> None:
        env = kwargs.get("env")
        if env is None and len(pargs) >= 8:
            env = pargs[7]
        cur_home = os.environ.get("HOME")
        if cur_home and not _is_real_home(cur_home):
            p = Path(cur_home)
            register_session_root(p.parent if p.name == "home" else p)
        orig_init(self, args, *pargs, **kwargs)
        pid = getattr(self, "pid", None)
        if isinstance(pid, int) and pid > 0:
            register_session_pid(pid)
            if not _is_git_command(args):
                exposure = _detect_env_exposure(env)
                if exposure is not None:
                    with _state_lock:
                        _exposed_subprocesses.append((pid, _format_cmd(args), exposure))

    subprocess.Popen.__init__ = _tracked_popen_init  # type: ignore[method-assign]
    _popen_wrapped = True


def exposed_subprocesses() -> list[tuple[int, str, str]]:
    """Return tracked non-git subprocesses whose environment exposed real HOME or ~/.bth."""
    with _state_lock:
        return list(_exposed_subprocesses)


def install() -> None:
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True
    _wrap_popen()


def _segment_pid(rel_or_name: str) -> int | None:
    name = os.path.basename(rel_or_name)
    m = _SEGMENT_PID_RE.match(name)
    if m is None:
        return None
    return int(m.group("pid"))


def _snapshot_projects_toml(path: Path) -> dict[str, Any]:
    st = path.stat()
    roots: list[dict[str, Any]] = []
    projects: list[dict[str, Any]] = []
    parse_error: str | None = None
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        raw_roots = parsed.get("roots", [])
        if isinstance(raw_roots, list):
            roots = [dict(r) for r in raw_roots if isinstance(r, dict)]
        raw_projects = parsed.get("projects", [])
        if isinstance(raw_projects, list):
            projects = [dict(p) for p in raw_projects if isinstance(p, dict)]
    except Exception as exc:
        parse_error = str(exc)
    return {
        "kind": "projects_toml",
        "stat": (st.st_size, st.st_mtime_ns),
        "roots": roots,
        "projects": projects,
        "parse_error": parse_error,
    }


def fingerprint() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for rel in FINGERPRINTED:
        p = REAL_BTH / rel
        try:
            if rel == "projects.toml":
                if p.exists():
                    out[rel] = _snapshot_projects_toml(p)
            elif p.is_dir():
                entries: list[tuple[str, int, int]] = []
                for f in p.rglob("*"):
                    try:
                        if f.is_file():
                            fst = f.stat()
                            entries.append((str(f.relative_to(p)), fst.st_size, fst.st_mtime_ns))
                    except OSError:
                        continue
                out[rel] = tuple(sorted(entries))
            elif p.exists():
                st = p.stat()
                out[rel] = (st.st_size, st.st_mtime_ns)
        except OSError:
            continue
    return out


def _is_log_dir_tuple(val: object) -> bool:
    if not isinstance(val, tuple):
        return False
    return all(isinstance(item, tuple) and len(item) == 3 and isinstance(item[0], str) for item in val)


def _inspect_segment_ownership(
    file_path: Path, rel_file: str, before_size: int
) -> tuple[bool, str | None]:
    """Return (owned_by_this_session, reason) for a segment file under log/ or log-mirror/."""
    pids = session_pids()
    file_pid = _segment_pid(rel_file)
    if file_pid is not None and file_pid in pids:
        return True, f"test PID {file_pid}"

    # Inspect newly appended JSONL lines for session PIDs or session temp roots.
    try:
        with open(file_path, "rb") as fh:
            if before_size > 0:
                fh.seek(before_size)
            chunk = fh.read()
    except OSError:
        chunk = b""

    if chunk:
        for raw_line in chunk.decode("utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                env = json.loads(line)
            except ValueError:
                continue
            if not isinstance(env, dict):
                continue
            writer = env.get("writer")
            if isinstance(writer, str):
                w_pid = _segment_pid(f"{writer}.jsonl")
                if w_pid is not None and w_pid in pids:
                    return True, f"test writer PID {w_pid}"
            for key in ("main_root", "worktree_root"):
                val = env.get(key)
                if isinstance(val, str) and _is_in_session_roots(val):
                    return True, f"test root {val}"

    if file_pid is None:
        return True, "unrecognized non-segment file"
    return False, None


def _diff_log_dir(rel: str, before_val: Any, after_val: Any) -> list[str]:
    if not (_is_log_dir_tuple(before_val or ()) and _is_log_dir_tuple(after_val or ())):
        if before_val == after_val:
            return []
        state = "created" if before_val is None else "removed" if after_val is None else "modified"
        return [f"{REAL_BTH / rel}: {state}"]

    before_map = {name: (size, mtime) for name, size, mtime in (before_val or ())}
    after_map = {name: (size, mtime) for name, size, mtime in (after_val or ())}
    pids = session_pids()
    findings: list[str] = []

    for rel_file in sorted(set(before_map) | set(after_map)):
        b_meta = before_map.get(rel_file)
        a_meta = after_map.get(rel_file)
        if b_meta == a_meta:
            continue
        if a_meta is None:
            file_pid = _segment_pid(rel_file)
            if file_pid is None or file_pid in pids:
                reason = f"test PID {file_pid}" if file_pid is not None else "unrecognized non-segment file"
                findings.append(f"{REAL_BTH / rel / rel_file}: removed ({reason})")
            continue
        state = "created" if b_meta is None else "modified"
        before_size = 0 if b_meta is None else b_meta[0]
        owned, reason = _inspect_segment_ownership(REAL_BTH / rel / rel_file, rel_file, before_size)
        if owned:
            findings.append(f"{REAL_BTH / rel / rel_file}: {state} ({reason})")
    return findings


def _is_projects_snapshot(val: object) -> bool:
    return isinstance(val, dict) and val.get("kind") == "projects_toml"


def _diff_projects_toml(before_val: Any, after_val: Any) -> list[str]:
    if not (
        (before_val is None or _is_projects_snapshot(before_val))
        and (after_val is None or _is_projects_snapshot(after_val))
    ):
        state = "created" if before_val is None else "removed" if after_val is None else "modified"
        return [f"{REAL_BTH / 'projects.toml'}: {state}"]

    state = "created" if before_val is None else "removed" if after_val is None else "modified"
    target = REAL_BTH / "projects.toml"
    exposed = exposed_subprocesses()
    if after_val is None:
        if exposed:
            pid, cmd, why = exposed[-1]
            return [f"{target}: removed (exposed subprocess PID {pid}: {cmd} [{why}])"]
        return []

    before_roots = {
        json.dumps(r, sort_keys=True) for r in (before_val or {}).get("roots", ())
    }
    before_projects = {
        json.dumps(p, sort_keys=True) for p in (before_val or {}).get("projects", ())
    }

    reasons: list[str] = []
    for entry in after_val.get("roots", ()):
        if json.dumps(entry, sort_keys=True) not in before_roots:
            root_path = entry.get("root")
            if isinstance(root_path, str) and _is_in_session_roots(root_path):
                reasons.append(f"test root {root_path}")
    for entry in after_val.get("projects", ()):
        if json.dumps(entry, sort_keys=True) not in before_projects:
            cat_dir = entry.get("catalog_dir")
            if isinstance(cat_dir, str) and _is_in_session_roots(cat_dir):
                reasons.append(f"test catalog_dir {cat_dir}")

    if exposed:
        pid, cmd, why = exposed[-1]
        reasons.append(f"exposed subprocess PID {pid}: {cmd} [{why}]")

    if reasons:
        return [f"{target}: {state} ({'; '.join(reasons)})"]
    return []


def _inspect_index_db_ownership(db_path: Path) -> list[str]:
    reasons: list[str] = []
    pids = session_pids()
    roots = session_roots()
    if db_path.exists():
        import duckdb

        con = None
        try:
            con = duckdb.connect(str(db_path), read_only=True)
            with contextlib.suppress(Exception):
                rows = con.execute(
                    "SELECT DISTINCT writer, main_root, worktree_root FROM events"
                ).fetchall()
                for writer, main_root, worktree_root in rows:
                    if isinstance(writer, str):
                        w_pid = _segment_pid(f"{writer}.jsonl")
                        if w_pid is not None and w_pid in pids:
                            reasons.append(f"test writer PID {w_pid}")
                            break
                    for candidate in (main_root, worktree_root):
                        if isinstance(candidate, str) and _is_under_roots(candidate, roots):
                            reasons.append(f"test event root {candidate}")
                            break
                    if reasons:
                        break
            with contextlib.suppress(Exception):
                wm_rows = con.execute("SELECT DISTINCT path FROM ingest_watermarks").fetchall()
                for (wm_path,) in wm_rows:
                    if isinstance(wm_path, str) and _is_under_roots(wm_path, roots):
                        reasons.append(f"test watermark {wm_path}")
                        break
        except Exception:
            pass
        finally:
            if con is not None:
                with contextlib.suppress(Exception):
                    con.close()

    exposed = exposed_subprocesses()
    if exposed:
        pid, cmd, why = exposed[-1]
        reasons.append(f"exposed subprocess PID {pid}: {cmd} [{why}]")
    return reasons


def _diff_index_db(before_val: Any, after_val: Any) -> list[str]:
    state = "created" if before_val is None else "removed" if after_val is None else "modified"
    target = REAL_BTH / "catalog/index.db"
    if after_val is None:
        exposed = exposed_subprocesses()
        if exposed:
            pid, cmd, why = exposed[-1]
            return [f"{target}: removed (exposed subprocess PID {pid}: {cmd} [{why}])"]
        return []
    reasons = _inspect_index_db_ownership(target)
    if reasons:
        return [f"{target}: {state} ({'; '.join(reasons)})"]
    return []


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for rel in sorted(set(before) | set(after)):
        if before.get(rel) == after.get(rel):
            continue
        if rel in ("log", "log-mirror"):
            changed.extend(_diff_log_dir(rel, before.get(rel), after.get(rel)))
        elif rel == "projects.toml":
            changed.extend(_diff_projects_toml(before.get(rel), after.get(rel)))
        elif rel == "catalog/index.db":
            changed.extend(_diff_index_db(before.get(rel), after.get(rel)))
        else:
            state = (
                "created" if rel not in before else "removed" if rel not in after else "modified"
            )
            changed.append(f"{REAL_BTH / rel}: {state}")
    return changed
