"""Bearings in cisternal shadow mode (spec 261104 T7; plan 261111_bearings-slice3-shadow).

The load-bearing check: the binary gate features ``derive`` writes count exactly what ``ac9.pooled_metrics`` counts,
so ``cisternal shadow check`` gates the same quantities AC-9 registered. Each gate also has a negative control
(degenerate mappers are blocked; a raw verdict whose labels were never logged at work is not used)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cisternal.shadow import prove_isolation
from cisternal.shadow.mode_state import load_mode_state
from cisternal.shadow.scoring import collect_records, confusion
from cisternal.telemetry.pipeline import get_pipeline, shutdown_pipeline

from bathos.bearings import shadow as bs
from bathos.bearings.ac9 import (
    answer_key,
    degenerate_mappings,
    oracle_reading,
    pooled_metrics,
    reading_gold,
)
from bathos.bearings.reading import _existing_reading, compare_reading, record_reading
from tests.test_bearings_ac9_planted import (  # noqa: F401  (planted is a fixture)
    SIDECARS,
    _packet,
    planted,
)
from tests.test_bearings_packet import commit_files, env  # noqa: F401

MODE_STATE = Path(__file__).resolve().parents[1] / ".praxia" / "mode_state"


@pytest.fixture
def no_pipeline():
    shutdown_pipeline()
    yield
    shutdown_pipeline()


@pytest.fixture
def shadow_log(tmp_path, monkeypatch, no_pipeline):  # noqa: ARG001  (ordering: no pipeline before and after)
    log = tmp_path / "shadow_logs"  # outside the repo (tmp_path/repo)
    monkeypatch.setenv(bs.ENV_VAR, str(log))
    return log


def _records(log: Path) -> list[dict]:
    shutdown_pipeline()  # flushes
    return bs.read_records(bs._record_files(log))


# ── resolution ───────────────────────────────────────────────────────────────────────────────────────────────────


def test_resolution_layers(tmp_path, monkeypatch):
    monkeypatch.delenv(bs.ENV_VAR, raising=False)
    proj = tmp_path / "p"
    proj.mkdir()
    ucfg = tmp_path / "u.toml"
    assert bs.resolve_shadow_log_dir(start=proj, user_config=ucfg).source == "off"
    ucfg.write_text('shadow_log_dir = "/x/user"\n')
    assert bs.resolve_shadow_log_dir(start=proj, user_config=ucfg).path == Path("/x/user")
    (proj / "pyproject.toml").write_text('[tool.bathos]\nshadow_log_dir = "logs"\n')
    r = bs.resolve_shadow_log_dir(start=proj, user_config=ucfg)
    assert r.path == proj / "logs" and r.source.startswith("pyproject:")
    monkeypatch.setenv(bs.ENV_VAR, "/x/env")
    assert bs.resolve_shadow_log_dir(start=proj, user_config=ucfg).path == Path("/x/env")
    monkeypatch.setenv(bs.ENV_VAR, "none")
    r = bs.resolve_shadow_log_dir(start=proj, user_config=ucfg)
    assert r.path is None and r.source == "disabled:env"
    assert bs.resolve_shadow_log_dir("/x/arg", start=proj).path == Path("/x/arg")


def test_a_malformed_config_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.delenv(bs.ENV_VAR, raising=False)
    (tmp_path / "pyproject.toml").write_text("[tool.bathos\n")
    with pytest.raises(bs.ShadowConfigError):
        bs.resolve_shadow_log_dir(start=tmp_path, user_config=tmp_path / "none.toml")
    (tmp_path / "pyproject.toml").write_text("[tool.bathos]\nshadow_log_dir = 3\n")
    with pytest.raises(bs.ShadowConfigError):
        bs.resolve_shadow_log_dir(start=tmp_path, user_config=tmp_path / "none.toml")


@pytest.mark.usefixtures("no_pipeline")
def test_off_starts_no_pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv(bs.ENV_VAR, "none")
    assert bs.ensure_shadow_pipeline(start=tmp_path)["pipeline"] == "off"
    assert get_pipeline() is None


@pytest.mark.usefixtures("no_pipeline")
def test_a_shadow_log_inside_the_work_tree_is_refused(planted, monkeypatch):  # noqa: F811
    monkeypatch.setenv(bs.ENV_VAR, str(planted["repo"] / "logs"))
    with pytest.raises(bs.ShadowConfigError):
        bs.ensure_shadow_pipeline(start=planted["repo"])
    assert get_pipeline() is None


# ── at-work labels ───────────────────────────────────────────────────────────────────────────────────────────────


def test_a_labelled_sidecar_emits_one_at_work_label(planted, shadow_log):  # noqa: F811
    p = planted["repo"] / "scripts" / "grow.bth.toml"
    assert bs.emit_at_work_label(p, run_id="r1", catalog_dir=planted["cat"]) == "emitted"
    labels = [r for r in _records(shadow_log) if r.get("name") == "shadow.label"]
    assert len(labels) == 1
    f = labels[0]["fields"]
    import hashlib

    assert f["subject_key"] == bs.sidecar_subject(
        "pkt", "scripts/grow.bth.toml", hashlib.sha256(p.read_bytes()).hexdigest()
    )
    assert (
        f["label"] == ["alpha-aim"]
        and f["label_source"] == "at_work"
        and f["feature"] == bs.LABEL_FEATURE
    )
    assert f["charter_sha256"] and f["run_id"] == "r1"


def test_no_label_malformed_and_no_log_emit_nothing_an_unknown_slug_is_logged(  # noqa: F811
    planted,  # noqa: F811
    shadow_log,
    monkeypatch,
):
    s = planted["repo"] / "scripts"
    assert bs.emit_at_work_label(s / "plain.bth.toml", catalog_dir=planted["cat"]) == "no_label"
    (s / "bad.bth.toml").write_text("[[charter]]\npurpose = 'x'\n")
    assert (
        bs.emit_at_work_label(s / "bad.bth.toml", catalog_dir=planted["cat"])
        == "skipped:malformed_label"
    )
    # Owner 261111: log always, filter at check; an unknown slug is logged and named.
    (s / "odd.bth.toml").write_text('[charter]\npurpose = "not-a-purpose"\n')
    assert bs.emit_at_work_label(s / "odd.bth.toml", catalog_dir=planted["cat"]) == "emitted"
    (label,) = [r for r in _records(shadow_log) if r.get("name") == "shadow.label"]
    assert label["fields"]["unknown_slugs"] == ["not-a-purpose"]
    monkeypatch.setenv(bs.ENV_VAR, "none")
    assert bs.emit_at_work_label(s / "grow.bth.toml", catalog_dir=planted["cat"]) == "off"


def test_a_label_under_an_edited_charter_is_logged_with_its_state(planted, shadow_log):  # noqa: F811
    ch = planted["repo"] / ".bth" / "charter.toml"
    ch.write_text(ch.read_text() + "\n# edited\n")  # editing the file unconfirms it
    p = planted["repo"] / "scripts" / "grow.bth.toml"
    assert bs.emit_at_work_label(p, catalog_dir=planted["cat"]) == "emitted"
    (label,) = [r for r in _records(shadow_log) if r.get("name") == "shadow.label"]
    f = label["fields"]
    assert f["charter_confirmed"] is False and f["after_first_confirmation"] is True
    assert f["unknown_slugs"] == []


def test_an_emit_error_never_raises(tmp_path):
    assert bs.emit_at_work_label(tmp_path / "missing.bth.toml").startswith("skipped:error:")


# ── derive matches AC-9's pooled counts ──────────────────────────────────────────────────────────────────────────


def _setup(planted, n_readers=2, mapping_kind="oracle", reconfirm=False):  # noqa: F811
    """Labels logged at work, one packet, n readers (oracle readings with distinct reader ids), compared.

    ``reconfirm``: edit and re-confirm the charter after the labels were logged, before the comparisons."""
    for name, lbl, _ in SIDECARS:
        if lbl:
            assert (
                bs.emit_at_work_label(
                    planted["repo"] / "scripts" / f"{name}.bth.toml", catalog_dir=planted["cat"]
                )
                == "emitted"
            )
    if reconfirm:
        from bathos.charter import confirm
        from tests.test_bearings_packet import TOKEN

        ch = planted["repo"] / ".bth" / "charter.toml"
        ch.write_text(ch.read_text() + "\n# wording fixed\n")
        confirm(
            planted["repo"], chair_token=TOKEN, interactive_tty=False, catalog_dir=planted["cat"]
        )
    out, sha = _packet(planted)
    key = answer_key(planted["repo"], out, planted["charter"])
    from bathos.bearings.shadow import _confirmed_charter

    charter_sha, _ = _confirmed_charter(planted["repo"], planted["cat"])
    readings = []
    for i in range(n_readers):
        reading = oracle_reading(out, key["labels"], sha) | {"reader_id": f"reader-{i}"}
        path = planted["tmp"] / f"reading{i}.json"
        path.write_text(json.dumps(reading))
        rid = record_reading(
            planted["repo"], reading_file=path, packet_dir=out, catalog_dir=planted["cat"]
        )["reading_id"]
        stored = _existing_reading(planted["cat"], "pkt", rid)
        gold = reading_gold(out, stored, key["labels"])
        if mapping_kind == "oracle":
            mapping = {
                rp: next((s for s in g if s != "off-charter"), None) for rp, g in gold.items()
            }
            if (
                i == 1
            ):  # a second reader that over-maps the off-charter work: off-charter misses and agreement misses
                mapping = {rp: (m or "alpha-aim") for rp, m in mapping.items()}
        else:
            mapping = degenerate_mappings(sorted(gold), planted["charter"])[mapping_kind]
        mp = planted["tmp"] / f"map{i}.json"
        mp.write_text(json.dumps({"mapper_id": "mapper", "mapping": mapping}))
        cmp = compare_reading(
            planted["repo"], reading_id=rid, mapping_file=mp, catalog_dir=planted["cat"]
        )
        payload = bs.comparison_payload(
            planted["repo"],
            out,
            stored,
            cmp,
            planted["charter"],
            charter_sha256=charter_sha,
            project="pkt",
        )
        assert bs.emit_comparison(payload, arm="readers", mapper_version="m1")
        readings.append(
            {
                "gold": gold,
                "maybe": reading_gold(out, stored, key["ambiguous_labels"]),
                "comparison": cmp,
            }
        )
    return pooled_metrics([{"key": key, "readings": readings}])


def _conf(derived, feat):
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        paths = bs.write_derived(derived, d)
        v, lab = paths[feat]
        col = collect_records(feat, [v, lab])
        return confusion(col.verdicts, col.labels, True)


def test_derived_counts_equal_ac9_pooled_counts(planted, shadow_log):  # noqa: F811
    m = _setup(planted)
    d = bs.derive(_records(shadow_log), project="pkt")
    assert d["skipped"] == [] and len(d["used"]) == 2
    s = _conf(d, "bearings.readers.silent")
    assert s.tp + s.fn == m["n_silent"] and s.tp == round(m["silent_recall"] * m["n_silent"])
    assert s.fp + s.tn == m["n_present"] and s.fp == round(m["false_silent"] * m["n_present"])
    o = _conf(d, "bearings.readers.off_charter")
    assert o.tp + o.fn == m["n_off_charter"] and o.tp == round(
        m["off_charter_unmapped"] * m["n_off_charter"]
    )
    a = _conf(d, "bearings.readers.agreement")
    assert a.tp + a.fp == m["n_mapped"] and a.tp == round(m["gold_agreement"] * m["n_mapped"])
    assert (
        m["off_charter_unmapped"] == 0.5 and m["gold_agreement"] < 1.0
    )  # the second reader's misses are counted


def test_a_raw_verdict_without_logged_at_work_labels_is_not_used(planted, shadow_log):  # noqa: F811
    _setup(planted, n_readers=1)
    recs = [r for r in _records(shadow_log) if r.get("name") != "shadow.label"]
    d = bs.derive(recs, project="pkt")
    assert d["used"] == [] and d["skipped"][0]["reason"].startswith("labels_not_logged_at_work")
    assert all(not v["verdicts"] for v in d["features"].values())


def test_retrospective_labels_do_not_count(planted, shadow_log):  # noqa: F811
    _setup(planted, n_readers=1)
    recs = _records(shadow_log)
    for r in recs:
        if r.get("name") == "shadow.label":
            r["fields"]["label_source"] = "retrospective"
    assert bs.derive(recs, project="pkt")["used"] == []


def test_labels_survive_a_charter_reconfirm(planted, shadow_log):  # noqa: F811
    """Owner 261111: a re-confirm (here a wording edit) must not erase labels logged under the earlier charter, while
    the verdicts themselves are pinned to the new charter's sha."""
    m = _setup(planted, reconfirm=True)
    recs = _records(shadow_log)
    label_shas = {r["fields"]["charter_sha256"] for r in recs if r.get("name") == "shadow.label"}
    d = bs.derive(recs, project="pkt")
    assert d["skipped"] == [] and len(d["used"]) == 2
    (fv,) = d["feature_version"].values()
    assert not any(fv.endswith(s[:16]) for s in label_shas)  # verdicts are under the new charter
    s = _conf(d, "bearings.readers.silent")
    assert s.tp + s.fn == m["n_silent"]


