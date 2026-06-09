"""Test suite for campaign_report sidecar schema (CAMPAIGN-REPORT-1).

The campaign_report is an out-of-catalog JSON sidecar that captures the truth-only
summary of a concluded campaign, closing the recon gap where campaign_review currently
renders stats to console and discards them.

Path: <catalog>/sidecars/<campaign_id>/campaign_report.json

Schema: {
  report_version: str,
  campaign_id: str,
  total_runs: int,
  residual_rate: float,
  bypass_rate: float,
  unknown_rate: float,
  outcome_distribution: dict[str, int],
  anomalies: list[str],
  popper: dict | None,
  conclude: str | None,  # Campaign conclusion narrative from Campaign.conclusion
  figure_manifest_ref: str | None,  # Path reference to the figure manifest
  stage_breakdown: dict[str | None, int],  # Counts of runs by stage_name, with null bucket
}

All fields except report_version, campaign_id, and stage_breakdown are optional/additive.
"""
import json
import tempfile
from pathlib import Path

from bathos.campaign_report import CampaignReport


class TestCampaignReportSchema:
    """Verify the campaign_report schema validates correctly."""

    def test_report_creates_with_required_fields(self):
        """Given required fields, report initializes with no validation errors."""
        report = CampaignReport(
            report_version="1.0",
            campaign_id="campaign_abc123",
            total_runs=10,
            residual_rate=0.1,
            bypass_rate=0.05,
            unknown_rate=0.0,
            outcome_distribution={"success": 8, "failure": 2},
            anomalies=[],
            stage_breakdown={"exploration": 10},
        )
        assert report.report_version == "1.0"
        assert report.campaign_id == "campaign_abc123"
        assert report.total_runs == 10
        assert report.stage_breakdown == {"exploration": 10}

    def test_report_with_optional_fields(self):
        """Given optional fields, they are captured in the report."""
        report = CampaignReport(
            report_version="1.0",
            campaign_id="camp_xyz",
            total_runs=5,
            residual_rate=0.2,
            bypass_rate=0.1,
            unknown_rate=0.0,
            outcome_distribution={"partial": 3, "error": 2},
            anomalies=["High residual rate: 20.0% (1/5 runs)"],
            popper={
                "mode": "sequential",
                "stopping_threshold": 0.05,
                "threshold_met": False,
                "scripts": [],
            },
            conclude="Experiment inconclusive; more runs needed.",
            figure_manifest_ref="sidecars/camp_xyz/figure_manifest.json",
            stage_breakdown={"exploration": 3, "validation": 2},
        )
        assert report.conclude == "Experiment inconclusive; more runs needed."
        assert report.figure_manifest_ref == "sidecars/camp_xyz/figure_manifest.json"
        assert report.popper["mode"] == "sequential"

    def test_report_stage_breakdown_with_null_key(self):
        """Given stage_breakdown with None (null) key, it is valid."""
        report = CampaignReport(
            report_version="1.0",
            campaign_id="camp_mixed",
            total_runs=4,
            residual_rate=0.0,
            bypass_rate=0.0,
            unknown_rate=0.0,
            outcome_distribution={"success": 4},
            anomalies=[],
            stage_breakdown={"exploration": 2, None: 2},  # Two runs have no stage_name
        )
        assert report.stage_breakdown.get(None) == 2
        assert report.stage_breakdown.get("exploration") == 2
        assert sum(report.stage_breakdown.values()) == 4

    def test_report_zero_runs_valid(self):
        """Given a campaign with zero runs, report is valid."""
        report = CampaignReport(
            report_version="1.0",
            campaign_id="camp_empty",
            total_runs=0,
            residual_rate=0.0,
            bypass_rate=0.0,
            unknown_rate=0.0,
            outcome_distribution={},
            anomalies=[],
            stage_breakdown={},
        )
        assert report.total_runs == 0
        # Should serialize without error
        json_str = report.model_dump_json()
        assert "campaign_id" in json_str


