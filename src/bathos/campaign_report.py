"""Campaign report schema (CAMPAIGN-REPORT-1).

The campaign_report is an out-of-catalog JSON sidecar that captures the truth-only
summary of a concluded campaign, closing the recon gap where campaign_review currently
renders stats to console and discards them.

Key design principles:
1. Schema-free sidecar: This is NOT a DuckDB column; it's a declarative sidecar JSON file.
2. Truth-only: Contains only what bathos can assert from the catalog — summary stats,
   per-run rollups, outcome distribution, stage breakdown, and conclude narrative.
   NO critique scores, NO per-claim grounding (those are interpretive/presentation concerns).
3. Optional/additive: All fields except report_version, campaign_id, and stage_breakdown
   are optional and additive for backward compatibility.
4. Stage breakdown: Explicit null bucket for runs with no stage_name; enables historical
   pre-bump campaigns to be analyzed without updating every run.

Path: <catalog>/sidecars/<campaign_id>/campaign_report.json

Fields:
  - report_version: Schema version (e.g., '1.0'). Used for backward-compatibility.
  - campaign_id: Campaign ID this report belongs to. Must match the sidecar path directory.
  - total_runs: Total number of runs in the campaign.
  - residual_rate: Fraction of runs marked outcome_is_residual.
  - bypass_rate: Fraction of runs with sidecar_mode='bypassed'.
  - unknown_rate: Fraction of runs with outcome in ('unknown', '').
  - outcome_distribution: Dict mapping outcome strings to counts.
  - anomalies: List of anomaly strings (e.g., "High residual rate: 10.0% (1/10 runs)").
  - popper: Dict | None. POPPER sequential test summary (mode, stopping_threshold, threshold_met, scripts).
  - conclude: str | None. Campaign conclusion narrative from Campaign.conclusion.
  - figure_manifest_ref: str | None. Path reference to the figure manifest.
  - stage_breakdown: Dict[str|None, int]. Counts of runs by stage_name, with null as explicit bucket.

Example usage (from maraxiom consumer):
    from bathos.campaign_report import CampaignReport

    # Read report at end of campaign
    report = CampaignReport.read_report(
        Path("<catalog>/sidecars/camp_123/campaign_report.json")
    )

    # Access truth-only stats
    print(f"Total runs: {report.total_runs}")
    print(f"Residual rate: {report.residual_rate:.1%}")
    print(f"Stage breakdown: {report.stage_breakdown}")

    # Conclude narrative for deck
    if report.conclude:
        print(f"Narrative: {report.conclude}")
"""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, field_validator


class CampaignReport(BaseModel):
    """Campaign report: truth-only summary of a concluded campaign.

    This sidecar is emitted by bathos at campaign_conclude and consumed by maraxiom
    to inform deck scaffolding and provide owner-reviewable campaign summaries.
    """

    report_version: str
    """Schema version of this report (e.g., '1.0'). Used for backward-compatibility."""

    campaign_id: str
    """Campaign ID this report belongs to. Must match the sidecar path directory."""

    total_runs: int
    """Total number of runs in the campaign."""

    residual_rate: float
    """Fraction of runs marked outcome_is_residual (0.0 to 1.0)."""

    bypass_rate: float
    """Fraction of runs with sidecar_mode='bypassed' (0.0 to 1.0)."""

    unknown_rate: float
    """Fraction of runs with outcome in ('unknown', '') (0.0 to 1.0)."""

    outcome_distribution: dict[str, int]
    """Dict mapping outcome strings to counts. Example: {'success': 8, 'failure': 2}."""

    anomalies: list[str]
    """List of anomaly strings. Example: ['High residual rate: 10.0% (1/10 runs)']."""

    stage_breakdown: dict[str | None, int]
    """Counts of runs by stage_name, with None (null) as explicit bucket for untagged runs."""

    popper: dict[str, Any] | None = None
    """POPPER sequential test summary (optional). Contains mode, stopping_threshold, threshold_met, scripts."""

    conclude: str | None = None
    """Campaign conclusion narrative from Campaign.conclusion (optional)."""

    figure_manifest_ref: str | None = None
    """Path reference to the figure manifest (optional). Example: 'sidecars/<campaign_id>/figure_manifest.json'."""

    @field_validator("residual_rate", "bypass_rate", "unknown_rate")
    @classmethod
    def validate_rates(cls, v: float) -> float:
        """Validate that rates are between 0.0 and 1.0.

        Args:
            v: The rate value being validated.

        Returns:
            The validated rate.

        Raises:
            ValueError: If rate is not in [0.0, 1.0].
        """
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"Rate must be between 0.0 and 1.0, got {v}")
        return v

    @field_validator("total_runs")
    @classmethod
    def validate_total_runs(cls, v: int) -> int:
        """Validate that total_runs is non-negative.

        Args:
            v: The total_runs value being validated.

        Returns:
            The validated total_runs.

        Raises:
            ValueError: If total_runs is negative.
        """
        if v < 0:
            raise ValueError(f"total_runs must be non-negative, got {v}")
        return v

    def write_report(self, path: Path) -> None:
        """Write the report to a JSON sidecar file.

        Args:
            path: Path to write the campaign_report.json file.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(self.model_dump_json(indent=2))

    @classmethod
    def read_report(cls, path: Path) -> "CampaignReport":
        """Read a report from a JSON sidecar file.

        Args:
            path: Path to the campaign_report.json file.

        Returns:
            CampaignReport instance.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValidationError: If the JSON is invalid or violates the schema.
        """
        path = Path(path)
        with open(path) as f:
            return cls.model_validate_json(f.read())
