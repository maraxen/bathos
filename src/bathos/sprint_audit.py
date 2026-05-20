"""Cross-project audit of recent runs and campaigns."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from bathos.schema import CURRENT_SCHEMA_VERSION


def sprint_audit(hours: int = 24) -> dict:
    """Cross-project audit of recent runs and campaigns.

    Queries each registered project's warm DB (read-only, ATTACH).
    Skips projects with incompatible schema versions with a warning.

    Args:
        hours: Lookback window in hours (default 24).

    Returns:
        Dict with 'audit_results' (by project) and 'warnings' list.
    """
    from bathos.config import list_registered_projects

    projects = list_registered_projects()
    audit_results: dict = {}
    warnings: list[str] = []

    for project in projects:
        catalog_dir = Path(project["catalog_dir"])
        db_path = catalog_dir / "bathos.db"

        if not db_path.exists():
            warnings.append(
                f"Project {project['slug']}: no warm DB found "
                f"(run bth compact first). Skipping."
            )
            continue

        # Check schema version before querying
        try:
            db_check = duckdb.connect(str(db_path), read_only=True)
            version_rows = db_check.execute(
                "SELECT value FROM _schema_meta WHERE key = 'warm_version'"
            ).fetchall()
            db_check.close()

            if version_rows:
                version = version_rows[0][0]
                if version != CURRENT_SCHEMA_VERSION:
                    warnings.append(
                        f"Project {project['slug']}: schema version mismatch "
                        f"(has {version!r}, need {CURRENT_SCHEMA_VERSION!r}) — "
                        f"run bth compact first. Skipping."
                    )
                    continue
        except Exception as e:
            warnings.append(
                f"Project {project['slug']}: failed schema check — {e}. Skipping."
            )
            continue

        # Safe to query
        try:
            db = duckdb.connect(str(db_path), read_only=True)
            db.execute("SET TimeZone='UTC'")
            cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()

            rows = db.execute(
                """
                SELECT id, campaign_id, sidecar_mode, outcome, outcome_is_residual, timestamp
                FROM runs
                WHERE timestamp > ?
                ORDER BY timestamp DESC
            """,
                [cutoff],
            ).fetchall()

            # Group by campaign
            by_campaign: dict[str, list] = {}
            for run_id, campaign_id, sidecar_mode, outcome, is_residual, ts in rows:
                key = campaign_id or "_uncampaigned"
                if key not in by_campaign:
                    by_campaign[key] = []
                by_campaign[key].append(
                    {
                        "run_id": run_id,
                        "sidecar_mode": sidecar_mode,
                        "outcome": outcome,
                        "is_residual": bool(is_residual),
                    }
                )

            # Compute anomalies
            anomalies: list[str] = []
            for campaign_id, runs in by_campaign.items():
                total = len(runs)
                unknown_count = sum(1 for r in runs if r["outcome"] in ("unknown", "", None))
                bypassed_count = sum(1 for r in runs if r["sidecar_mode"] == "bypassed")
                residual_count = sum(1 for r in runs if r["is_residual"])

                if unknown_count > 0:
                    anomalies.append(
                        f"Campaign {campaign_id}: {unknown_count} runs with unknown outcome"
                    )
                if total > 0 and bypassed_count / total > 0.1:
                    anomalies.append(
                        f"Campaign {campaign_id}: {bypassed_count}/{total} "
                        f"bypassed (>{10:.0f}%)"
                    )
                if total > 0 and residual_count / total > 0.1:
                    anomalies.append(
                        f"Campaign {campaign_id}: residual rate "
                        f"{residual_count/total:.1%} > 10%"
                    )

            audit_results[project["slug"]] = {
                "runs": len(rows),
                "campaigns": len(by_campaign),
                "anomalies": anomalies,
            }
            db.close()
        except Exception as e:
            warnings.append(f"Project {project['slug']}: query failed — {e}. Skipping.")

    return {"audit_results": audit_results, "warnings": warnings}
