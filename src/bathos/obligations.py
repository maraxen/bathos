"""Post-mortem obligation ledger (build-order step 4).

Post-mortems were an isolated island: `postmortem.py` validated well, but nothing ever
*required* one, every field defaulted to an inert value, and `campaigns.py` never mentioned
them. The result was a system that captured intent (pre-registration is the most heavily gated
surface in the tool) and learning not at all.

An obligation is the missing link. It is discharged by writing a valid post-mortem that names
it. Per decision **D1** the design never blocks: an obligation downgrades a campaign verdict at
conclude and warns at submit. Nothing to bypass means the bypass metrics stay honest, and the
fragile script-stem key never sits behind anything binding.

**What is wired, and what is not — read this before relying on it.** All four §5 triggers
have live call sites, each behind its **own** opt-in environment flag, and every flag
defaults OFF (see `WIRED_TRIGGERS` and `_FLAG_PREFIX`):

- `outcome_failed` and `adversarial_check_fired` — `runner.run_script`, at run end.
- `campaign_confounded` and `citation_contradicted` — `campaigns.conclude_campaign`.

With every flag unset — the default — nothing calls `open_obligation` automatically, so the
ledger stays empty and no verdict or exit code changes anywhere. That is the same posture the
Review Coverage Gate ships in, and for the same reason: auto-opening obligations changes
behaviour on every run, and enabling it wholesale would retroactively burden every existing
script, which is the failure §7's sequencing constraint exists to prevent.

Downgrading is gated separately from opening (`ENFORCE_FLAG`). `bth campaign conclude` lists
open obligations and warns; it downgrades the verdict to `confounded` only under
`BTH_OBLIGATION_ENFORCE=1`. `bth submit` warns and never blocks, per D1.

Ledger layout — `.bth/obligations/<kind>_<entity_id>.json`, one file per (entity, trigger).
The `kind` prefix is deliberate: §10's open item 3 noted that a flat `<run_or_campaign_id>.json`
conflates two ID namespaces with no discriminator. Prefixing resolves that and lets a run and a
campaign share an id without collision.

Spec: `.praxia/docs/specs/260729_post-mortem-obligations-targeted-review-schema-and-a-shipped-rule-card-corpus.md` §5
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

#: Why an obligation opened. Kept as an explicit closed set so a reader can tell a
#: contradicted-citation obligation (the highest-value one) from a routine failure.
TRIGGERS = frozenset(
    {
        "outcome_failed",  # (1) a run's computed outcome is non-pass
        "campaign_confounded",  # (2) a campaign concluded confounded / was downgraded
        "adversarial_check_fired",  # (3) the selected branch's stricter bar evaluated FALSE
        "citation_contradicted",  # (4) a [review] 'supports' entry the outcome contradicted
    }
)

#: Triggers with a live call site — now all four.
#:
#: Trigger 3's **polarity is settled**: `adversarial_check` is a *stricter conjunct* the
#: outcome must also clear, so it FIRES when it evaluates FALSE. See
#: `sidecar.evaluate_adversarial_check` for the evidence (the D3 ADR's definition, the spec's
#: strengthened-conjunct example, and the distinct-column lint heuristic all agree). The
#: inverse reading — a refuter that fires when true — was rejected: it cannot account for the
#: spec's example, which restates the pass condition and adds requirements to it.
WIRED_TRIGGERS = frozenset(TRIGGERS)

ENTITY_KINDS = frozenset({"run", "campaign"})

#: Per-trigger opt-in: `BTH_OBLIGATION_<TRIGGER>`, e.g. BTH_OBLIGATION_CITATION_CONTRADICTED=1.
#: Independently toggleable rather than one master switch, because the four differ sharply in
#: blast radius. `citation_contradicted` fires only where a [review] entry already exists, so
#: it can touch nothing authored before step 2. `outcome_failed` fires on any non-pass run and
#: is the widest — enabling it against an existing catalog opens an obligation per historical
#: failure. Bundling them would force the safest trigger to wait on the riskiest.
_FLAG_PREFIX = "BTH_OBLIGATION_"

#: Conclude-time downgrade, separate from the opening flags above. Mirrors
#: BTH_REVIEW_COVERAGE_ENFORCE for the same reason it exists: opening obligations is
#: observation, downgrading a verdict is enforcement, and §7 sequences enforcement behind
#: having observed real data. Off => conclude lists open obligations and warns, changing
#: no verdict.
ENFORCE_FLAG = "BTH_OBLIGATION_ENFORCE"


_TRUE = ("1", "true", "yes")
_FALSE = ("0", "false", "no")


def _env_override(name: str) -> bool | None:
    """Tri-state read of an env flag: True / False / None (unset or unrecognised).

    A set-but-false value must be able to turn a config-enabled flag OFF, so this cannot
    collapse to a plain truthiness test — otherwise `BTH_OBLIGATION_X=0` would silently mean
    "fall through to config", i.e. still enabled.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return None


