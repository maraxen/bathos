"""A fake ``myxcel cold ...`` runner that simulates a remote filesystem in a temp directory.

It honours the section 6.2 contracts of the output-tiering spec (261023): paths are relative to the profile's
``cold_root``; ``put`` creates parents and never overwrites; ``commit`` is ``ln && rm`` and answers ``exists``
when the final is taken; ``lock`` is an atomic ``mkdir`` of ``<dir>/.bth-lock`` holding a holder record;
``verify-file`` hashes the file it finds (``verified`` / ``failed``, and ``unverifiable`` when it cannot be read);
``get`` copies one file into a local directory; ``ls`` lists one directory (regular files, plus the lock directory
as ``{"name": ".bth-lock", "lock": true, "holder"}``; ``missing`` for an absent directory); ``rm-partial`` removes
one regular file whose last segment is ``*.partial-<8 hex>`` and refuses everything else (a directory is an
``error``, an absent file is ``absent``). No real myxcel or ssh is ever run.

:meth:`FakeMyxcel.without_ls_rm_partial` simulates an older myxcel that has the base primitives but neither ``ls``
nor ``rm-partial``: their ``--help`` probe exits 2, and any real call of them is recorded in ``unsupported_calls``
(a test asserts it stays empty). :attr:`FakeMyxcel.rm_log` records, for every ``rm-partial`` call, the path and
whether the lock directory of that directory existed at that moment.

Crash windows are tested by killing the "process" at a chosen step: :meth:`FakeMyxcel.kill_after` makes the call
that completes the Nth matching command raise :class:`KillSwitch` (a ``BaseException``, so no bathos handler
swallows it) and puts the fake in the ``dead`` state, where every further call from the killed process raises too
(a killed process cannot release its lock). :meth:`revive` is the next process. The fake has NO operation that
deletes a final file: the only removals are the partial a ``commit`` links away, a ``rm-partial`` of a
``*.partial-<8 hex>`` file, and an ``unlock`` of the lock directory, which is what the contract allows.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

from bathos.output_tiers.myxcel import RunResult

HELP = "Usage: myxcel cold put PROFILE LOCAL REMOTE [--json]\n  --json  machine output\n"
LOCK = ".bth-lock"
_PARTIAL = re.compile(r".+\.partial-[0-9a-f]{8}")


class KillSwitch(BaseException):
    """The simulated process was killed."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeMyxcel:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.calls: list[tuple[str, list[str]]] = []
        self.dead = False
        self._kill: tuple[str, int] | None = None
        self._seen: dict[str, int] = {}
        self._hooks: dict[str, Callable[[list[str], Path], None]] = {}
        self._after_hooks: dict[str, Callable[[list[str], Path], None]] = {}
        self._mu = threading.RLock()
        self._fail: dict[str, str] = {}
        self.has_ls = True
        self.has_rm_partial = True
        self.unsupported_calls: list[list[str]] = []
        self.rm_log: list[tuple[str, bool]] = []  # (remote path, lock held at that moment)
        self.holder = {
            "host": "fake-host",
            "pid": 4242,
            "attempt": "deadbeef",
            "started": "2026-01-01T00:00:00Z",
        }

    # --- remote filesystem helpers (tests) ---
    def remote(self, profile: str, rel: str = "") -> Path:
        return self.root / profile / rel

    def snapshot(self, profile: str) -> dict[str, str]:
        """``{relpath: sha256}`` of every regular file under the profile, lock records excluded."""
        base = self.root / profile
        if not base.exists():
            return {}
        return {
            str(p.relative_to(base)): _sha(p)
            for p in sorted(base.rglob("*"))
            if p.is_file() and LOCK not in p.parts
        }

    def finals(self, profile: str) -> dict[str, str]:
        return {k: v for k, v in self.snapshot(profile).items() if ".partial-" not in k}

    def calls_named(self, sub: str) -> list[list[str]]:
        return [a for s, a in self.calls if s == sub]

    def mutating_calls(self) -> list[str]:
        return [s for s, _ in self.calls if s in {"put", "commit", "lock", "unlock", "rm-partial"}]

    def without_ls_rm_partial(self) -> None:
        """Become an older myxcel: the base primitives only."""
        self.has_ls = False
        self.has_rm_partial = False

    def plant(self, profile: str, rel: str, data: bytes = b"stale") -> None:
        """Put a file on the remote as if an earlier attempt (or anything else) had left it."""
        target = self.root / profile / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def hold_lock(self, profile: str, dest: str, holder: dict | None = None) -> None:
        """Plant a lock as if another (live or dead) writer took it."""
        d = self.root / profile / dest / LOCK
        d.mkdir(parents=True)
        (d / "holder.json").write_text(json.dumps(holder or self.holder))

    def lock_exists(self, profile: str, dest: str) -> bool:
        return (self.root / profile / dest / LOCK).is_dir()

    # --- failure injection ---
    def kill_after(self, sub: str, nth: int = 1) -> None:
        """The process dies right after its ``nth`` successful ``sub`` call has taken effect."""
        self._kill = (sub, nth)
        self._seen = {}

    def revive(self) -> None:
        self.dead = False
        self._kill = None

    def fail(self, sub: str, message: str = "boom", *, status: str = "error") -> None:
        """Every call of ``sub`` answers ``status`` (exit 1) without touching the remote."""
        self._fail[sub] = f"{status}|{message}"

    def before(self, sub: str, fn: Callable[[list[str], Path], None]) -> None:
        self._hooks[sub] = fn

    def after(self, sub: str, fn: Callable[[list[str], Path], None]) -> None:
        self._after_hooks[sub] = fn

    # --- the runner ---
    def __call__(self, argv: list[str], _timeout: float) -> RunResult:
        if self.dead:
            raise KillSwitch("process is dead")
        if argv[:4] == ["myxcel", "cold", "put", "--help"]:
            return RunResult(0, HELP, "")
        if (
            argv[:2] == ["myxcel", "cold"]
            and argv[3:] == ["--help"]
            and argv[2] in {"ls", "rm-partial"}
        ):
            has = self.has_ls if argv[2] == "ls" else self.has_rm_partial
            if has:
                return RunResult(0, HELP, "")
            return RunResult(2, "", f"Unused Tokens: ['{argv[2]}']")
        assert argv[:2] == ["myxcel", "cold"] and argv[-1] == "--json", argv
        sub, profile, args = argv[2], argv[3], argv[4:-1]
        if (sub == "ls" and not self.has_ls) or (sub == "rm-partial" and not self.has_rm_partial):
            with self._mu:
                self.unsupported_calls.append(argv)
            return RunResult(2, "", f"Unused Tokens: ['{sub}']")
        base = self.root / profile
        with self._mu:
            self.calls.append((sub, [profile, *args]))
        hook = self._hooks.get(sub)
        if hook is not None:
            hook(args, base)  # may block (a concurrent writer) or raise
        with self._mu:
            if sub in self._fail:
                status, message = self._fail[sub].split("|", 1)
                code, payload = 1, {"status": status, "message": message}
            else:
                code, payload = getattr(self, f"_do_{sub.replace('-', '_')}")(base, args)
        after = self._after_hooks.get(sub)
        if after is not None:
            after(args, base)
        if self._kill and self._kill[0] == sub and code == 0:
            self._seen[sub] = self._seen.get(sub, 0) + 1
            if self._seen[sub] == self._kill[1]:
                self.dead = True
                raise KillSwitch(f"killed after {sub} #{self._seen[sub]}")
        return RunResult(code, json.dumps(payload), "")

    # --- commands (section 6.2) ---
    def _do_put(self, base: Path, args: list[str]):
        local, rel = Path(args[0]), args[1]
        target = base / rel
        if target.exists():
            return 1, {"status": "error", "message": f"{rel} exists"}
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, target)
        return 0, {
            "status": "ok",
            "remote_path": rel,
            "bytes": target.stat().st_size,
            "message": "",
        }

    def _do_commit(self, base: Path, args: list[str]):
        part, final = base / args[0], base / args[1]
        if not part.exists():
            return 1, {"status": "error", "message": f"no such partial {args[0]}"}
        try:
            os.link(part, final)  # ln without -f: fails if final exists
        except FileExistsError:
            return 1, {"status": "exists"}
        part.unlink()  # the rm half of `ln && rm`: a partial, never a final
        return 0, {"status": "ok"}

    def _do_verify_file(self, base: Path, args: list[str]):
        rel, sha, size = args[0], args[1], int(args[2])
        p = base / rel
        if not p.is_file():
            return 1, {"status": "unverifiable", "message": f"cannot read {rel}"}
        got, got_size = _sha(p), p.stat().st_size
        status = "verified" if (got == sha and got_size == size) else "failed"
        return (0 if status == "verified" else 1), {
            "status": status,
            "sha256_observed": got,
            "size_observed": got_size,
            "method": "login_ssh",
            "message": "",
        }

    def _do_get(self, base: Path, args: list[str]):
        rel, local_dir = args[0], Path(args[1])
        p = base / rel
        if not p.is_file():
            return 1, {"status": "error", "message": f"no such file {rel}"}
        if not local_dir.is_dir():
            # real myxcel (cold.py cold_get) never creates local_dir
            return 1, {
                "status": "error",
                "local_path": None,
                "bytes": None,
                "message": f"local_dir does not exist or is not a directory: {local_dir}",
            }
        out = local_dir / p.name
        if out.exists():
            return 1, {"status": "error", "message": "local target exists"}
        shutil.copyfile(p, out)
        return 0, {"status": "ok", "local_path": str(out), "bytes": out.stat().st_size}

    def _do_lock(self, base: Path, args: list[str]):
        d = base / args[0] / LOCK
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            d.mkdir()  # atomic
        except FileExistsError:
            try:
                holder = json.loads((d / "holder.json").read_text())
            except (OSError, ValueError):
                holder = None
            return 1, {"status": "held", "holder": holder}
        (d / "holder.json").write_text(json.dumps(self.holder))
        return 0, {"status": "acquired", "holder": self.holder}

    def _do_unlock(self, base: Path, args: list[str]):
        d = base / args[0] / LOCK
        if not d.is_dir():
            # real myxcel (cold.py cold_unlock): ssh exits 2 "lock does not exist" -> status error
            return 1, {
                "status": "error",
                "holder": None,
                "message": "lock does not exist",
            }
        for child in d.iterdir():
            child.unlink()
        d.rmdir()
        return 0, {"status": "released", "holder": None, "message": ""}

    def _do_ls(self, base: Path, args: list[str]):
        d = base / args[0].rstrip("/")
        if not d.is_dir():
            return 1, {
                "status": "missing",
                "entries": [],
                "message": f"no such directory {args[0]}",
            }
        entries: list[dict] = []
        for child in sorted(d.iterdir()):
            if child.name == LOCK and child.is_dir() and not child.is_symlink():
                try:
                    holder = json.loads((child / "holder.json").read_text())
                except (OSError, ValueError):
                    holder = None
                entries.append({"name": LOCK, "lock": True, "holder": holder})
            elif child.is_file() and not child.is_symlink():
                st = child.stat()
                entries.append({"name": child.name, "size_bytes": st.st_size, "mtime": st.st_mtime})
            # other directories are omitted, as in real myxcel
        return 0, {"status": "ok", "entries": entries, "message": ""}

    def _do_rm_partial(self, base: Path, args: list[str]):
        rel = args[0]
        last = rel.rsplit("/", 1)[-1]
        target = base / rel
        self.rm_log.append((rel, (target.parent / LOCK).is_dir()))
        if not _PARTIAL.fullmatch(last):
            return 1, {"status": "refused", "message": f"not a partial upload name: {last!r}"}
        if not target.parent.is_dir() or not (target.exists() or target.is_symlink()):
            return 1, {"status": "absent", "message": f"no such partial: {rel}"}
        if target.is_symlink() or target.is_dir() or not target.is_file():
            return 1, {"status": "error", "message": "target is not a regular file, not removed"}
        target.unlink()
        return 0, {"status": "removed", "message": ""}
