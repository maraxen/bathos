from __future__ import annotations

import importlib.resources
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from bathos.catalog import init_catalog
from bathos.git_pin import MANIFEST_GITIGNORE_LINES
from bathos.runlog.project_id import mint_project_id

SCRIPT_DIRS = [
    "scripts/experiments",
    "scripts/analysis",
    "scripts/validation",
    "scripts/benchmarks",
    "scripts/data",
    "scripts/slurm",
    "scripts/debug",
    "scripts/explore",
    "scripts/scratch",
]

_BTH_TOML_TEMPLATE = """\
[project]
slug = "{slug}"
root = "{root}"
id = "{project_id}"
"""

# scripts/scratch/ is gitignored by convention (its own dir table says "No (gitignored)").
# The manifest lines are debt #1943: `bth run` appends to `.bth/refs/manifest.jsonl` on
# literally every run, and unlike `.bth/refs/authoring.jsonl` (whose git-tracked history IS
# the tamper-evidence mechanism `bathos.authoring.ledger` depends on) nothing ever commits
# those appends -- so leaving the manifest trackable-but-uncommitted means every run after
# the first sees a dirty tree purely from bathos's own bookkeeping. See
# `bathos.git_pin.ensure_manifest_ignored` for the full rationale and the `bth run`-side half
# of this fix (covers projects that never re-run `bth init`).
_GITIGNORE_ENTRIES: tuple[str, ...] = ("scripts/scratch/", *MANIFEST_GITIGNORE_LINES)


@dataclass
class InitReport:
    """What `init_project` actually did to `.bth.toml` on this call.

    Debt #1952: `bth init` on an already-initialized project used to unconditionally
    overwrite `.bth.toml`, silently destroying any hand-added `[remotes.*]`/`[slurm]`
    config. This is the audit trail proving a given call didn't -- every section it left
    alone is named in `preserved`, every section it added is named in `added`, and any
    `--remote`/`--slurm-partition` request it could NOT honor (because the corresponding
    config already existed and touching it would mean overwriting a value) is named in
    `skipped_requests` along with why.
    """

    created: bool = False
    added: list[str] = field(default_factory=list)
    preserved: list[str] = field(default_factory=list)
    skipped_requests: list[str] = field(default_factory=list)


def _load_env_sh_template() -> str:
    pkg = importlib.resources.files("bathos") / "templates" / "_bth_env.sh"
    return pkg.read_text(encoding="utf-8")