def test_labels_written_before_the_first_confirmation_do_not_count(planted, shadow_log):  # noqa: F811
    _setup(planted, n_readers=1)
    recs = _records(shadow_log)
    for r in recs:
        if r.get("name") == "shadow.label":
            r["fields"]["after_first_confirmation"] = False
    d = bs.derive(recs, project="pkt")
    assert d["used"] == [] and d["skipped"][0]["reason"].startswith("labels_not_logged_at_work")


# ── mode-state and the gate ──────────────────────────────────────────────────────────────────────────────────────


def test_every_gate_feature_has_a_valid_mode_state_in_shadow():
    for feat in bs.GATE_FEATURES:
        data = load_mode_state(MODE_STATE / f"{feat}.toml")
        assert data["feature"]["name"] == feat
        assert data["ladder"]["current_stage"] == "shadow"
        assert data["promotion"]["target_stage"] == "advisory"
        assert "retrospective" not in data["promotion"]["allowed_label_sources"]


@pytest.mark.parametrize(
    "kind", ["all_unmapped", "all_to_first_live", "each_to_every_purpose", "first_only"]
)
def test_degenerate_mappers_are_blocked(planted, shadow_log, kind):  # noqa: F811
    _setup(planted, n_readers=1, mapping_kind=kind)
    res = bs.shadow_check(
        records_dir=shadow_log,
        mode_state_dir=MODE_STATE,
        out_dir=planted["tmp"] / "derived",
        project="pkt",
    )
    readers = {k: v for k, v in res["features"].items() if k.startswith("bearings.readers.")}
    assert readers and not any(v.get("promotion_permitted") for v in readers.values())