class TestCampaignReportSerialization:
    """Verify JSON serialization round-trips correctly."""

    def test_report_to_json(self):
        """Given a report, model_dump_json produces valid JSON."""
        report = CampaignReport(
            report_version="1.0",
            campaign_id="camp_1",
            total_runs=10,
            residual_rate=0.1,
            bypass_rate=0.05,
            unknown_rate=0.0,
            outcome_distribution={"success": 9, "failure": 1},
            anomalies=["High residual rate: 10.0% (1/10 runs)"],
            stage_breakdown={"exploration": 10},
        )
        json_str = report.model_dump_json()
        parsed = json.loads(json_str)
        assert parsed["report_version"] == "1.0"
        assert parsed["campaign_id"] == "camp_1"
        assert parsed["total_runs"] == 10
        assert parsed["stage_breakdown"]["exploration"] == 10

    def test_report_from_json(self):
        """Given JSON, model_validate_json reconstructs the report."""
        json_str = json.dumps(
            {
                "report_version": "1.0",
                "campaign_id": "camp_2",
                "total_runs": 5,
                "residual_rate": 0.2,
                "bypass_rate": 0.0,
                "unknown_rate": 0.0,
                "outcome_distribution": {"success": 5},
                "anomalies": [],
                "stage_breakdown": {"calibration": 5},
            }
        )
        report = CampaignReport.model_validate_json(json_str)
        assert report.campaign_id == "camp_2"
        assert report.total_runs == 5
        assert report.stage_breakdown["calibration"] == 5

    def test_report_with_null_stage_roundtrip(self):
        """Given stage_breakdown with null key, JSON roundtrip preserves it."""
        report = CampaignReport(
            report_version="1.0",
            campaign_id="camp_null",
            total_runs=3,
            residual_rate=0.0,
            bypass_rate=0.0,
            unknown_rate=0.0,
            outcome_distribution={"success": 3},
            anomalies=[],
            stage_breakdown={"exploration": 2, None: 1},
        )
        json_str = report.model_dump_json()
        parsed = json.loads(json_str)
        # Pydantic serializes None key as string "None"
        assert parsed["stage_breakdown"].get("None") == 1 or parsed["stage_breakdown"].get(None) == 1

    def test_report_with_popper_roundtrip(self):
        """Given popper data in report, JSON roundtrip preserves it."""
        popper_data = {
            "mode": "sequential",
            "stopping_threshold": 0.05,
            "threshold_met": True,
            "scripts": [
                {
                    "script_key": "test.py",
                    "n_effective": 15,
                    "n_excluded": 2,
                    "evalue_product": 0.02,
                    "threshold_met": True,
                }
            ],
        }
        report = CampaignReport(
            report_version="1.0",
            campaign_id="camp_popper",
            total_runs=17,
            residual_rate=0.0,
            bypass_rate=0.0,
            unknown_rate=0.0,
            outcome_distribution={"success": 15, "error": 2},
            anomalies=[],
            popper=popper_data,
            stage_breakdown={"validation": 17},
        )
        json_str = report.model_dump_json()
        report_restored = CampaignReport.model_validate_json(json_str)
        assert report_restored.popper["mode"] == "sequential"
        assert report_restored.popper["threshold_met"] is True
        assert len(report_restored.popper["scripts"]) == 1


class TestCampaignReportFileHandling:
    """Verify reading and writing report to disk."""

    def test_report_write_to_file(self):
        """Given a report, write_report creates a valid JSON sidecar file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            campaign_id = "camp_test"
            sidecar_dir = Path(tmpdir) / "sidecars" / campaign_id
            sidecar_dir.mkdir(parents=True, exist_ok=True)

            report = CampaignReport(
                report_version="1.0",
                campaign_id=campaign_id,
                total_runs=5,
                residual_rate=0.0,
                bypass_rate=0.0,
                unknown_rate=0.0,
                outcome_distribution={"success": 5},
                anomalies=[],
                stage_breakdown={"test": 5},
            )

            report_path = sidecar_dir / "campaign_report.json"
            report.write_report(report_path)

            assert report_path.exists()
            with open(report_path) as f:
                data = json.load(f)
            assert data["campaign_id"] == campaign_id

    def test_report_read_from_file(self):
        """Given a report file, read_report reconstructs the report."""
        with tempfile.TemporaryDirectory() as tmpdir:
            campaign_id = "camp_read"
            sidecar_dir = Path(tmpdir) / "sidecars" / campaign_id
            sidecar_dir.mkdir(parents=True, exist_ok=True)

            report_path = sidecar_dir / "campaign_report.json"
            report_data = {
                "report_version": "1.0",
                "campaign_id": campaign_id,
                "total_runs": 3,
                "residual_rate": 0.0,
                "bypass_rate": 0.0,
                "unknown_rate": 0.0,
                "outcome_distribution": {"success": 3},
                "anomalies": [],
                "stage_breakdown": {"read_test": 3},
            }
            with open(report_path, "w") as f:
                json.dump(report_data, f)

            report = CampaignReport.read_report(report_path)
            assert report.campaign_id == campaign_id
            assert report.total_runs == 3

    def test_report_roundtrip_via_file(self):
        """Given a report, write and read preserves all data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            campaign_id = "camp_roundtrip"
            sidecar_dir = Path(tmpdir) / "sidecars" / campaign_id
            report_path = sidecar_dir / "campaign_report.json"

            original = CampaignReport(
                report_version="1.0",
                campaign_id=campaign_id,
                total_runs=10,
                residual_rate=0.1,
                bypass_rate=0.05,
                unknown_rate=0.0,
                outcome_distribution={"success": 8, "failure": 2},
                anomalies=["High residual rate: 10.0% (1/10 runs)"],
                conclude="Campaign completed with mixed results.",
                figure_manifest_ref=f"sidecars/{campaign_id}/figure_manifest.json",
                stage_breakdown={"exploration": 6, "validation": 4},
            )

            original.write_report(report_path)
            restored = CampaignReport.read_report(report_path)

            assert restored.report_version == original.report_version
            assert restored.campaign_id == original.campaign_id
            assert restored.total_runs == original.total_runs
            assert restored.conclude == original.conclude
            assert restored.figure_manifest_ref == original.figure_manifest_ref
            assert restored.stage_breakdown == original.stage_breakdown