def _ensure_gitignore_entries(project_root: Path, entries: tuple[str, ...]) -> list[str]:
    """Append any of `entries` missing from `.gitignore`, preserving everything else.

    Returns the entries that were newly added (empty if all were already present).
    """
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    missing = [e for e in entries if e not in existing]
    if missing:
        with open(gitignore, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            for entry in missing:
                f.write(entry + "\n")
    return missing


def _write_fresh_bth_toml(
    toml_path: Path,
    slug: str,
    project_root: Path,
    remote: str | None,
    slurm_partition: str | None,
) -> InitReport:
    """First-time `.bth.toml` write -- unchanged behavior from before debt #1952."""
    content = _BTH_TOML_TEMPLATE.format(
        slug=slug, root=str(project_root), project_id=mint_project_id()
    )
    added = ["[project]"]
    if remote:
        host, remote_root = remote.split(":", 1)
        content += f'\n[remotes.{host}]\nhost = "{host}"\nremote_root = "{remote_root}"\n'
        added.append(f"[remotes.{host}]")
    if slurm_partition:
        content += f'\n[slurm]\npartition = "{slurm_partition}"\n'
        added.append("[slurm]")
    toml_path.write_text(content)
    return InitReport(created=True, added=added)


def _merge_bth_toml(
    toml_path: Path,
    slug: str,
    project_root: Path,
    remote: str | None,
    slurm_partition: str | None,
) -> InitReport:
    """Re-`init` on an already-initialized project: merge only missing defaults.

    Never rewrites a byte of the existing file -- only ever APPENDS whole new TOML
    tables for sections that are entirely absent. An existing table (even a `[slurm]`
    lacking `partition`) is left alone rather than risk corrupting hand-edited content
    with a text-level key insertion; the request that couldn't be honored is reported
    in `skipped_requests` instead, with the exact edit the user can make by hand.
    """
    report = InitReport(created=False)
    existing_text = toml_path.read_text()
    try:
        existing_data = tomllib.loads(existing_text)
    except tomllib.TOMLDecodeError:
        # Can't safely reason about what's already there -- do nothing rather than guess.
        report.preserved.append(".bth.toml (unparseable; left untouched)")
        return report

    to_append = ""

    project_section = existing_data.get("project")
    if project_section and "slug" in project_section and "root" in project_section:
        report.preserved.append("[project]")
    else:
        to_append += _BTH_TOML_TEMPLATE.format(
            slug=slug, root=str(project_root), project_id=mint_project_id()
        )
        report.added.append("[project]")

    if remote:
        host, remote_root = remote.split(":", 1)
        existing_remotes = existing_data.get("remotes", {})
        if host in existing_remotes:
            report.preserved.append(f"[remotes.{host}]")
            report.skipped_requests.append(
                f"--remote {remote}: [remotes.{host}] already exists in .bth.toml; "
                "edit it by hand to change the remote_root"
            )
        else:
            to_append += f'\n[remotes.{host}]\nhost = "{host}"\nremote_root = "{remote_root}"\n'
            report.added.append(f"[remotes.{host}]")

    if slurm_partition:
        existing_slurm = existing_data.get("slurm", {})
        if "partition" in existing_slurm:
            report.preserved.append("[slurm].partition")
            report.skipped_requests.append(
                f"--slurm-partition {slurm_partition}: [slurm].partition is already "
                f"{existing_slurm['partition']!r}; edit .bth.toml by hand to change it"
            )
        elif existing_slurm:
            report.preserved.append("[slurm]")
            report.skipped_requests.append(
                f"--slurm-partition {slurm_partition}: an existing [slurm] table has no "
                f'`partition` key; add `partition = "{slurm_partition}"` to it by hand '
                "rather than risk corrupting the existing table"
            )
        else:
            to_append += f'\n[slurm]\npartition = "{slurm_partition}"\n'
            report.added.append("[slurm]")

    if to_append:
        new_text = existing_text
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += to_append
        toml_path.write_text(new_text)

    return report


def init_project(
    project_root: Path,
    slug: str,
    catalog_dir: Path,
    remote: str | None = None,
    slurm_partition: str | None = None,
) -> InitReport:
    # Script directories
    for d in SCRIPT_DIRS:
        (project_root / d).mkdir(parents=True, exist_ok=True)

    # .bth.toml -- merge-only on an already-initialized project (debt #1952); the
    # existing config's [remotes.*]/[slurm] must survive a re-`init` untouched.
    toml_path = project_root / ".bth.toml"
    if toml_path.exists():
        report = _merge_bth_toml(toml_path, slug, project_root, remote, slurm_partition)
    else:
        report = _write_fresh_bth_toml(toml_path, slug, project_root, remote, slurm_partition)

    from bathos.cluster_catalog import write_bth_env_sh

    # Read back whatever `remote_root` ended up actually persisted for this host -- NOT
    # necessarily this call's `remote` argument. On a merge that preserved an existing
    # `[remotes.<host>]` (debt #1952), the request's own remote_root was deliberately
    # never written; using it here anyway would make `_bth_env.sh` disagree with the
    # `.bth.toml` that's the source of truth.
    remote_root = None
    if remote:
        host, _requested_root = remote.split(":", 1)
        try:
            persisted = tomllib.loads(toml_path.read_text())
        except tomllib.TOMLDecodeError:
            persisted = {}
        remote_root = persisted.get("remotes", {}).get(host, {}).get("remote_root")

    write_bth_env_sh(
        project_root,
        slug=slug,
        project_root_value=project_root,
        remote_root=remote_root,
    )

    # .gitignore
    _ensure_gitignore_entries(project_root, _GITIGNORE_ENTRIES)

    # D3: .bth/log/ must be gitignored before any run can append to it.
    from bathos.runlog.resolve import ensure_log_ignored

    ensure_log_ignored(project_root)

    # Catalog
    init_catalog(catalog_dir)

    # Register in global project registry
    from bathos.config import register_project

    register_project(slug=slug, catalog_dir=catalog_dir)

    return report


def assign_id_to_existing_project(project_root: Path) -> tuple[str, bool]:
    """`bth init --assign-id`: retrofit a `[project] id` onto an existing
    project's `.bth.toml` without redoing the rest of `init_project` (script
    dirs, .gitignore, catalog, env.sh). Returns `(project_id, minted)`.
    """
    from bathos.runlog.project_id import assign_project_id

    result = assign_project_id(project_root)
    return result.project_id, result.minted