@pytest.mark.usefixtures("no_pipeline")
def test_shadow_check_with_no_records_refuses(tmp_path):
    (tmp_path / "logs").mkdir()
    res = bs.shadow_check(
        records_dir=tmp_path / "logs", mode_state_dir=MODE_STATE, out_dir=tmp_path / "d"
    )
    assert all("refused" in v for v in res["features"].values())


# ── isolation (the enforcer the mode-state files name) ───────────────────────────────────────────────────────────


def test_shadow_reader_isolation(planted, shadow_log):  # noqa: F811
    """The shadow arm (labels at work, comparison payload and raw verdicts) writes only to the shadow log and leaves
    the live arm's output (the packet and the recorded comparison) unchanged."""
    out, sha = _packet(planted)
    key = answer_key(planted["repo"], out, planted["charter"])
    reading = oracle_reading(out, key["labels"], sha)
    rpath = planted["tmp"] / "reading.json"
    rpath.write_text(json.dumps(reading))
    rid = record_reading(
        planted["repo"], reading_file=rpath, packet_dir=out, catalog_dir=planted["cat"]
    )["reading_id"]
    stored = _existing_reading(planted["cat"], "pkt", rid)
    mp = planted["tmp"] / "map.json"
    mp.write_text(
        json.dumps({"mapper_id": "mapper", "mapping": {p["id"]: None for p in stored["purposes"]}})
    )
    live_out = planted["tmp"] / "live_out"
    live_out.mkdir()
    # The comparison itself (with its timestamped ledger event) is the live path's own work, done once; the live arm
    # here is its deterministic output, so any difference or stray write comes from the shadow additions.
    cmp = compare_reading(
        planted["repo"], reading_id=rid, mapping_file=mp, catalog_dir=planted["cat"]
    )

    def live():
        (live_out / "cmp.json").write_text(json.dumps(cmp, sort_keys=True, default=str))

    def shadow():
        live()
        for name, lbl, _ in SIDECARS:
            if lbl:
                bs.emit_at_work_label(
                    planted["repo"] / "scripts" / f"{name}.bth.toml", catalog_dir=planted["cat"]
                )
        from bathos.bearings.shadow import _confirmed_charter

        payload = bs.comparison_payload(
            planted["repo"],
            out,
            stored,
            cmp,
            planted["charter"],
            charter_sha256=_confirmed_charter(planted["repo"], planted["cat"])[0],
            project="pkt",
        )
        bs.emit_comparison(payload, arm="readers", mapper_version="m1")

    bs.ensure_shadow_pipeline(start=planted["repo"])
    enforced = "tests/test_bearings_shadow.py::test_shadow_reader_isolation"
    for feat in bs.GATE_FEATURES:
        res = prove_isolation(
            live,
            shadow,
            live_roots=[live_out, planted["cat"]],
            shadow_log_dir=shadow_log,
            check_roots=[planted["repo"], out],
            feature=feat,
            enforced_by=enforced,
        )
        assert res.passed, res


