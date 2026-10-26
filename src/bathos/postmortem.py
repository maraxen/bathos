from __future__ import annotations

import dataclasses
import hashlib
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bathos.git import capture_git_state
from bathos.schema import Run
from bathos.telemetry import event
from bathos.walk import PRUNE_DIRS, iter_project_files


@dataclass
class ValidationError:
    message: str


@dataclass
class ValidationResult:
    ok: bool
    errors: list[ValidationError] = field(default_factory=list)
    #: advisory findings that never flip `ok` (spec 261026 D5): format-2 checks skipped for want of
    #: a catalog, `[fork]`/`[[amendments]]` present without `format = 2`
    warnings: list[str] = field(default_factory=list)


#: banner shown wherever amendments are rendered (spec 261026 D5)
AMENDMENTS_BANNER = (
    "Amendments record later evidence about a verdict. They do not change the run's recorded "
    "outcome; only verdict_override does."
)
FORK_CHOICES = ("stop", "extend", "replicate", "breakdown", "escalate")
AMENDMENT_KINDS = ("distributional_outlier", "reanalysis", "correction")
AMENDMENT_EFFECTS = ("verdict_unchanged", "verdict_contextualized", "verdict_changed")

#: the postmortem file formats `parse_postmortem` accepts; absent `format` means 1
POSTMORTEM_FORMATS = (1, 2)


@dataclass
class Amendment:
    """One `[[amendments]]` entry (format 2, spec 261026 D5): later evidence about a run's
    verdict. Recorded alongside the run; it never alters `runs.outcome`."""

    analysis_run_id: str = ""
    #: the run being amended; "" means this postmortem's own run (required when campaign-scoped)
    amends_run_id: str = ""
    kind: str = ""
    outlier_units: list[str] = field(default_factory=list)
    effect: str = ""
    note: str = ""


@dataclass
class Postmortem:
    # Exactly one of run_id / campaign_id must be set. campaign_id exists because D1 puts
    # the only binding site at conclude, which is campaign-scoped — a run-scoped postmortem
    # cannot discharge a campaign-level obligation without letting an unrelated run's
    # write-up clear it (spec §8b objection 4).
    run_id: str = ""
    campaign_id: str = ""
    hypothesis_status: str = ""
    summary: str = ""
    unexpected_observations: str = ""
    root_cause: str = ""
    verdict_override: str = "none"
    next_steps: str = ""
    asset_links: dict = field(default_factory=dict)
    author: str = ""
    status: str = "draft"
    project_slug: str = ""
    git_hash: str = ""
    git_dirty: bool = False
    script_sha256: str = ""
    refutation_criteria_met: list[str] = field(default_factory=list)
    #: obligation ids this postmortem discharges (build-order step 4)
    discharges: list[str] = field(default_factory=list)
    #: rule-card ids this postmortem judges to have been violated (§5, closing the loop to
    #: Piece 1). The cards most often cited here are the ones worth promoting from advisory
    #: prose into a hard lint — this is how the corpus acquires a usage signal instead of
    #: accreting forever.
    violated_cards: list[str] = field(default_factory=list)
    anomalies: dict = field(default_factory=dict)
    #: file format: 1 (default, today's behaviour) or 2 (enables `[fork]` and `[[amendments]]`)
    format: int = 1
    #: the raw `[fork]` table (format 2); checked by the near_bound discharge, not parsed here
    fork: dict = field(default_factory=dict)
    amendments: list[Amendment] = field(default_factory=list)


def amendments_as_dicts(postmortem: Postmortem) -> list[dict]:
    return [dataclasses.asdict(a) for a in postmortem.amendments]


def removed_amendments(previous: list[dict], postmortem: Postmortem) -> list[str]:
    """Amendments recorded by an earlier validation that this file no longer carries, as
    "<analysis_run_id> (<kind>)" (the `amendment_removed` warning, log mode only)."""
    current = {(a.analysis_run_id, a.kind) for a in postmortem.amendments}
    return [
        f"{a.get('analysis_run_id', '')} ({a.get('kind', '')})"
        for a in previous
        if (a.get("analysis_run_id", ""), a.get("kind", "")) not in current
    ]