def _config_flag(workspace_root: Path | str | None, key: str) -> bool:
    """Read `[obligations] <key>` from the project's .bth.toml. Missing/unreadable → False."""
    from bathos.config import find_project_config, load_project_config

    try:
        start = Path(workspace_root) if workspace_root else None
        cfg_path = find_project_config(start)
        if cfg_path is None:
            return False
        return bool(load_project_config(cfg_path).obligations.get(key, False))
    except Exception:
        # A malformed .bth.toml must not crash a run; an unreadable config means "not enabled",
        # which is the safe direction for a flag that opens ledger entries.
        return False


def trigger_enabled(trigger: str, workspace_root: Path | str | None = None) -> bool:
    """Is this trigger enabled? Unknown triggers are never enabled.

    Resolution order: `BTH_OBLIGATION_<TRIGGER>` env var wins in **both** directions, then
    `[obligations] <trigger>` in `.bth.toml`, then off.

    The config file is the durable home: a SLURM job reads the same `.bth.toml`, whereas a
    shell-only export is honoured locally and silently skipped on the cluster — which would
    produce a ledger where identical work does or does not open obligations depending on where
    it ran. The env var stays as the per-invocation override.
    """
    if trigger not in TRIGGERS:
        return False
    override = _env_override(f"{_FLAG_PREFIX}{trigger.upper()}")
    if override is not None:
        return override
    return _config_flag(workspace_root, trigger)


def enforcement_enabled(workspace_root: Path | str | None = None) -> bool:
    """Should open obligations downgrade a campaign verdict at conclude?

    Same resolution order as :func:`trigger_enabled`, via `[obligations] enforce`.
    """
    override = _env_override(ENFORCE_FLAG)
    if override is not None:
        return override
    return _config_flag(workspace_root, "enforce")


def maybe_open(
    workspace_root: Path | str,
    entity_kind: str,
    entity_id: str,
    trigger: str,
    detail: str = "",
) -> Obligation | None:
    """`open_obligation` gated on the trigger's opt-in flag. Returns None when disabled.

    Every automatic call site goes through this rather than `open_obligation` directly, so
    "does this trigger write to the ledger today?" has exactly one answer per trigger and it
    is visible in the environment. `open_obligation` stays ungated for explicit/manual use.
    """
    if not trigger_enabled(trigger, workspace_root):
        return None
    return open_obligation(workspace_root, entity_kind, entity_id, trigger, detail)


@dataclass
class Obligation:
    """One open obligation to explain something."""

    obligation_id: str
    entity_kind: str  # "run" | "campaign"
    entity_id: str
    trigger: str
    detail: str = ""
    opened_at: str = ""
    discharged_at: str = ""
    discharged_by: str = ""  # path to the postmortem that discharged it
    notes: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return not self.discharged_at

    def age_days(self, now: datetime | None = None) -> float:
        """Days since opening. Reported, never thresholded — see `signal_open_obligation_age`."""
        if not self.opened_at:
            return 0.0
        try:
            opened = datetime.fromisoformat(self.opened_at)
        except ValueError:
            return 0.0
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        return ((now or datetime.now(UTC)) - opened).total_seconds() / 86400.0


