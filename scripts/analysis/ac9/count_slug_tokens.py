"""Count whole-token uses of draft charter slugs in a project's primary records (spec 261104 T7, review 261110).

Question: if the packet excluded every record naming a charter slug, how much of the project's ordinary record would
it drop? Slugs were coined from the project's own vocabulary, so a hit in a git subject, a primary doc or a sidecar
path may be ordinary evidence, not a leak. Read-only over git; writes nothing but its JSON report.

Usage::

    python scripts/analysis/ac9/count_slug_tokens.py --repo ~/projects/tev_design \
        --charter scripts/analysis/ac9/tev_design_charter.DRAFT.toml \
        --since 2026-10-12 --until 2026-11-09 --out outputs/ac9/slug_token_counts.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import tomllib
from pathlib import Path

logger = logging.getLogger("count_slug_tokens")

PRIMARY_DOC_DIRS = (".praxia/docs/decisions/", ".praxia/docs/specs/")


def token_re(slug: str) -> re.Pattern[str]:
    """The packet builder's own matcher (``bathos.bearings.purpose_label.id_token_pattern``)."""
    from bathos.bearings.purpose_label import id_token_pattern

    return id_token_pattern(slug)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    ).stdout.decode("utf-8", errors="replace")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--charter", type=Path, required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    slugs = sorted(tomllib.loads(a.charter.read_text())["purpose"]) + ["off-charter"]
    pats = {s: token_re(s) for s in slugs}

    subjects = [
        s
        for s in git(
            a.repo, "log", a.ref, f"--since={a.since}", f"--until={a.until}", "--format=%s"
        ).split("\n")
        if s
    ]
    files = [f for f in git(a.repo, "ls-tree", "-r", "--name-only", a.ref).split("\n") if f]
    docs = [f for f in files if f.startswith(PRIMARY_DOC_DIRS)]
    sidecar_paths = [f for f in files if f.endswith(".bth.toml")]
    doc_text = {d: git(a.repo, "show", f"{a.ref}:{d}") for d in docs}

    report: dict[str, object] = {
        "repo": str(a.repo),
        "ref": git(a.repo, "rev-parse", a.ref).strip(),
        "window": [a.since, a.until],
        "n_subjects": len(subjects),
        "n_primary_docs": len(docs),
        "n_sidecar_paths": len(sidecar_paths),
        "per_slug": {},
    }
    any_subj, any_doc, any_path = set(), set(), set()
    for s, p in pats.items():
        hs = [i for i, t in enumerate(subjects) if p.search(t)]
        hd = [d for d, t in doc_text.items() if p.search(t)]
        hp = [f for f in sidecar_paths if p.search(f)]
        any_subj.update(hs)
        any_doc.update(hd)
        any_path.update(hp)
        report["per_slug"][s] = {  # type: ignore[index]
            "subjects": len(hs),
            "docs": len(hd),
            "sidecar_paths": len(hp),
            "example_subjects": [subjects[i] for i in hs[:3]],
            "example_paths": hp[:3],
        }
    report["subjects_with_any_slug"] = len(any_subj)
    report["docs_with_any_slug"] = len(any_doc)
    report["sidecar_paths_with_any_slug"] = len(any_path)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2) + "\n")
    logger.info(
        "subjects %d/%d, docs %d/%d, sidecar paths %d/%d name a slug",
        len(any_subj),
        len(subjects),
        len(any_doc),
        len(docs),
        len(any_path),
        len(sidecar_paths),
    )


if __name__ == "__main__":
    main()
