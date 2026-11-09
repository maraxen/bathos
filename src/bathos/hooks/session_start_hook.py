"""SessionStart fast-path hook for Claude Code.

Stdlib-only entry point (<15ms cold start) reading pre-rendered
`<main_root>/.bth/cache/session_start.json` and emitting:
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}

Spec: 261106 §1.2 I4, §1.4.C.6, §4.5, AC-P2-07, AC-P2-15.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _find_cache_file(start_dir: Path) -> Path | None:
    env_root = os.environ.get("BTH_WORKSPACE_ROOT")
    if env_root:
        p = Path(env_root) / ".bth" / "cache" / "session_start.json"
        if p.is_file():
            return p

    curr = start_dir.resolve()
    for d in [curr, *curr.parents]:
        cand = d / ".bth" / "cache" / "session_start.json"
        if cand.is_file():
            return cand
        git_file = d / ".git"
        if git_file.is_file():
            try:
                line = git_file.read_text(encoding="utf-8").strip()
                if line.startswith("gitdir:"):
                    gitdir = Path(line.split(":", 1)[1].strip())
                    if not gitdir.is_absolute():
                        gitdir = (d / gitdir).resolve()
                    commondir_file = gitdir / "commondir"
                    if commondir_file.is_file():
                        commondir = Path(commondir_file.read_text(encoding="utf-8").strip())
                        if not commondir.is_absolute():
                            commondir = (gitdir / commondir).resolve()
                        cand = commondir.parent / ".bth" / "cache" / "session_start.json"
                        if cand.is_file():
                            return cand
            except Exception:
                pass
    return None


def main() -> None:
    try:
        payload = {}
        if not sys.stdin.isatty():
            try:
                raw = sys.stdin.read()
                if raw.strip():
                    payload = json.loads(raw)
            except Exception:
                pass

        cwd_str = payload.get("cwd") if isinstance(payload, dict) else None
        start_dir = Path(cwd_str).resolve() if cwd_str else Path.cwd()

        cache_path = _find_cache_file(start_dir)
        if cache_path is None or not cache_path.is_file():
            sys.exit(0)

        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if "hookSpecificOutput" in data:
                sys.stdout.write(json.dumps(data) + "\n")
            elif "additionalContext" in data:
                sys.stdout.write(
                    json.dumps({
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": data["additionalContext"],
                        }
                    })
                    + "\n"
                )
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"bth-session-start-hook: {exc}\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