# ── a real `bth run` (subprocess): the label survives process exit, and the hook never changes the run ──────────


def _bth_run(repo: Path, script: str) -> int:
    import subprocess
    import sys

    bth = Path(sys.executable).with_name("bth")
    return subprocess.run(
        [str(bth), "run", script], cwd=repo, capture_output=True, text=True, timeout=300
    ).returncode


def test_a_real_bth_run_logs_one_at_work_label(planted, shadow_log):  # noqa: F811
    commit_files(planted["repo"], {"scripts/grow.py": "print('ok')\n"}, msg="script")
    assert _bth_run(planted["repo"], "scripts/grow.py") == 0
    labels = [
        r for r in bs.read_records(bs._record_files(shadow_log)) if r.get("name") == "shadow.label"
    ]
    assert len(labels) == 1 and labels[0]["fields"]["label"] == ["alpha-aim"]
    assert labels[0]["fields"]["run_id"]


def test_a_failing_shadow_hook_leaves_the_run_unchanged(planted, monkeypatch, no_pipeline):  # noqa: F811, ARG001
    commit_files(planted["repo"], {"scripts/grow.py": "print('ok')\n"}, msg="script")
    monkeypatch.setenv(bs.ENV_VAR, str(planted["repo"] / "inside"))  # refused: inside the work tree
    assert _bth_run(planted["repo"], "scripts/grow.py") == 0
    assert not (planted["repo"] / "inside").exists()
    commit_files(
        planted["repo"], {"scripts/fail.py": "raise SystemExit(3)\n"}, msg="failing script"
    )
    assert (
        _bth_run(planted["repo"], "scripts/fail.py") == 3
    )  # the script's own exit code, not the hook's
