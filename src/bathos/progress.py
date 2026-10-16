"""Advisory per-run progress file (`$BTH_PROGRESS_PATH`, issue #105).

`bth run` exports `BTH_PROGRESS_PATH`, a per-run path in the temp directory. A script MAY
overwrite that file atomically with a small JSON object::

    {"done": 3, "total": 10, "unit": "frames", "updated_at": "2026-10-16T12:00:00+00:00"}

The file is advisory only:

- It is never read as a result and never part of the run hash, run metadata or any event payload.
- It is removed together with the temp results file when the run ends.
- A missing, unreadable, partial or malformed file means "unknown", never an error.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

PROGRESS_SUFFIX = ".bth-progress.json"


def progress_path_for(run_id: str, temp_dir: Path | None = None) -> Path:
    """Return the progress-file path for `run_id` under `temp_dir` (default: system temp)."""
    base = temp_dir if temp_dir is not None else Path(tempfile.gettempdir())
    return base / f"{run_id}{PROGRESS_SUFFIX}"


def _as_int_or_none(value: object) -> int | None:
    # bool is a subclass of int; a boolean is not a progress count.
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def read_progress(path: Path) -> dict | None:
    """Read a progress file, or return None when it is absent or not usable.

    Returns a dict with keys `done` (int), `total` (int or None), `unit` (str or None) and
    `updated_at` (str or None). Returns None for a missing, unreadable, non-object or
    malformed file, or when `done` is not an int. Never raises.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    done = _as_int_or_none(data.get("done"))
    if done is None:
        return None
    unit = data.get("unit")
    updated_at = data.get("updated_at")
    return {
        "done": done,
        "total": _as_int_or_none(data.get("total")),
        "unit": unit if isinstance(unit, str) else None,
        "updated_at": updated_at if isinstance(updated_at, str) else None,
    }