def amendments_by_run(workspace_root: Path | str, run_ids) -> list[dict]:
    """Amendments from every run-scoped postmortem under `workspace_root` whose run is in
    `run_ids`, one dict per amendment with the postmortem's `run_id` (campaign review)."""
    wanted = set(run_ids)
    out: list[dict] = []
    root = Path(workspace_root)
    if not wanted or not root.exists():
        return out
    for pm_file in iter_project_files(
        root, ".bth.postmortem.toml", event_name="postmortem.boundary_pruned"
    ):
        try:
            pm = parse_postmortem(pm_file)
        except Exception:
            continue
        if pm.run_id in wanted:
            out += [{"run_id": pm.run_id, **d} for d in amendments_as_dicts(pm)]
    return out


def _parse_format(data: dict) -> int:
    fmt = data.get("format", 1)
    if isinstance(fmt, bool) or fmt not in POSTMORTEM_FORMATS:
        raise ValueError(f"Unsupported postmortem format {fmt!r}: format must be 1 or 2")
    return fmt


def _parse_amendments(data: dict) -> list[Amendment]:
    raw = data.get("amendments", [])
    if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
        raise ValueError("amendments must be an array of tables ([[amendments]])")
    return [
        Amendment(
            analysis_run_id=str(e.get("analysis_run_id", "") or ""),
            amends_run_id=str(e.get("amends_run_id", "") or ""),
            kind=str(e.get("kind", "") or ""),
            outlier_units=[str(u) for u in (e.get("outlier_units", []) or [])],
            effect=str(e.get("effect", "") or ""),
            note=str(e.get("note", "") or ""),
        )
        for e in raw
    ]


def parse_postmortem(path: Path) -> Postmortem:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        raise ValueError(f"Failed to parse TOML: {e}") from e

    # The run_id can be in the top-level
    run_id = data.get("run_id")
    postmortem_section = data.get("postmortem", {})
    if not run_id:
        run_id = postmortem_section.get("run_id")

    campaign_id = data.get("campaign_id") or postmortem_section.get("campaign_id") or ""

    # Exactly one of run_id / campaign_id. Replaces the previous unconditional run_id raise;
    # every existing postmortem stays valid because run_id is still accepted — only the error
    # message changes, not the accepted input.
    if not run_id and not campaign_id:
        raise ValueError("Missing run_id (or campaign_id for a campaign-scoped postmortem)")
    if run_id and campaign_id:
        raise ValueError("Set exactly one of run_id or campaign_id, not both")

    if "hypothesis_status" not in postmortem_section:
        raise ValueError("Missing hypothesis_status in [postmortem]")

    hypothesis_status = postmortem_section.get("hypothesis_status")

    summary = postmortem_section.get("summary", "")
    unexpected_observations = postmortem_section.get("unexpected_observations", "")
    root_cause = postmortem_section.get("root_cause", "")
    verdict_override = postmortem_section.get("verdict_override", "none")
    next_steps = postmortem_section.get("next_steps", "")
    author = postmortem_section.get("author", "")
    status = postmortem_section.get("status", "submitted")
    project_slug = postmortem_section.get("project_slug", "")
    git_hash = postmortem_section.get("git_hash", "")
    git_dirty = postmortem_section.get("git_dirty", False)
    script_sha256 = postmortem_section.get("script_sha256", "")
    refutation_criteria_met = postmortem_section.get("refutation_criteria_met", [])

    # Decisions section can also have verdict_override and next_steps
    decisions = data.get("decisions", {})
    if not verdict_override or verdict_override == "none":
        verdict_override = decisions.get("verdict_override", "none")
    if not next_steps:
        next_steps = decisions.get("next_steps", "")

    # Parse asset_links
    asset_links = data.get("asset_links", {})
    anomalies = data.get("anomalies", {})

    fork = data.get("fork", {})
    if not isinstance(fork, dict):
        raise ValueError("fork must be a table ([fork])")

    return Postmortem(
        run_id=run_id or "",
        campaign_id=campaign_id,
        discharges=list(postmortem_section.get("discharges", []) or []),
        violated_cards=list(postmortem_section.get("violated_cards", []) or []),
        hypothesis_status=hypothesis_status,
        summary=summary,
        unexpected_observations=unexpected_observations,
        root_cause=root_cause,
        verdict_override=verdict_override,
        next_steps=next_steps,
        asset_links=asset_links,
        author=author,
        status=status,
        project_slug=project_slug,
        git_hash=git_hash,
        git_dirty=git_dirty,
        script_sha256=script_sha256,
        refutation_criteria_met=refutation_criteria_met,
        anomalies=anomalies,
        format=_parse_format(data),
        fork=fork,
        amendments=_parse_amendments(data),
    )