def ledger_dir(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / ".bth" / "obligations"


def _median(sorted_ages: list[float]) -> float:
    """True median of an ascending list."""
    n = len(sorted_ages)
    mid = n // 2
    return sorted_ages[mid] if n % 2 else (sorted_ages[mid - 1] + sorted_ages[mid]) / 2


def _atomic_write(path: Path, payload: dict) -> None:
    """Write-then-rename, matching catalog.py's persistence convention.

    A torn write would leave malformed JSON that `_read` swallows as None, which would make
    `open_obligation` re-open an already-old obligation with a fresh timestamp — silently
    breaking the "never resets the age" guarantee this module advertises.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.rename(path)  # atomic on POSIX


def _obligation_id(entity_kind: str, entity_id: str, trigger: str) -> str:
    return f"{entity_kind}_{entity_id}_{trigger}"


def open_obligation(
    workspace_root: Path | str,
    entity_kind: str,
    entity_id: str,
    trigger: str,
    detail: str = "",
) -> Obligation:
    """Open an obligation, or return the existing one unchanged.

    Idempotent by (entity_kind, entity_id, trigger): re-running a failing experiment must not
    accumulate duplicate obligations for the same reason, and re-opening must not reset the
    age of one that has been outstanding for weeks.
    """
    if entity_kind not in ENTITY_KINDS:
        raise ValueError(f"entity_kind must be one of {sorted(ENTITY_KINDS)}, got {entity_kind!r}")
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {sorted(TRIGGERS)}, got {trigger!r}")

    oid = _obligation_id(entity_kind, entity_id, trigger)
    path = ledger_dir(workspace_root) / f"{oid}.json"
    existing = _read(path)
    if existing is not None:
        return existing

    ob = Obligation(
        obligation_id=oid,
        entity_kind=entity_kind,
        entity_id=entity_id,
        trigger=trigger,
        detail=detail,
        opened_at=datetime.now(UTC).isoformat(),
    )
    _atomic_write(path, asdict(ob))
    return ob


def _read(path: Path) -> Obligation | None:
    if not path.exists():
        return None
    try:
        return Obligation(**json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, OSError):
        return None


def list_obligations(
    workspace_root: Path | str, entity_id: str | None = None, open_only: bool = True
) -> list[Obligation]:
    """List obligations, newest first. A corrupt ledger file is skipped, never fatal."""
    d = ledger_dir(workspace_root)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        ob = _read(p)
        if ob is None:
            continue
        if entity_id and ob.entity_id != entity_id:
            continue
        if open_only and not ob.is_open:
            continue
        out.append(ob)
    return sorted(out, key=lambda o: o.opened_at, reverse=True)


def discharge(workspace_root: Path | str, obligation_id: str, by_path: str) -> Obligation | None:
    """Mark an obligation discharged by a post-mortem. Returns None if unknown."""
    path = ledger_dir(workspace_root) / f"{obligation_id}.json"
    ob = _read(path)
    if ob is None:
        return None
    if ob.is_open:
        ob.discharged_at = datetime.now(UTC).isoformat()
        ob.discharged_by = str(by_path)
        _atomic_write(path, asdict(ob))
    return ob


def discharge_from_postmortem(workspace_root: Path | str, postmortem_path: Path | str) -> list[str]:
    """Discharge every obligation a post-mortem names in `discharges`.

    Only a *valid* post-mortem discharges: validity is the existing
    hypothesis_status/verdict_override consistency check, so no new validation tier is
    introduced. An invalid post-mortem discharges nothing.
    """
    from bathos.postmortem import parse_postmortem, validate_postmortem

    # validate_postmortem calls workspace_root.resolve() when asset_links are present;
    # a plain str (which this signature accepts) would AttributeError there.
    workspace_root = Path(workspace_root)
    p = Path(postmortem_path)
    pm = parse_postmortem(p)
    result = validate_postmortem(pm, workspace_root=workspace_root)
    if not getattr(result, "ok", False):
        return []

    discharged = []
    for oid in getattr(pm, "discharges", []) or []:
        if discharge(workspace_root, oid, str(p)) is not None:
            discharged.append(oid)
    return discharged


def list_obligations_for_scope(
    workspace_root: Path | str, entity_ids: set[str] | list[str]
) -> list[Obligation]:
    """Open obligations for any of `entity_ids`, newest first.

    The conclude-time gate binds on a campaign *and its member runs* — §5's "open obligations
    on member runs downgrade the campaign verdict" — which `list_obligations` cannot express
    with its single `entity_id` filter.
    """
    wanted = set(entity_ids)
    return [o for o in list_obligations(workspace_root, open_only=True) if o.entity_id in wanted]


def signal_open_obligation_age(workspace_root: Path | str) -> dict:
    """Signal 11: reports open obligations and their ages. **No threshold.**

    Deliberately reporting-only. §7 sequences the enforcing parts of this design behind
    observing real data; an "obligations older than N days" alarm would be exactly the guessed
    constant that sequencing exists to prevent. Report the distribution and let a human — or a
    later, calibrated rule — decide what is too old.

    Signal *11* rather than 14: sprint_audit's numbered markers run 1, 2, 4-10, 12, 13, so 11
    is an actual gap rather than an extension.
    """
    obs = list_obligations(workspace_root, open_only=True)
    # ASCENDING with a true even/odd median. A descending sort indexed at n//2 returns the
    # younger side of an even-sized ledger, understating how stale the backlog is — the
    # one thing this signal exists to report honestly.
    ages = sorted(o.age_days() for o in obs)
    by_trigger: dict[str, int] = {}
    for o in obs:
        by_trigger[o.trigger] = by_trigger.get(o.trigger, 0) + 1
    return {
        "signal": "open_obligation_age",
        "open_count": len(obs),
        "max_age_days": round(ages[-1], 2) if ages else 0.0,
        "median_age_days": round(_median(ages), 2) if ages else 0.0,
        "by_trigger": by_trigger,
        "oldest": [
            {
                "obligation_id": o.obligation_id,
                "trigger": o.trigger,
                "age_days": round(o.age_days(), 2),
            }
            for o in obs[-3:]
        ],
    }
