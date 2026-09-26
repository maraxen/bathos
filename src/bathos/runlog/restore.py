"""`bth log restore` (AC-14 log half, AC-27).

Copies back into the project log every mirror line the project log lacks,
except one whose `main_root` names another currently *live* root sharing the
same `project_id` (D7) -- so a genuine fork never receives the other copy's
own local events, while a moved-or-deleted project's history still comes
back at its new path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .project_id import live_roots_with_id
from .writer import mirror_dir_for


@dataclass(frozen=True)
class RestoreReport:
    restored: int
    already_present: int
    skipped_other_live_root: int
    segments_written: list[Path] = field(default_factory=list)


def _collect_eids(log_dir: Path) -> set[str]:
    eids: set[str] = set()
    if not log_dir.exists():
        return eids
    for segment_file in log_dir.glob("*.jsonl"):
        for raw_line in segment_file.read_text(errors="replace").splitlines():
            if not raw_line.strip():
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            eid = obj.get("eid")
            if eid:
                eids.add(eid)
    return eids


def restore_from_mirror(
    main_root: Path,
    *,
    project_id: str | None = None,
    slug: str | None = None,
) -> RestoreReport:
    """Restore `main_root/.bth/log/` from its mirror.

    Exactly one of `project_id`/`slug` should be given, matching how the
    project's events were mirrored (D7): an id-carrying project mirrors under
    `~/.bth/log-mirror/<project_id>/`; an unaffiliated one under
    `~/.bth/log-mirror/_null/<slug or _unaffiliated>/`, which has no
    "other live root" concept (no fork problem without a shared id), so the
    live-root exclusion below is skipped for it.
    """
    main_root = main_root.resolve()
    project_log_dir = main_root / ".bth" / "log"
    project_log_dir.mkdir(parents=True, exist_ok=True)

    existing_eids = _collect_eids(project_log_dir)
    live_others = live_roots_with_id(project_id, exclude=main_root) if project_id else set()

    mirror_dir = mirror_dir_for(project_id, slug)
    if not mirror_dir.exists():
        return RestoreReport(restored=0, already_present=0, skipped_other_live_root=0)

    restored = 0
    already = 0
    skipped_other = 0
    written_segments: list[Path] = []

    for segment_file in sorted(mirror_dir.glob("*.jsonl")):
        recovered_lines: list[str] = []
        for raw_line in segment_file.read_text(errors="replace").splitlines():
            if not raw_line.strip():
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            eid = obj.get("eid")
            if eid in existing_eids:
                already += 1
                continue
            line_main_root = obj.get("main_root")
            if project_id and line_main_root:
                try:
                    if Path(line_main_root).resolve() in live_others:
                        skipped_other += 1
                        continue
                except OSError:
                    pass
            recovered_lines.append(raw_line)
            if eid:
                existing_eids.add(eid)
            restored += 1

        if recovered_lines:
            dest = project_log_dir / f"restore-{segment_file.stem}.jsonl"
            with open(dest, "a") as f:
                for line in recovered_lines:
                    f.write(line + "\n")
            written_segments.append(dest)

    return RestoreReport(
        restored=restored,
        already_present=already,
        skipped_other_live_root=skipped_other,
        segments_written=written_segments,
    )