def postmortem_applied_event_data(postmortem: Postmortem, path: Path) -> dict:
    """`run.postmortem_applied.data` (delivery step 2b / AC-25): the parsed
    fields `compact.py`'s legacy fold reads off a validated postmortem file --
    `status`, `verdict_override`, `author`, `path`, `hypothesis_status`,
    `has_anomalies`, `summary`, `asset_links` -- so the fold never has to
    re-read the file itself. `sha256` is the postmortem file's content hash (the
    spec's natural key alongside run_id), so two different contents applied to
    one run stay distinguishable."""
    return {
        "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "status": postmortem.status,
        "verdict_override": postmortem.verdict_override,
        "author": postmortem.author,
        "path": str(path),
        "hypothesis_status": postmortem.hypothesis_status,
        "has_anomalies": bool(postmortem.anomalies),
        "summary": postmortem.summary,
        "asset_links": postmortem.asset_links,
        "amendments": amendments_as_dicts(postmortem),
    }


def validate_postmortem(
    postmortem: Postmortem,
    workspace_root: Path | None = None,
    run: Run | None = None,
    strict_files: bool = False,
    postmortem_path: Path | None = None,
    run_lookup: Callable[[str], object | None] | None = None,
) -> ValidationResult:
    """Validate a postmortem. `run_lookup(run_id) -> Run | None` (unique-prefix resolution) feeds
    only the format-2 checks; format-1 files validate exactly as they always did."""
    errors = []
    warnings: list[str] = []

    # 1. Check refutation mapping consistency
    # refuted and pass -> invalid
    # held and fail -> invalid
    # inconclusive and marginal -> valid
    if postmortem.hypothesis_status == "refuted" and postmortem.verdict_override == "pass":
        errors.append(ValidationError("Hypothesis status is refuted but verdict override is pass"))
        if postmortem_path:
            event(
                "postmortem.validate_error",
                path=str(postmortem_path),
                reason="Hypothesis status is refuted but verdict override is pass",
            )
    if postmortem.hypothesis_status == "held" and postmortem.verdict_override == "fail":
        errors.append(ValidationError("Hypothesis status is held but verdict override is fail"))
        if postmortem_path:
            event(
                "postmortem.validate_error",
                path=str(postmortem_path),
                reason="Hypothesis status is held but verdict override is fail",
            )

    # 2. Check asset links paths and checksums
    if postmortem.asset_links and workspace_root:
        for key, link_val in postmortem.asset_links.items():
            path_str = None
            sha256_val = None
            if isinstance(link_val, str):
                path_str = link_val
            elif isinstance(link_val, dict):
                path_str = link_val.get("path")
                sha256_val = link_val.get("sha256")

            if path_str:
                path = Path(path_str)
                # Check absolute path
                if path.is_absolute():
                    err = ValidationError(f"Asset link '{key}' is an absolute path: '{path_str}'")
                    errors.append(err)
                    if postmortem_path:
                        event(
                            "postmortem.validate_error",
                            path=str(postmortem_path),
                            reason=err.message,
                        )
                    continue

                # Check escaping workspace
                resolved_path = (workspace_root / path).resolve()
                try:
                    resolved_path.relative_to(workspace_root.resolve())
                except ValueError:
                    err = ValidationError(
                        f"Asset link '{key}' escapes the workspace (escape the workspace): '{path_str}'"
                    )
                    errors.append(err)
                    if postmortem_path:
                        event(
                            "postmortem.validate_error",
                            path=str(postmortem_path),
                            reason=err.message,
                        )
                    continue

                # Check file existence and checksum if sha256_val is provided
                file_path = workspace_root / path
                if not file_path.exists():
                    if sha256_val or strict_files:
                        err = ValidationError(f"Asset link '{key}' does not exist: '{path_str}'")
                        errors.append(err)
                        if postmortem_path:
                            event(
                                "postmortem.validate_error",
                                path=str(postmortem_path),
                                reason=err.message,
                            )
                else:
                    if sha256_val:
                        # calculate checksum
                        h = hashlib.sha256()
                        try:
                            with open(file_path, "rb") as f:
                                while chunk := f.read(8192):
                                    h.update(chunk)
                            actual_sha = h.hexdigest()
                            if actual_sha != sha256_val:
                                err = ValidationError(
                                    f"Asset link '{key}' checksum mismatch: expected '{sha256_val}', got '{actual_sha}'"
                                )
                                errors.append(err)
                                if postmortem_path:
                                    event(
                                        "postmortem.validate_error",
                                        path=str(postmortem_path),
                                        reason=err.message,
                                    )
                        except Exception as e:
                            err = ValidationError(
                                f"Asset link '{key}' could not compute checksum: {e}"
                            )
                            errors.append(err)
                            if postmortem_path:
                                event(
                                    "postmortem.validate_error",
                                    path=str(postmortem_path),
                                    reason=err.message,
                                )

    # 3. Drift detection if run is provided
    if run:
        # Check dirty state
        if run.git_dirty:
            err = ValidationError("Run was recorded with git_dirty = True")
            errors.append(err)
            if postmortem_path:
                event("postmortem.validate_error", path=str(postmortem_path), reason=err.message)
        # Check git hash drift (warn or error?)
        if workspace_root:
            git_state = capture_git_state(workspace_root)
            if git_state.hash != "unknown" and run.git_hash != git_state.hash:
                err = ValidationError(
                    f"Code drift detected: run git_hash '{run.git_hash}' differs from workspace HEAD '{git_state.hash}'"
                )
                errors.append(err)
                if postmortem_path:
                    event(
                        "postmortem.validate_error", path=str(postmortem_path), reason=err.message
                    )

    # 4. Format-2 sections (spec 261026 D5). Format 1 ignores them, but says so.
    if postmortem.format != 2 and (postmortem.fork or postmortem.amendments):
        warnings.append(
            "[fork]/[[amendments]] present but not enforced: add format = 2 to enable them"
        )

    if postmortem.format == 2:
        _validate_amendments(postmortem, run_lookup, errors, warnings, postmortem_path)
    _validate_fork(postmortem, errors, postmortem_path)

    ok = len(errors) == 0
    if ok and postmortem_path:
        event(
            "postmortem.validated",
            path=str(postmortem_path),
            run_id=postmortem.run_id,
            sprint_id=None,
        )
    return ValidationResult(ok=ok, errors=errors, warnings=warnings)


