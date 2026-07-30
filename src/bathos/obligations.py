"""Post-mortem obligation ledger (build-order step 4).

Post-mortems were an isolated island: `postmortem.py` validated well, but nothing ever
*required* one, every field defaulted to an inert value, and `campaigns.py` never mentioned
them. The result was a system that captured intent (pre-registration is the most heavily gated
surface in the tool) and learning not at all.

An obligation is the missing link. It opens automatically from events bathos already computes,
and is discharged by writing a valid post-mortem that names it. Per decision **D1** it never
blocks: it downgrades a campaign verdict at conclude and warns at submit. Nothing to bypass
means the bypass metrics stay honest, and the fragile script-stem key never sits behind
anything binding.

Ledger layout — `.bth/obligations/<kind>_<entity_id>.json`, one file per (entity, trigger).
The `kind` prefix is deliberate: §10's open item 3 noted that a flat `<run_or_campaign_id>.json`
conflates two ID namespaces with no discriminator. Prefixing resolves that and lets a run and a
campaign share an id without collision.

Spec: `.praxia/docs/specs/260729_post-mortem-obligations-targeted-review-schema-and-a-shipped-rule-card-corpus.md` §5
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

#: Why an obligation opened. Kept as an explicit closed set so a reader can tell a
#: contradicted-citation obligation (the highest-value one) from a routine failure.
TRIGGERS = frozenset(
    {
        "outcome_failed",  # (1) a run's computed outcome is non-pass
        "campaign_confounded",  # (2) a campaign concluded confounded / was downgraded
        "adversarial_check_fired",  # (3)
        "citation_contradicted",  # (4) a [review] 'supports' entry the outcome contradicted
    }
)

ENTITY_KINDS = frozenset({"run", "campaign"})


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(ob), indent=2) + "\n", encoding="utf-8")
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
        path.write_text(json.dumps(asdict(ob), indent=2) + "\n", encoding="utf-8")
    return ob


def discharge_from_postmortem(workspace_root: Path | str, postmortem_path: Path | str) -> list[str]:
    """Discharge every obligation a post-mortem names in `discharges`.

    Only a *valid* post-mortem discharges: validity is the existing
    hypothesis_status/verdict_override consistency check, so no new validation tier is
    introduced. An invalid post-mortem discharges nothing.
    """
    from bathos.postmortem import parse_postmortem, validate_postmortem

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
    ages = sorted((o.age_days() for o in obs), reverse=True)
    by_trigger: dict[str, int] = {}
    for o in obs:
        by_trigger[o.trigger] = by_trigger.get(o.trigger, 0) + 1
    return {
        "signal": "open_obligation_age",
        "open_count": len(obs),
        "max_age_days": round(ages[0], 2) if ages else 0.0,
        "median_age_days": round(ages[len(ages) // 2], 2) if ages else 0.0,
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
