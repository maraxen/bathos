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
  that the in-process hook cannot see.

The real home comes from the passwd database, not ``$HOME``, since tests redirect ``$HOME``.
"""

from __future__ import annotations

import os
import pwd
import sys
import traceback
from pathlib import Path

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_BTH = REAL_HOME / ".bth"

# Real ~/.bth paths the run-log work writes. The catalog itself is excluded on purpose: a
# concurrent real `bth run` legitimately writes it, which would make this check flaky.
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


# BTH_* env vars that name a filesystem path (see `bathos_test_home` in conftest.py).
BTH_PATH_ENV_VARS = (
    "BTH_CATALOG_DIR",
    "BTH_WORKSPACE_ROOT",
    "BTH_PROJECT_ROOT",
    "BTH_LOG_DIR",
    "BTH_MCP_TOKEN_PATH",
    "BTH_RESULTS_PATH",
    "BTH_OUTPUT_DIR",
)


class RealHomeWriteError(PermissionError):
    """A test tried to write under the real ``~/.bth``."""


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
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    return home


def _is_real_bth(path: object) -> bool:
    if isinstance(path, int) or path is None:
        return False
    try:
        p = os.path.abspath(os.fsdecode(path))  # type: ignore[arg-type]
    except TypeError:
        return False
    return any(p == r or p.startswith(r + os.sep) for r in _roots)


def _is_write_open(mode: object, flags: object) -> bool:
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _hook(event: str, args: tuple) -> None:
    if event == "open":
        if len(args) >= 3 and _is_write_open(args[1], args[2]) and _is_real_bth(args[0]):
            _refuse(event, args[0])
        return
    idx = _PATH_EVENTS.get(event)
    if idx is None:
        return
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


def install() -> None:
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True


def fingerprint() -> dict[str, tuple]:
    out: dict[str, tuple] = {}
    for rel in FINGERPRINTED:
        p = REAL_BTH / rel
        if p.is_dir():
            out[rel] = tuple(
                sorted(
                    (str(f.relative_to(p)), f.stat().st_size, f.stat().st_mtime_ns)
                    for f in p.rglob("*")
                    if f.is_file()
                )
            )
        elif p.exists():
            st = p.stat()
            out[rel] = (st.st_size, st.st_mtime_ns)
    return out


def diff(before: dict[str, tuple], after: dict[str, tuple]) -> list[str]:
    changed = []
    for rel in sorted(set(before) | set(after)):
        if before.get(rel) != after.get(rel):
            state = (
                "created" if rel not in before else "removed" if rel not in after else "modified"
            )
            changed.append(f"{REAL_BTH / rel}: {state}")
    return changed