def _validate_fork(
    postmortem: Postmortem, errors: list[ValidationError], postmortem_path: Path | None
) -> None:
    """Fork checks for a postmortem that discharges a `near_bound` obligation (spec 261026 D6).
    Run-independent: they read only `pm.fork` and `pm.discharges`, so they fire with no run."""
    if not any(oid.endswith("_near_bound") for oid in postmortem.discharges):
        return

    def err(message: str) -> None:
        errors.append(ValidationError(message))
        if postmortem_path:
            event("postmortem.validate_error", path=str(postmortem_path), reason=message)

    if postmortem.format != 2:
        err("a near_bound obligation can only be discharged by a postmortem with format = 2")
        return
    fork = postmortem.fork
    if not fork:
        err("discharging near_bound requires a [fork] record (choice, reason, cited)")
        return
    choice = str(fork.get("choice", "") or "")
    if choice not in FORK_CHOICES:
        err(f"[fork] choice {choice!r} is not one of {list(FORK_CHOICES)}")
    if not str(fork.get("reason", "") or "").strip():
        err("[fork] reason must not be empty")
    if choice == "stop" and fork.get("cited") is True:
        err("[fork] choice = stop cannot have cited = true: a cited result needs a follow-up")


def _validate_amendments(
    postmortem: Postmortem,
    run_lookup: Callable[[str], object | None] | None,
    errors: list[ValidationError],
    warnings: list[str],
    postmortem_path: Path | None,
) -> None:
    """Format-2 `[[amendments]]` checks (spec 261026 D5). Run-independent rules always apply;
    the ones that resolve a run id need `run_lookup` and become a warning without it."""

    def err(message: str) -> None:
        errors.append(ValidationError(message))
        if postmortem_path:
            event("postmortem.validate_error", path=str(postmortem_path), reason=message)

    def resolve(run_id: str) -> tuple[str | None, str | None]:
        """(full id or None, error text or None) for `run_id` through `run_lookup`."""
        try:
            found = run_lookup(run_id)  # type: ignore[misc]
        except Exception as e:  # noqa: BLE001 -- an ambiguous prefix or a failed read
            return None, str(e)
        return (getattr(found, "id", None) if found is not None else None), None

    skipped = False
    for i, a in enumerate(postmortem.amendments):
        where = f"amendments[{i}]"
        if not a.analysis_run_id:
            err(f"{where}: analysis_run_id is required")
        if a.kind not in AMENDMENT_KINDS:
            err(f"{where}: kind {a.kind!r} is not one of {list(AMENDMENT_KINDS)}")
        if a.effect not in AMENDMENT_EFFECTS:
            err(f"{where}: effect {a.effect!r} is not one of {list(AMENDMENT_EFFECTS)}")
        if a.kind == "distributional_outlier" and not a.outlier_units:
            err(f"{where}: a distributional_outlier amendment must name outlier_units")
        if a.effect == "verdict_changed" and postmortem.verdict_override in ("", "none"):
            err(f"{where}: effect verdict_changed requires a verdict_override other than none")
        if postmortem.campaign_id and not a.amends_run_id:
            err(f"{where}: amends_run_id is required in a campaign-scoped postmortem")
        if not a.analysis_run_id:
            continue
        amended = a.amends_run_id or postmortem.run_id
        if run_lookup is None:
            skipped = True
            continue
        full, problem = resolve(a.analysis_run_id)
        if problem is not None:
            err(f"{where}: analysis_run_id {a.analysis_run_id!r} cannot be resolved: {problem}")
            continue
        if full is None:
            err(
                f"{where}: analysis_run_id {a.analysis_run_id!r} is not in the catalog; if this "
                "is a log-mode run not yet ingested (SLURM / BTH_NO_INGEST), run `bth compact` "
                "and retry"
            )
            continue
        amended_full, _ = resolve(amended) if amended else (None, None)
        if full == (amended_full or amended):
            err(f"{where}: analysis_run_id must not equal the amended run ({amended!r})")
    if skipped:
        warnings.append(
            "amendments: run-id checks skipped: no catalog to resolve analysis_run_id against"
        )


