"""Static HTML export for bathos catalog."""
from __future__ import annotations

import importlib.resources
import json
import sys
from pathlib import Path

from jinja2 import Environment, PackageLoader, TemplateError

from bathos.schema import Run
from bathos.campaigns import Campaign
from bathos.viz.data import project_run, project_campaign, RunDisplay, CampaignDisplay


def _load_static_asset(filename: str) -> str:
    """Load a static asset from src/bathos/viz/static/.

    Args:
        filename: Name of the file to load (e.g., 'alpine.min.js')

    Returns:
        The file contents as a string

    Raises:
        RuntimeError: If the file cannot be loaded
    """
    try:
        files = importlib.resources.files("bathos.viz")
        static_file = files.joinpath("static", filename)
        return static_file.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to load static asset {filename}: {e}") from e


def _project_campaigns(
    campaigns: list[Campaign],
    catalog_dir: Path | None = None,
) -> list[CampaignDisplay]:
    """Project campaigns to CampaignDisplay TypedDicts with aggregates.

    For each campaign, queries DuckDB for outcome aggregates if catalog_dir
    is provided. If catalog_dir is None, uses zeroed aggregates.

    Args:
        campaigns: List of Campaign objects
        catalog_dir: Optional path to catalog directory with bathos.db

    Returns:
        List of CampaignDisplay TypedDicts with aggregates populated
    """
    results: list[CampaignDisplay] = []

    for campaign in campaigns:
        aggregates: dict = {
            "run_count": 0,
            "outcome_distribution": {},
            "residual_rate": 0.0,
            "bypass_rate": 0.0,
            "unknown_rate": 0.0,
            "anomalies": [],
        }

        # Query DuckDB for aggregates if catalog_dir provided
        if catalog_dir is not None:
            try:
                import duckdb

                conn = duckdb.connect(
                    str(catalog_dir / "bathos.db"), read_only=True
                )

                agg_sql = """
                    SELECT outcome, COUNT(*) AS n,
                      COUNT(*) FILTER (WHERE outcome_is_residual) AS n_residual,
                      COUNT(*) FILTER (WHERE sidecar_mode = 'bypassed') AS n_bypassed,
                      COUNT(*) FILTER (WHERE outcome IN ('unknown', '')) AS n_unknown
                    FROM runs WHERE campaign_id = ? GROUP BY outcome
                """

                rows = conn.execute(agg_sql, [campaign.id]).fetchall()
                col_names = [d[0] for d in conn.description]
                row_dicts = [dict(zip(col_names, row)) for row in rows]
                conn.close()

                # Aggregate results
                run_count = sum(r["n"] for r in row_dicts)
                outcome_distribution = {r["outcome"]: r["n"] for r in row_dicts}
                n_residual = sum(r["n_residual"] for r in row_dicts)
                n_bypassed = sum(r["n_bypassed"] for r in row_dicts)
                n_unknown = sum(r["n_unknown"] for r in row_dicts)

                residual_rate = n_residual / run_count if run_count else 0.0
                bypass_rate = n_bypassed / run_count if run_count else 0.0
                unknown_rate = n_unknown / run_count if run_count else 0.0

                aggregates = {
                    "run_count": run_count,
                    "outcome_distribution": outcome_distribution,
                    "residual_rate": residual_rate,
                    "bypass_rate": bypass_rate,
                    "unknown_rate": unknown_rate,
                    "anomalies": [],
                }
            except Exception:
                # If DuckDB query fails, use zeroed aggregates
                pass

        results.append(project_campaign(campaign, aggregates))

    return results


def render_html_report(
    runs: list[Run],
    campaigns: list[Campaign] | None = None,
    catalog_state: str = "warm",
    catalog_dir: Path | None = None,
    total_run_count: int | None = None,
) -> str:
    """Render runs and campaigns to a static HTML report.

    Loads Alpine.js and Pico CSS inline, projects runs and campaigns to
    display format, renders Jinja2 template, and injects a data blob as
    window.__BATHOS_DATA__ for client-side application state.

    Args:
        runs: List of Run objects to display
        campaigns: Optional list of Campaign objects to display
        catalog_state: State label for catalog ("warm" or "cool"), passed to template
        catalog_dir: Optional path to catalog directory for campaign aggregates
        total_run_count: Optional total run count to display (defaults to len(runs))

    Returns:
        HTML string ready for writing to file

    Raises:
        RuntimeError: If static assets cannot be loaded
        TemplateError: If Jinja2 template rendering fails
    """
    # Load static assets
    alpine_js = _load_static_asset("alpine.min.js")
    pico_css = _load_static_asset("pico.min.css")

    # Project runs and campaigns
    run_displays: list[RunDisplay] = [project_run(run) for run in runs]
    campaign_displays: list[CampaignDisplay] = _project_campaigns(
        campaigns or [], catalog_dir
    )

    # Build data blob for embedding
    data_blob = {
        "runs": run_displays,
        "campaigns": campaign_displays,
        "catalog_state": catalog_state,
    }

    # Render template
    env = Environment(loader=PackageLoader("bathos.viz", "templates"))
    template = env.get_template("index.html")
    html = template.render(
        alpine_js=alpine_js,
        pico_css=pico_css,
        catalog_state=catalog_state,
        runs=run_displays,
        campaigns=campaign_displays,
        total_run_count=total_run_count if total_run_count is not None else len(run_displays),
    )

    # Inject data blob into head before </head> tag
    data_json = json.dumps(data_blob)
    script_block = f'\n    <script>\n        window.__BATHOS_DATA__ = {data_json};\n    </script>\n</head>'
    html = html.replace("</head>", script_block)

    return html


def estimate_html_size(html: str) -> float:
    """Estimate HTML size in megabytes.

    Args:
        html: HTML string

    Returns:
        Size in MB
    """
    return len(html) / (1024 * 1024)


def export_html(
    runs: list[Run],
    campaigns: list[Campaign] | None = None,
    output_path: str | None = None,
    catalog_dir: Path | None = None,
) -> tuple[str, bool]:
    """Export runs and campaigns to a static HTML file.

    Args:
        runs: List of Run objects to export
        campaigns: Optional list of Campaign objects to export
        output_path: Path to write HTML to (defaults to "report.html")
        catalog_dir: Optional path to catalog directory for campaign aggregates

    Returns:
        Tuple of (output_path, size_warning_issued)
            output_path: Path where HTML was written
            size_warning_issued: True if file size exceeded 5 MB (warning printed to stderr)
    """
    if output_path is None:
        output_path = "report.html"

    # Render report
    html = render_html_report(
        runs,
        campaigns=campaigns,
        catalog_dir=catalog_dir,
    )

    # Check size and warn if too large
    size_mb = estimate_html_size(html)
    size_warning = False
    if size_mb > 5.0:
        print(
            f"Warning: HTML report is {size_mb:.1f} MB (> 5 MB). "
            "Consider filtering runs or compressing.",
            file=sys.stderr,
        )
        size_warning = True

    # Write to file
    Path(output_path).write_text(html, encoding="utf-8")

    return output_path, size_warning
