"""Randomized operation-sequence generator for the AC-17 differential test.

`generate_ops(seed)` returns a plain list of op dicts (the abstract
sequence). Both backends execute the SAME list, in the SAME order, via
`ac17_harness.execute_ops` -- see that module and
`test_ac17_differential.py`'s module docstring for how ids/timestamps are
kept deterministic across the two executions.
"""

from __future__ import annotations

import hashlib
import random
from datetime import timedelta
from typing import Any

from .ac17_harness import BASE_TS, sidecar_toml


def generate_ops(seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    ops: list[dict[str, Any]] = []

    tick = [0]

    def next_ts():
        tick[0] += 1
        return BASE_TS + timedelta(seconds=tick[0])

    n_campaigns = rng.randint(1, 3)
    campaign_handles: list[str] = []
    campaign_mode: dict[str, str] = {}
    campaign_popper: dict[str, tuple[float, float, float]] = {}

    for i in range(n_campaigns):
        handle = f"camp-{i}"
        mode = "sequential" if rng.random() < 0.6 else "exploration"
        campaign_handles.append(handle)
        campaign_mode[handle] = mode
        if mode == "sequential":
            campaign_popper[handle] = (0.1, 0.9, round(rng.uniform(0.01, 0.3), 3))
        ops.append({"kind": "create_campaign", "handle": handle, "name": f"campaign {i}", "mode": mode})

    n_runs = rng.randint(4, 7)
    run_ids: list[str] = []
    # Current resolved campaign per run (updated on reassignment), used to
    # pick edge/anchor targets and to avoid reassigning into the SAME campaign.
    run_campaign: dict[str, str | None] = {}
    finished_run_ids: list[str] = []
    # AC-17 finding (classification b, legacy quirk -- NOT fixed here, see
    # ac17_harness's reassignment-guard comment for the sibling case):
    # `conclude_campaign`'s own `link_cool_runs_to_campaigns(...,
    # campaign_id=full_id)` call RAISES CampaignError on a threshold
    # mismatch, unlike compact()'s bulk (unscoped) pass, which only
    # logs+skips. The deliberate threshold-deviation below (to exercise
    # AC-22's mismatch-skip path) is real and valuable, but it must never
    # be paired with a `conclude` op on the SAME campaign, or the legacy
    # side crashes instead of producing a comparable canonical state.
    # Tracked here so the conclude-generation loop can skip such campaigns.
    campaign_has_deviation: dict[str, bool] = {}

    for i in range(n_runs):
        run_id = f"run-{i}"
        run_ids.append(run_id)

        campaign_handle = None
        if campaign_handles and rng.random() < 0.75:
            campaign_handle = rng.choice(campaign_handles)

        has_sidecar = rng.random() < 0.85
        sidecar_text = None
        popper = None
        if has_sidecar:
            if campaign_handle and campaign_mode[campaign_handle] == "sequential":
                null, alt, threshold = campaign_popper[campaign_handle]
                if rng.random() < 0.1:
                    # Deliberate deviation: exercises the threshold-mismatch
                    # skip path (AC-22), which both sides must resolve
                    # identically -- not a BC, a shared-logic assertion.
                    threshold = round(threshold + 0.5, 3)
                    campaign_has_deviation[campaign_handle] = True
            else:
                null, alt, threshold = 0.2, 0.8, round(rng.uniform(0.01, 0.3), 3)
            sidecar_text = sidecar_toml(null, alt, threshold)
            popper = (null, alt, threshold)

        ops.append(
            {
                "kind": "start_run",
                "run_id": run_id,
                "ts": next_ts(),
                "campaign_handle": campaign_handle,
                "sidecar": sidecar_text,
                # (null, alt, threshold) as generated -- kept alongside the
                # rendered TOML so the differential test can independently
                # compute the e-value BC-7 exemption (which run/campaign
                # pairs a postmortem override is expected to change the
                # e-value bucket for) without re-parsing the sidecar file.
                "popper": popper,
            }
        )
        run_campaign[run_id] = campaign_handle

        branch = rng.random()
        if branch < 0.18:
            ops.append({"kind": "reap", "run_id": run_id, "ts": next_ts()})
            if rng.random() < 0.5:
                ops.append({"kind": "revert_reap", "run_id": run_id, "ts": next_ts()})
                if rng.random() < 0.6:
                    outcome, status, exit_code = _pick_outcome(rng)
                    ops.append(
                        {
                            "kind": "finish_run",
                            "run_id": run_id,
                            "ts": next_ts(),
                            "outcome": outcome,
                            "status": status,
                            "exit_code": exit_code,
                        }
                    )
                    finished_run_ids.append(run_id)
        else:
            outcome, status, exit_code = _pick_outcome(rng)
            ops.append(
                {
                    "kind": "finish_run",
                    "run_id": run_id,
                    "ts": next_ts(),
                    "outcome": outcome,
                    "status": status,
                    "exit_code": exit_code,
                }
            )
            finished_run_ids.append(run_id)

            if campaign_handle and len(campaign_handles) > 1 and rng.random() < 0.25:
                # AC-17 finding (classification b, legacy quirk -- NOT
                # fixed here): reassigning a run AWAY from a sequential
                # campaign whose threshold it already locked leaves that
                # threshold PERMANENTLY stuck at the departed run's value
                # (nothing ever resets `campaigns.stopping_threshold` on
                # membership change) -- a LATER member with a genuinely
                # different threshold, or `conclude_campaign`'s own
                # `link_cool_runs_to_campaigns(..., campaign_id=full_id)`
                # call (which RAISES CampaignError on mismatch instead of
                # compact()'s bulk pass, which only logs+skips), then
                # crashes with no soft-skip available. This is a real,
                # user-facing legacy behaviour (a "conclude" can start
                # raising after an unrelated reassignment), but it means
                # there is no canonical legacy state to compute at all once
                # triggered -- not something AC-17's row-level comparison
                # can characterize. Avoided here by never reassigning a run
                # AWAY from a sequential campaign (regardless of the
                # target's own mode); reassigning INTO one, or between two
                # non-sequential campaigns, is unaffected and still
                # exercised.
                candidates = (
                    [h for h in campaign_handles if h != campaign_handle]
                    if campaign_mode[campaign_handle] != "sequential"
                    else []
                )
                if candidates:
                    other = rng.choice(candidates)
                    ops.append(
                        {
                            "kind": "add_run_to_campaign",
                            "campaign_handle": other,
                            "run_id": run_id,
                            "prev_campaign_handle": campaign_handle,
                        }
                    )
                    run_campaign[run_id] = other

            if rng.random() < 0.35:
                hyp = rng.choice(["held", "refuted", "inconclusive"])
                if hyp == "refuted":
                    verdict = rng.choice(["fail", "none"])
                elif hyp == "held":
                    verdict = rng.choice(["pass", "none"])
                else:
                    verdict = rng.choice(["none", "marginal"])
                ops.append(
                    {
                        "kind": "postmortem",
                        "run_id": run_id,
                        "hypothesis_status": hyp,
                        "verdict_override": verdict,
                        "author": "ac17",
                    }
                )

    for i in range(1, len(campaign_handles)):
        if rng.random() < 0.5:
            parent = campaign_handles[rng.randrange(i)]
            ops.append({"kind": "add_campaign_edge", "child": campaign_handles[i], "parent": parent})

    for i in range(1, len(run_ids)):
        if rng.random() < 0.3:
            parent = run_ids[rng.randrange(i)]
            ops.append({"kind": "add_run_edge", "child": run_ids[i], "parent": parent})

    for k in range(rng.randint(0, 3)):
        campaign_handle = rng.choice([None, *campaign_handles]) if campaign_handles else None
        ops.append(
            {
                "kind": "anchor_insert",
                "path": f"figs/{k}.svg",
                "sha256": hashlib.sha256(f"content-{seed}-{k}".encode()).hexdigest(),
                "anchor_kind": "figure",
                "label": f"fig{k}",
                "campaign_handle": campaign_handle,
                "ts": next_ts(),
            }
        )

    for handle in campaign_handles:
        if campaign_has_deviation.get(handle):
            continue
        if rng.random() < 0.7:
            outcome_label = rng.choice(["pass", "fail", "confirmed", "refuted", "exploratory"])
            ops.append(
                {
                    "kind": "conclude",
                    "handle": handle,
                    "outcome_label": outcome_label,
                    "conclusion": f"concluding {handle}",
                }
            )

    return ops


def _pick_outcome(rng: random.Random) -> tuple[str, str, int]:
    choice = rng.choice(
        [
            ("pass", "completed", 0),
            ("fail", "completed", 0),
            ("marginal", "completed", 0),
            ("error", "completed", 1),
            ("unknown", "completed", 0),
            ("fail", "failed", 1),
        ]
    )
    return choice