def find_postmortem(
    workspace_root: Path | str,
    run_id: str = "",
    campaign_id: str = "",
) -> tuple[Path, Postmortem] | None:
    """Locate the postmortem for a run OR a campaign. Returns (path, parsed) or None.

    Exactly one of `run_id` / `campaign_id`, mirroring the `Postmortem` schema itself. Both
    retrieval surfaces previously matched on `pm.run_id` only, so a campaign-scoped postmortem
    could be written and validated but never retrieved by id — the `campaign_id` field added
    for §8b objection 4 was write-only.

    Matching is against the *named* field rather than "either id equals this string": run ids
    and campaign ids are separate namespaces with no discriminator, the same collision
    `obligations.py` prefixes its ledger filenames to avoid. An `id` that matched either would
    reintroduce it here.

    Shared by `bth postmortem show` and the `postmortem_get` MCP tool so the two cannot
    diverge — the same reason `scaffold_postmortem_template` is shared (debt #479).
    """
    if bool(run_id) == bool(campaign_id):
        raise ValueError("Pass exactly one of run_id or campaign_id")

    root = Path(workspace_root)
    if not root.exists():
        return None

    # Boundary-aware walk (backlog #4233): a bare rglob crossed into vendored
    # submodules and nested .claude/worktrees/ checkouts, so a stale copy could be
    # returned as this workspace's answer for a run/campaign id. This is the read
    # path behind `bth postmortem show` and the postmortem_get MCP tool.
    for pm_file in iter_project_files(
        root, ".bth.postmortem.toml", event_name="postmortem.boundary_pruned"
    ):
        try:
            pm = parse_postmortem(pm_file)
        except Exception:
            continue
        if run_id and pm.run_id == run_id:
            return pm_file, pm
        if campaign_id and pm.campaign_id == campaign_id:
            return pm_file, pm
    return None


