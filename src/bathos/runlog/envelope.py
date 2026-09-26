"""The line envelope (spec "Line envelope") and UUIDv7 `eid` generation.

`eid` is the event identity: every event is ingested at most once per `eid`,
whichever copy it arrives from. Python's stdlib `uuid` module has no `uuid7()`
on the 3.13 interpreter this project targets (added in 3.14) and no UUIDv7
library is a project dependency, so RFC 9562 §5.7's layout is implemented
directly here: a 48-bit big-endian Unix-epoch-millisecond timestamp gives
`eid`s a total, (near-)monotonic order without a database round trip, which is
exactly the tie-break the fold rules rely on (UUIDv7 makes ordering total and
deterministic for a fixed event set).
"""

from __future__ import annotations

import dataclasses
import os
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def uuid7() -> uuid.UUID:
    """Generate a UUIDv7 (RFC 9562 <A6BA7C_60> layout): 48-bit ms timestamp,
    version nibble, 12+62 random bits, packed as a standard 128-bit UUID."""
    unix_ts_ms = time.time_ns() // 1_000_000
    rand = os.urandom(10)
    b = bytearray(16)
    b[0:6] = unix_ts_ms.to_bytes(6, "big")
    b[6] = 0x70 | (rand[0] & 0x0F)  # version 0111, top 4 bits of rand_a
    b[7] = rand[1]
    b[8] = 0x80 | (rand[2] & 0x3F)  # variant 10, top 6 bits of rand_b
    b[9:16] = rand[3:10]
    return uuid.UUID(bytes=bytes(b))


def build_envelope(
    *,
    kind: str,
    entity: list[str],
    data: dict[str, Any],
    main_root: Path,
    worktree_root: Path,
    project: str | None,
    project_id: str | None,
    writer: str,
    seq: int,
    origin: str = "live",
    ts: str | None = None,
    eid: str | None = None,
) -> dict[str, Any]:
    """Build one line-envelope dict, ready for `json.dumps` (spec "Line envelope").

    `ts`/`eid` are accepted as overrides purely for deterministic tests
    (AC-1's fixed-`ts` fold tests); real callers leave both unset.
    """
    if origin not in ("live", "migration"):
        raise ValueError(f"origin must be 'live' or 'migration', got {origin!r}")
    return {
        "v": SCHEMA_VERSION,
        "eid": eid or str(uuid7()),
        "kind": kind,
        "entity": list(entity),
        "ts": ts or _now_rfc3339(),
        "project": project,
        "project_id": project_id,
        "writer": writer,
        "seq": seq,
        "main_root": str(main_root),
        "worktree_root": str(worktree_root),
        "origin": origin,
        "data": data,
    }


def _now_rfc3339() -> str:
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        + f".{time.time_ns() // 1000 % 1_000_000:06d}Z"
    )


def segment_stem(host: str, pid: int, start_ns: int, *, slurm_suffix: str | None = None) -> str:
    """`<host>.<pid>.<start_ns>` with `.slurm-<job>-<task>-<restart>` appended
    as information only -- uniqueness comes from host/pid/start_ns alone."""
    stem = f"{host}.{pid}.{start_ns}"
    if slurm_suffix:
        stem += f".{slurm_suffix}"
    return stem


def slurm_suffix_from_env() -> str | None:
    job = os.environ.get("SLURM_JOB_ID")
    if not job:
        return None
    task = os.environ.get("SLURM_ARRAY_TASK_ID", "0")
    restart = os.environ.get("SLURM_RESTART_COUNT", "0")
    return f"slurm-{job}-{task}-{restart}"


def capture_git_provenance(
    cwd: Path, run_id: str, declared_paths: list[str] | tuple[str, ...] = ()
) -> dict[str, Any]:
    """D4: provenance for `run.started` comes only from cisternal -- `GitState`
    plus the `PinResult`, computed the same way `runner.py` computes them
    today (`capture_git_state` then `pin_run`). bathos adds no git logic of
    its own; this is a thin assembly of the two existing calls into one dict
    shaped for `run.started.data`.
    """
    from bathos.git import capture_git_state
    from bathos.git_pin import pin_result_as_dict, pin_run

    git_state = capture_git_state(cwd)
    pin = pin_run(
        run_id=run_id,
        git_hash=git_state.hash,
        git_branch=git_state.branch,
        dirty=git_state.dirty,
        cwd=cwd,
        declared_paths=declared_paths,
    )
    return {
        "git": dataclasses.asdict(git_state),
        "pin": pin_result_as_dict(pin),
    }
