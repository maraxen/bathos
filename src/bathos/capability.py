"""Capability probe (B2-06, #2181, AC-20).

Grounding: B2-06's gate text asks for "a machine-checkable endpoint reporting whether Run.seed +
the stats battery are live (not a hand-maintained attestation)", so a loop controller (T2-27,
xtrax-side, not yet built) can "probe live-capability before confirmatory start" rather than
trusting a static doc/flag that could drift from reality.

"Live" here means infrastructure readiness, not data presence -- deliberately checked BEFORE any
campaign work happens (a not-yet-started confirmatory campaign has no seed data recorded yet, so a
data-presence check would always read "not live" and be useless for its own stated purpose):

- `seed_live`: whether the CURRENT catalog's warm `runs` table actually has the B2-02 columns
  (`seed`, `baseline_hpo_trials`, `baseline_hpo_compute_budget`). This is genuinely
  catalog-specific, not a hardcoded "yes, bathos vN+ supports this" answer -- schema migrations
  apply lazily (`bathos.compact.compact()`'s `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`), so an
  existing warm DB that hasn't been compacted since upgrading past schema v9 genuinely does not
  have these columns yet, and a caller querying it would get a real DuckDB error, not silently
  missing data.
- `stats_battery_live`: whether scipy (the `bathos[stats]` extra, B2-01) is importable in the
  current environment -- reuses `bathos.stats_gates`'s own `ScipyUnavailableError` lazy-import
  check, the same mechanism `run_stats_battery` itself uses to decide `verdict="underpowered"`.

Both checks are pure reads (no catalog mutation) -- this module has no write-token-gated surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

SEED_COLUMNS = ("seed", "baseline_hpo_trials", "baseline_hpo_compute_budget")


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    """The composed B2-06 probe result."""

    seed_live: bool
    missing_seed_columns: tuple[str, ...]
    stats_battery_live: bool
    stats_unavailable_reason: str


def _warm_runs_columns(catalog_dir: Path) -> set[str] | None:
    """The actual column names on the warm `runs` table, or None if no warm DB exists yet
    (catalog is cool-tier only -- `bth compact` has never run)."""
    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        return None
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute("PRAGMA table_info('runs')").fetchall()
        return {row[1] for row in rows}
    finally:
        con.close()


def check_seed_live(catalog_dir: Path) -> tuple[bool, tuple[str, ...]]:
    """Whether this catalog's warm `runs` table has all of `SEED_COLUMNS`.

    Returns:
        `(True, ())` if all three B2-02 columns are present. `(False, missing)` otherwise --
        `missing` names exactly which columns are absent (including all three if no warm DB
        exists yet at all).
    """
    columns = _warm_runs_columns(catalog_dir)
    if columns is None:
        return False, SEED_COLUMNS
    missing = tuple(c for c in SEED_COLUMNS if c not in columns)
    return (len(missing) == 0, missing)


def check_stats_battery_live() -> tuple[bool, str]:
    """Whether scipy (the `bathos[stats]` extra) is importable right now.

    Returns:
        `(True, "")` if scipy imports cleanly. `(False, reason)` otherwise, naming the install
        command -- the same message `bathos.stats_gates.ScipyUnavailableError` raises, kept in
        sync by intent (both name `bathos[stats]`) rather than by importing that module's
        private `_require_scipy` helper.
    """
    try:
        import scipy.stats  # noqa: F401
    except ImportError:
        return False, "scipy is not installed. Install with: uv tool install 'bathos[stats]'"
    return True, ""


def probe_capabilities(catalog_dir: Path) -> CapabilityReport:
    """The composed B2-06 probe: `seed_live` AND `stats_battery_live`, each independently
    checked and reported (a caller may need only one, e.g. a confirmatory campaign that doesn't
    use the stats battery yet still needs `seed_live`)."""
    seed_live, missing = check_seed_live(catalog_dir)
    stats_live, reason = check_stats_battery_live()
    return CapabilityReport(
        seed_live=seed_live,
        missing_seed_columns=missing,
        stats_battery_live=stats_live,
        stats_unavailable_reason=reason,
    )


__all__ = [
    "SEED_COLUMNS",
    "CapabilityReport",
    "check_seed_live",
    "check_stats_battery_live",
    "probe_capabilities",
]