def scaffold_campaign_postmortem_template(
    campaign_id: str,
    workspace_root: Path,
    open_obligation_ids: list[str] | tuple[str, ...] = (),
) -> Path:
    """Write a draft campaign-scoped postmortem and return its path.

    `find_run_for_scaffold` + `scaffold_postmortem_template` are hard-keyed to a Run row —
    they resolve the script path out of the run's `command` string — so a campaign-scoped
    postmortem had to be hand-authored. A campaign has no script and no single directory to
    sit beside, hence `.bth/postmortems/`, mirroring `.bth/claims/` and `.bth/obligations/`.

    Open obligation ids are emitted as a *commented* checklist and never pre-filled into
    `discharges`. Naming an obligation there asserts this write-up explains it; a scaffold
    that pre-filled them would discharge the ledger by default, which inverts the point.
    """
    pm_dir = Path(workspace_root) / ".bth" / "postmortems"
    pm_dir.mkdir(parents=True, exist_ok=True)
    postmortem_path = pm_dir / f"campaign_{campaign_id}.bth.postmortem.toml"

    obligation_block = _obligation_checklist(open_obligation_ids, "campaign and its member runs")

    toml_content = f"""campaign_id = "{campaign_id}"
format = 2

[postmortem]
hypothesis_status = "unassigned"
summary = ""
unexpected_observations = ""
root_cause = ""
verdict_override = "none"
next_steps = ""
author = ""
status = "draft"
discharges = []
violated_cards = []

{_FORK_SCAFFOLD}
{obligation_block}
[asset_links]
"""
    postmortem_path.write_text(toml_content)
    return postmortem_path


def find_run_for_scaffold(run_id: str, catalog_dir: Path) -> tuple[str, str] | None:
    """Find (command, project_slug) for run_id, checking the warm DB then cool fragments.

    Unlike query.get_run(), this always checks both tiers regardless of whether a
    warm DB file exists — a freshly-run script only lives in the cool tier until
    the next `bth compact`, and callers here (postmortem scaffold) must still find it.
    """
    from bathos.catalog import read_runs
    from bathos.index import catalog_readable, connect_read

    if catalog_readable(catalog_dir):
        con = connect_read(catalog_dir)
        try:
            row = con.execute(
                "SELECT command, project_slug FROM runs WHERE id = ?", [run_id]
            ).fetchone()
            if row:
                return row[0], row[1]
        except Exception:
            pass
        finally:
            con.close()

    for r in read_runs(catalog_dir):
        if r.id == run_id:
            return r.command, r.project_slug
    return None


