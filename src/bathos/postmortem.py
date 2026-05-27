from __future__ import annotations
import tomllib
import hashlib
from pathlib import Path
from dataclasses import dataclass, field
from bathos.schema import Run
from bathos.git import capture_git_state
from bathos.telemetry import event

@dataclass
class ValidationError:
    message: str

@dataclass
class ValidationResult:
    ok: bool
    errors: list[ValidationError] = field(default_factory=list)

@dataclass
class Postmortem:
    run_id: str
    hypothesis_status: str
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
    anomalies: dict = field(default_factory=dict)

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
    
    if not run_id:
        raise ValueError("Missing run_id")
        
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
    
    return Postmortem(
        run_id=run_id,
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
    )

def validate_postmortem(
    postmortem: Postmortem,
    workspace_root: Path | None = None,
    run: Run | None = None,
    catalog_dir: Path | None = None,
    strict: bool = False,
    strict_files: bool = False,
    postmortem_path: Path | None = None,
) -> ValidationResult:
    errors = []

    # 1. Check refutation mapping consistency
    # refuted and pass -> invalid
    # held and fail -> invalid
    # inconclusive and marginal -> valid
    if postmortem.hypothesis_status == "refuted" and postmortem.verdict_override == "pass":
        errors.append(ValidationError("Hypothesis status is refuted but verdict override is pass"))
        if postmortem_path:
            event("postmortem.validate_error", path=str(postmortem_path), reason="Hypothesis status is refuted but verdict override is pass")
    if postmortem.hypothesis_status == "held" and postmortem.verdict_override == "fail":
        errors.append(ValidationError("Hypothesis status is held but verdict override is fail"))
        if postmortem_path:
            event("postmortem.validate_error", path=str(postmortem_path), reason="Hypothesis status is held but verdict override is fail")

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
                        event("postmortem.validate_error", path=str(postmortem_path), reason=err.message)
                    continue

                # Check escaping workspace
                resolved_path = (workspace_root / path).resolve()
                try:
                    resolved_path.relative_to(workspace_root.resolve())
                except ValueError:
                    err = ValidationError(f"Asset link '{key}' escapes the workspace (escape the workspace): '{path_str}'")
                    errors.append(err)
                    if postmortem_path:
                        event("postmortem.validate_error", path=str(postmortem_path), reason=err.message)
                    continue

                # Check file existence and checksum if sha256_val is provided
                file_path = workspace_root / path
                if not file_path.exists():
                    if sha256_val or strict_files:
                        err = ValidationError(f"Asset link '{key}' does not exist: '{path_str}'")
                        errors.append(err)
                        if postmortem_path:
                            event("postmortem.validate_error", path=str(postmortem_path), reason=err.message)
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
                                err = ValidationError(f"Asset link '{key}' checksum mismatch: expected '{sha256_val}', got '{actual_sha}'")
                                errors.append(err)
                                if postmortem_path:
                                    event("postmortem.validate_error", path=str(postmortem_path), reason=err.message)
                        except Exception as e:
                            err = ValidationError(f"Asset link '{key}' could not compute checksum: {e}")
                            errors.append(err)
                            if postmortem_path:
                                event("postmortem.validate_error", path=str(postmortem_path), reason=err.message)

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
                err = ValidationError(f"Code drift detected: run git_hash '{run.git_hash}' differs from workspace HEAD '{git_state.hash}'")
                errors.append(err)
                if postmortem_path:
                    event("postmortem.validate_error", path=str(postmortem_path), reason=err.message)

    ok = len(errors) == 0
    if ok and postmortem_path:
        event("postmortem.validated", path=str(postmortem_path), run_id=postmortem.run_id, sprint_id=None)
    return ValidationResult(ok=ok, errors=errors)