def _script_from_command(command: str, workspace_root: Path) -> Path | None:
    """The run's script, from its recorded command string, or None.

    Only tokens inside the workspace and outside `PRUNE_DIRS` count, and a `.py` token
    wins over any other existing file. Otherwise an absolute interpreter such as
    `/abs/.venv/bin/python3` is taken for the script, and the postmortem lands in
    `.venv/bin/` (EDQUOT on Engaging, debt #3046) where no walk can find it.

    Containment is lexical (`normpath`, no symlink resolution): a script symlinked into
    the workspace from elsewhere still counts, and its postmortem sits beside the link.
    """
    import os
    import shlex

    given = Path(os.path.abspath(workspace_root))
    roots = {given, Path(workspace_root).resolve()}
    candidates = []
    for part in shlex.split(command) if command else []:
        p = Path(os.path.normpath(given / part))
        rel = None
        for root in roots:
            try:
                rel = p.relative_to(root)
                break
            except ValueError:
                continue
        if rel is None or PRUNE_DIRS.intersection(rel.parts):
            continue
        candidates.append(p)

    for p in candidates:
        if p.suffix == ".py":
            return p
    for p in candidates:
        if p.is_file():
            return p
    return None


_FORK_SCAFFOLD = """[fork]
# What was decided after a near-bound result, and why. Required to discharge a near_bound
# obligation. choice: stop | extend | replicate | breakdown | escalate
choice = ""
reason = ""
cited = false
follow_up_run_ids = []
"""


def _obligation_checklist(open_obligation_ids, scope: str) -> str:
    if open_obligation_ids:
        checklist = "\n".join(f"#   {oid}" for oid in open_obligation_ids)
        return (
            f"# Open obligations for this {scope}. Copy the ids this write-up actually explains\n"
            "# into `discharges` above: a valid postmortem discharges exactly what it names,\n"
            "# and nothing else.\n" + checklist + "\n"
        )
    return f"# No open obligations recorded for this {scope}.\n"


def scaffold_postmortem_template(
    command: str,
    run_id: str,
    workspace_root: Path,
    open_obligation_ids: list[str] | tuple[str, ...] = (),
) -> Path:
    """Resolve the script path from a run's command string and write a draft postmortem TOML.

    Shared by the CLI (`bth postmortem scaffold`) and the MCP tool
    (`postmortem_scaffold`) so the two surfaces cannot diverge (regression: debt #479).

    Returns the path to the written template.
    """
    script_path = _script_from_command(command, workspace_root)
    if script_path is None:
        # Same home as campaign postmortems; `find_postmortem` walks `.bth/`.
        pm_dir = Path(workspace_root) / ".bth" / "postmortems"
        pm_dir.mkdir(parents=True, exist_ok=True)
        postmortem_path = pm_dir / f"run_{run_id}.bth.postmortem.toml"
    else:
        script_path.parent.mkdir(parents=True, exist_ok=True)
        postmortem_path = script_path.parent / f"{script_path.name}.{run_id}.bth.postmortem.toml"

    toml_content = f"""run_id = "{run_id}"
format = 2

[postmortem]
hypothesis_status = "unassigned"
summary = ""
unexpected_observations = ""
root_cause = ""
verdict_override = "none"
next_steps = ""
author = ""
status = "draft"
discharges = []

{_FORK_SCAFFOLD}
{_obligation_checklist(open_obligation_ids, "run")}
[asset_links]
"""
    postmortem_path.write_text(toml_content)
    return postmortem_path
