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
    cat = tmp_path / "cat"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat))
    proj = tmp_path / "p"
    proj.mkdir()
    ucfg = tmp_path / "u.toml"
    # Nothing configured: the code default, derived from the catalog root (owner 261111).
    r = bs.resolve_shadow_log_dir(start=proj, user_config=ucfg)
    assert r.path == cat / "shadow" and r.source == f"default:{cat}"
    ucfg.write_text('[bearings]\nshadow_log_dir = "/x/user"\n')
    assert bs.resolve_shadow_log_dir(start=proj, user_config=ucfg).path == Path("/x/user")
    (proj / "pyproject.toml").write_text('[tool.bathos.bearings]\nshadow_log_dir = "logs"\n')
    r = bs.resolve_shadow_log_dir(start=proj, user_config=ucfg)
    assert r.path == proj / "logs" and r.source.startswith("pyproject:")
    (proj / ".bth.toml").write_text(
        '[bearings]\nshadow_log_dir = "/x/bth"\n'
    )  # what SLURM jobs read
    r = bs.resolve_shadow_log_dir(start=proj, user_config=ucfg)
    assert r.path == Path("/x/bth") and r.source.startswith("bth_toml:")
    monkeypatch.setenv(bs.ENV_VAR, "/x/env")
    assert bs.resolve_shadow_log_dir(start=proj, user_config=ucfg).path == Path("/x/env")
    monkeypatch.setenv(bs.ENV_VAR, "none")
    r = bs.resolve_shadow_log_dir(start=proj, user_config=ucfg)
    assert r.path is None and r.source == "disabled:env"
    assert bs.resolve_shadow_log_dir("/x/arg", start=proj).path == Path("/x/arg")


def test_none_in_a_config_file_disables(tmp_path, monkeypatch):
    monkeypatch.delenv(bs.ENV_VAR, raising=False)
    (tmp_path / ".bth.toml").write_text('[bearings]\nshadow_log_dir = "none"\n')
    r = bs.resolve_shadow_log_dir(start=tmp_path, user_config=tmp_path / "absent.toml")
    assert r.path is None and r.source.startswith("disabled:bth_toml:")


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("pyproject.toml", "[tool.bathos\n"),  # does not parse
        ("pyproject.toml", "[tool.bathos.bearings]\nshadow_log_dir = 3\n"),  # wrong type
        (
            "pyproject.toml",
            "[tool.bathos.bearings]\nshadow_log_dri = 'x'\n",
        ),  # unknown key (a typo)
        (".bth.toml", "[bearings]\nshadow_log_dir = 'x'\nextra = 1\n"),
        (".bth.toml", "bearings = 'x'\n"),  # not a table
    ],
)
def test_a_malformed_config_fails_loudly_and_never_falls_back(tmp_path, monkeypatch, name, text):
    monkeypatch.delenv(bs.ENV_VAR, raising=False)
    (tmp_path / name).write_text(text)
    with pytest.raises(bs.ShadowConfigError, match=name.replace(".", r"\.")):
        bs.resolve_shadow_log_dir(start=tmp_path, user_config=tmp_path / "absent.toml")


def test_every_file_is_read_up_front_even_when_a_higher_layer_decides(tmp_path, monkeypatch):
    ucfg = tmp_path / "u.toml"
    ucfg.write_text("[bearings\n")  # broken, at the lowest layer
    monkeypatch.setenv(bs.ENV_VAR, "/x/env")  # a higher layer decides
    with pytest.raises(bs.ShadowConfigError, match="u.toml"):
        bs.resolve_shadow_log_dir(start=tmp_path, user_config=ucfg)


def test_other_tool_bathos_keys_are_not_this_features_business(tmp_path, monkeypatch):
    monkeypatch.delenv(bs.ENV_VAR, raising=False)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "cat"))
    (tmp_path / "pyproject.toml").write_text(
        "[tool.bathos]\nslug = 'p'\n[tool.bathos.outputs]\nx = 1\n"
    )
    r = bs.resolve_shadow_log_dir(start=tmp_path, user_config=tmp_path / "absent.toml")
    assert r.source.startswith("default:")


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


def _nothing_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(bs.ENV_VAR, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


@pytest.mark.usefixtures("no_pipeline")
def test_a_cluster_layout_catalog_inside_a_pushed_project_is_accepted(tmp_path, monkeypatch):
    """Review 261111 V5: `bth submit` jobs use BTH_CATALOG_DIR=$BTH_PROJECT_ROOT/.bth/catalog, and myxcel pushes the
    project without `.git`, so the default <catalog>/shadow is not inside a work tree and labels are recorded."""
    _nothing_configured(tmp_path, monkeypatch)
    root = tmp_path / "pushed_project"
    cat = root / ".bth" / "catalog"
    cat.mkdir(parents=True)
    res = bs.ensure_shadow_pipeline(start=root, catalog_dir=cat)
    assert res["active"] and (cat / "shadow").is_dir()


@pytest.mark.usefixtures("no_pipeline")
def test_a_git_failure_refuses_the_shadow_dir_and_creates_nothing(tmp_path, monkeypatch):
    """The work-tree guard must not pass by not knowing: a git timeout is not "outside a work tree"."""
    import subprocess

    _nothing_configured(tmp_path, monkeypatch)
    cat = tmp_path / "proj" / ".bth" / "catalog"
    cat.mkdir(parents=True)
    real_run = subprocess.run

    def fake_run(argv, *a, **k):
        if "rev-parse" in argv:
            raise subprocess.TimeoutExpired(argv, 10)
        return real_run(argv, *a, **k)

    monkeypatch.setattr(bs.subprocess, "run", fake_run)
    with pytest.raises(bs.ShadowConfigError, match="cannot tell"):
        bs.ensure_shadow_pipeline(start=cat.parent.parent, catalog_dir=cat)
    assert not (cat / "shadow").exists() and get_pipeline() is None


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


def test_an_emit_error_never_raises(tmp_path, capsys):
    assert bs.emit_at_work_label(tmp_path / "missing.bth.toml").startswith("skipped:error:")
    assert "NOT recorded" in capsys.readouterr().err


@pytest.mark.usefixtures("no_pipeline")
def test_with_nothing_configured_labels_go_to_the_catalog_default(planted, monkeypatch):  # noqa: F811
    monkeypatch.delenv(bs.ENV_VAR, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(planted["tmp"] / "xdg"))
    p = planted["repo"] / "scripts" / "grow.bth.toml"
    assert bs.emit_at_work_label(p, catalog_dir=planted["cat"]) == "emitted"
    labels = [r for r in _records(planted["cat"] / "shadow") if r.get("name") == "shadow.label"]
    assert len(labels) == 1


@pytest.mark.usefixtures("no_pipeline")
def test_a_disabled_or_broken_shadow_log_is_said_on_stderr(planted, monkeypatch, capsys):  # noqa: F811
    p = planted["repo"] / "scripts" / "grow.bth.toml"
    monkeypatch.setenv(bs.ENV_VAR, "none")
    assert bs.emit_at_work_label(p, catalog_dir=planted["cat"]) == "off"
    assert "NOT recorded (off)" in capsys.readouterr().err
    monkeypatch.delenv(bs.ENV_VAR)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(planted["tmp"] / "xdg"))
    (planted["tmp"] / "xdg" / "bathos").mkdir(parents=True)
    (planted["tmp"] / "xdg" / "bathos" / "config.toml").write_text("[bearings\n")
    assert bs.emit_at_work_label(p, catalog_dir=planted["cat"]) == "skipped:config_error"
    assert "config.toml" in capsys.readouterr().err


# ── bth sync carries the default shadow dir ──────────────────────────────────────────────────────────────────────


def _fake_popen_factory(calls, fail_on=None):
    from unittest.mock import MagicMock

    def fake(cmd, **kw):  # noqa: ARG001
        calls.append(cmd)
        proc = MagicMock()
        proc.stderr.readline.return_value = ""
        proc.stdout.read.return_value = ""
        proc.stderr.read.return_value = ""
        rc = 23 if (fail_on and any(fail_on in c for c in cmd)) else 0
        proc.wait.return_value = rc
        proc.returncode = rc
        proc.communicate.return_value = ("", "boom" if rc else "")
        proc.poll.return_value = rc
        return proc

    return fake


@pytest.mark.parametrize("pull", [False, True])
def test_sync_carries_the_shadow_dir_with_update(tmp_path, monkeypatch, pull):
    from unittest.mock import patch

    from bathos.config import ProjectConfig
    from bathos.sync import sync_catalog

    cat = tmp_path / "catalog"
    (cat / "runs").mkdir(parents=True)
    (cat / "shadow").mkdir()
    cfg = ProjectConfig(
        slug="t", root=tmp_path, remotes={"eng": {"host": "eng", "remote_root": "~/p"}}
    )
    calls: list[list[str]] = []
    monkeypatch.setattr("bathos.sync.ensure_remote_catalog_dir", lambda *_a, **_k: None)
    with patch("bathos.sync.subprocess.Popen", side_effect=_fake_popen_factory(calls)):
        sync_catalog("eng", cfg, cat, pull=pull)
    shadow = [c for c in calls if any(str(a).rstrip("/").endswith("shadow") for a in c)]
    assert len(shadow) == 1
    assert "--update" in shadow[0] and "--ignore-existing" not in shadow[0]


def test_a_failed_shadow_sync_warns_and_never_fails_the_sync(tmp_path, monkeypatch, capsys):
    from unittest.mock import patch

    from bathos.config import ProjectConfig
    from bathos.sync import sync_catalog

    cat = tmp_path / "catalog"
    (cat / "runs").mkdir(parents=True)
    (cat / "shadow").mkdir()
    cfg = ProjectConfig(
        slug="t", root=tmp_path, remotes={"eng": {"host": "eng", "remote_root": "~/p"}}
    )
    monkeypatch.setattr("bathos.sync.ensure_remote_catalog_dir", lambda *_a, **_k: None)
    with patch(
        "bathos.sync.subprocess.Popen", side_effect=_fake_popen_factory([], fail_on="/shadow")
    ):
        sync_catalog("eng", cfg, cat, pull=False)
    assert "shadow log not synced" in capsys.readouterr().err


# ── derive matches AC-9's pooled counts ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def planted_full(request, monkeypatch):
    """``planted`` with every sidecar labelled (``plain`` serves gamma-aim), so silent is scored (owner 261112: a packet
    holding an unlabelled sidecar has unknown silent truth)."""
    import tests.test_bearings_ac9_planted as planted_mod

    full = [
        (n, lbl or '[charter]\npurpose = "gamma-aim"\n', h) for n, lbl, h in planted_mod.SIDECARS
    ]
    monkeypatch.setattr(planted_mod, "SIDECARS", full)  # what the fixture commits
    monkeypatch.setitem(globals(), "SIDECARS", full)  # what _setup labels at work
    return request.getfixturevalue("planted")


def _setup(planted, n_readers=3, mapping_kind="oracle", reconfirm=False):  # noqa: F811
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


def test_derived_counts_equal_ac9_pooled_counts(planted_full, shadow_log):
    m = _setup(planted_full)
    d = bs.derive(_records(shadow_log), project="pkt")
    assert d["skipped"] == [] and len(d["used"]) == 3
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
        0 < m["off_charter_unmapped"] < 1.0 and m["gold_agreement"] < 1.0
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


def test_labels_survive_a_charter_reconfirm(planted_full, shadow_log):
    """Owner 261111: a re-confirm (here a wording edit) must not erase labels logged under the earlier charter, while
    the verdicts themselves are pinned to the new charter's sha."""
    m = _setup(planted_full, reconfirm=True)
    recs = _records(shadow_log)
    label_shas = {r["fields"]["charter_sha256"] for r in recs if r.get("name") == "shadow.label"}
    d = bs.derive(recs, project="pkt")
    assert d["skipped"] == [] and len(d["used"]) == 3
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


def _raw_verdicts(recs):
    return [r for r in recs if r.get("name") == "shadow.verdict"]


def _restamp(rec, *, version=None, project=None, ts_add=1000.0):
    import copy

    r = copy.deepcopy(rec)
    f = r.get("fields", r)
    if version is not None:
        f["feature_version"] = version
    if project is not None:
        f["verdict"]["payload"]["project"] = project
    r["ts"] = float(r.get("ts", 0.0)) + ts_add
    return r


def test_another_projects_newer_version_does_not_hide_this_projects_verdicts(planted, shadow_log):  # noqa: F811
    """Review 261111 V3: the version is picked after the project filter."""
    _setup(planted)
    recs = _records(shadow_log)
    before = bs.derive(recs, project="pkt")
    other = _restamp(_raw_verdicts(recs)[0], version="m9-other", project="elsewhere")
    after = bs.derive([*recs, other], project="pkt")
    assert after["feature_version"] == before["feature_version"]
    assert sorted(after["used"]) == sorted(before["used"]) and after["used"]


def test_verdicts_left_out_by_the_version_pick_are_listed_as_skipped(planted, shadow_log):  # noqa: F811
    """Review 261111 V4: one re-emit under a new version must not drop the rest of the corpus silently."""
    _setup(planted)
    recs = _records(shadow_log)
    newer = _restamp(_raw_verdicts(recs)[0], version="m2-newer")
    d = bs.derive([*recs, newer], project="pkt")
    assert set(d["feature_version"].values()) == {"m2-newer"}
    reasons = [s["reason"] for s in d["skipped"]]
    assert sum(r.startswith("other_feature_version:") for r in reasons) == len(_raw_verdicts(recs))


def test_the_readers_arm_needs_three_blind_readers(planted, shadow_log):  # noqa: F811
    """Review 261111 V7: a packet with fewer than three blind readers is not scored on the readers arm."""
    _setup(planted, n_readers=2)
    d = bs.derive(_records(shadow_log), project="pkt")
    assert [s["reason"] for s in d["skipped"]] == [
        "readers_arm_needs_3_blind_readers:2_blind_0_oracle"
    ]
    assert d["used"] == []  # a dropped packet's readings are not counted as used
    assert not any(
        d["features"][f]["verdicts"] for f in bs.GATE_FEATURES if f.startswith("bearings.readers.")
    )


def test_an_oracle_reading_is_refused_on_the_readers_arm():
    payload = {
        "reader_id": "oracle-from-labels",
        "packet_sha256": "p",
        "charter_sha256": "c" * 64,
        "project": "x",
    }
    with pytest.raises(bs.ShadowConfigError, match="mapper_only"):
        bs.emit_comparison(payload, arm="readers", mapper_version="m1")


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
    _setup(planted, mapping_kind=kind)
    res = bs.shadow_check(
        records_dir=shadow_log,
        mode_state_dir=MODE_STATE,
        out_dir=planted["tmp"] / "derived",
        project="pkt",
        catalog_dir=planted["cat"],
    )
    assert any(k.startswith("bearings.readers.") for k in res["features"])
    # The arm is the decision: a sparse mapper can pass one metric alone (review 261111 F3).
    assert res["arms"]["readers"]["promotion_permitted"] is False


@pytest.mark.usefixtures("no_pipeline")
def test_shadow_check_with_no_records_refuses(tmp_path):
    (tmp_path / "logs").mkdir()
    res = bs.shadow_check(
        records_dir=tmp_path / "logs",
        mode_state_dir=MODE_STATE,
        out_dir=tmp_path / "d",
        catalog_dir=tmp_path / "no_catalog",
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
        # An oracle reading is never a blind reader, so it is refused on the readers arm (V7); isolation does not
        # depend on the arm.
        bs.emit_comparison(payload, arm="mapper_only", mapper_version="m1")

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


# ── positive control: a perfect, well-mixed record passes every gate (review 261111 V1) ─────────────────────────────

_LIVE = ["alpha-aim", "beta-aim", "gamma-aim", "delta-aim", "epsilon-aim", "zeta-aim"]
_ENFORCER = "tests/test_bearings_shadow.py::test_shadow_reader_isolation"


def _perfect_corpus(n_packets: int = 3, n_readers: int = 3) -> list[dict]:
    """Synthetic raw verdicts and at-work labels, shaped as comparison_payload freezes them.

    Each packet: three present purposes (each cited by one reading purpose and mapped to it), three silent ones (no
    reader maps them), and one off-charter reading purpose left unmapped. Prevalence stays well below 1, so the
    base-rate check is a real bar, not a free pass."""
    present, silent = _LIVE[:3], _LIVE[3:]
    recs: list[dict] = []
    ts = 1000.0
    for k in range(n_packets):
        pkt = f"pkt{k}"
        subj = {f"sidecar:t|s{k}_{i}": [pid] for i, pid in enumerate(present)} | {
            f"sidecar:t|s{k}_off": ["off-charter"]
        }
        for sk, slugs in subj.items():
            recs.append(
                {
                    "name": "shadow.label",
                    "ts": ts,
                    "fields": {
                        "feature": bs.LABEL_FEATURE,
                        "subject_key": sk,
                        "label": slugs,
                        "label_source": "at_work",
                        "after_first_confirmation": True,
                        "project": "pc",
                    },
                }
            )
        gold = {f"r{i}": [pid] for i, pid in enumerate(present)} | {"r_off": ["off-charter"]}
        items = [
            {"kind": "mapped", "reading_purpose_id": f"r{i}", "charter_purpose_id": pid}
            for i, pid in enumerate(present)
        ] + [{"kind": "silent", "charter_purpose_id": pid} for pid in silent]
        items.append({"kind": "unmapped", "reading_purpose_id": "r_off"})
        for j in range(n_readers):
            ts += 1
            payload = {
                "project": "pc",
                "packet_sha256": pkt,
                "reader_id": f"reader-{j}",
                "charter_sha256": "c" * 64,
                "charter_ids": _LIVE,
                "live_ids": _LIVE,
                "validity": [],
                "window": {
                    "since": f"2026-11-{1 + 7 * k:02d}T00:00:00Z",
                    "until": f"2026-11-{8 + 7 * k:02d}T00:00:00Z",
                },
                "n_unlabelled_sidecars": 0,
                "key": {
                    "present": present,
                    "silent": silent,
                    "revived": [],
                    "indeterminate": [],
                    "budget_limited": False,
                },
                "gold": gold,
                "maybe": {},
                "items": items,
                "required_labels": subj,
            }
            for arm in bs.ARMS:
                recs.append(
                    {
                        "name": "shadow.verdict",
                        "ts": ts,
                        "fields": {
                            "feature": bs.raw_feature(arm),
                            "subject_key": f"packet:{pkt}|reader:reader-{j}",
                            "verdict": {"value": "recorded", "payload": payload},
                            "feature_version": "m1-" + "c" * 16,
                        },
                    }
                )
    for feat in bs.GATE_FEATURES:
        recs.append(
            {
                "name": "shadow.isolation",
                "ts": ts + 1,
                "fields": {
                    "feature": feat,
                    "writes_isolated": True,
                    "leaked_paths": [],
                    "enforced_by": _ENFORCER,
                },
            }
        )
    return recs


@pytest.mark.usefixtures("no_pipeline")
def test_a_perfect_well_mixed_record_passes_every_gate(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in _perfect_corpus()))
    res = bs.shadow_check(
        records_dir=logs,
        mode_state_dir=MODE_STATE,
        out_dir=tmp_path / "d",
        project="pc",
        catalog_dir=tmp_path / "no_catalog",
    )
    assert res["skipped"] == []
    blocked = {
        f: v.get("blocking_reasons", v.get("refused"))
        for f, v in res["features"].items()
        if v.get("promotion_permitted") is not True
    }
    assert blocked == {}
    assert all(a["promotion_permitted"] for a in res["arms"].values())


@pytest.mark.usefixtures("no_pipeline")
def test_agreement_scores_unmapped_live_targets_as_negatives():
    d = bs.derive(_perfect_corpus(n_packets=1), project="pc")
    c = _conf(d, "bearings.readers.agreement")
    # 3 readers x 3 supported mapped pairs; 3 readers x 3 reading purposes x 5 unmapped live targets
    assert (c.tp, c.fp, c.fn, c.tn) == (9, 0, 0, 45)


@pytest.mark.usefixtures("no_pipeline")
def test_an_invalid_or_unlabelled_comparison_is_not_scored():
    recs = _perfect_corpus(n_packets=1)
    for r in recs:
        if r["name"] == "shadow.verdict":
            p = r["fields"]["verdict"]["payload"]
            if p["reader_id"] == "reader-0":
                p["validity"] = ["1 withheld run(s) with an unresolvable sidecar"]
            elif p["reader_id"] == "reader-1":
                p["required_labels"] = {}
            else:
                p.pop("validity", None)  # the payload dict is shared by both arms' records
    d = bs.derive(recs, project="pc")
    reasons = sorted({s["reason"].split(":")[0] for s in d["skipped"]})
    assert reasons == ["invalid", "no_labelled_sidecars", "payload_predates_validity_check"]
    assert d["used"] == []


def _degenerate_corpus(kind: str) -> list[dict]:
    """``_perfect_corpus`` with every reader's mapping replaced by a degenerate one (same labels, same packets)."""
    recs = _perfect_corpus()
    done: set[int] = set()
    for r in recs:
        if r["name"] != "shadow.verdict":
            continue
        p = r["fields"]["verdict"]["payload"]
        if id(p) in done:  # the payload dict is shared by both arms' records
            continue
        done.add(id(p))
        rps = sorted(p["gold"])
        if kind == "all_unmapped":
            mapping = {rp: [] for rp in rps}
        elif kind == "all_to_first_live":
            mapping = {rp: [_LIVE[0]] for rp in rps}
        elif kind == "each_to_every_purpose":
            mapping = {rp: list(_LIVE) for rp in rps}
        else:  # first_only: one reading purpose mapped (correctly), the rest left unmapped
            mapping = {rp: (p["gold"][rp] if rp == rps[0] else []) for rp in rps}
        mapped = {t for ts in mapping.values() for t in ts}
        items = [
            {"kind": "mapped", "reading_purpose_id": rp, "charter_purpose_id": t}
            for rp, ts in mapping.items()
            for t in ts
        ]
        items += [
            {"kind": "unmapped", "reading_purpose_id": rp} for rp, ts in mapping.items() if not ts
        ]
        items += [{"kind": "silent", "charter_purpose_id": t} for t in _LIVE if t not in mapped]
        p["items"] = items
    return recs


@pytest.mark.usefixtures("no_pipeline")
@pytest.mark.parametrize(
    "kind", ["all_unmapped", "all_to_first_live", "each_to_every_purpose", "first_only"]
)
def test_degenerate_mappers_are_blocked_on_metrics_not_counts(tmp_path, kind):
    """On a corpus where a perfect mapper passes every gate, a degenerate mapper is blocked by what it gets wrong,
    never by sample size or missing isolation evidence (the planted fixture is too small to show that)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in _degenerate_corpus(kind))
    )
    res = bs.shadow_check(
        records_dir=logs,
        mode_state_dir=MODE_STATE,
        out_dir=tmp_path / "d",
        project="pc",
        catalog_dir=tmp_path / "no_catalog",
    )
    for arm in bs.ARMS:
        assert res["arms"][arm]["promotion_permitted"] is False
        for m in res["arms"][arm]["blocked_metrics"]:
            reasons = res["features"][bs.gate_feature(arm, m)].get("blocking_reasons", [])
            assert reasons and not any(
                r.startswith(("insufficient_", "no_isolation", "enforcer_")) for r in reasons
            ), (m, reasons)
    if kind == "first_only":
        # A sparse but correct mapper passes agreement alone (F3); only the arm-level conjunction stops it.
        assert res["features"]["bearings.readers.agreement"]["promotion_permitted"] is True
        assert "agreement" not in res["arms"]["readers"]["blocked_metrics"]


# ── cluster runs: pushed without .git, catalog without confirmations (owner 261112: judge at check time) ─────────


def test_a_cluster_run_without_git_emits_a_label_keyed_like_the_local_packet(
    tmp_path, monkeypatch, shadow_log
):  # noqa: ARG001  (shadow_log: the env and the pipeline lifecycle)
    """V5: `bth submit` jobs run in a checkout pushed without `.git`; BTH_PROJECT_ROOT gives the root, so the subject
    key is the repo-relative one a local packet's required_labels uses."""
    import hashlib

    root = tmp_path / "pushed"
    (root / "scripts").mkdir(parents=True)
    side = root / "scripts" / "grow.bth.toml"
    side.write_text('[experiment]\nhypothesis = "h"\n\n[charter]\npurpose = ["alpha-aim"]\n')
    monkeypatch.setenv("BTH_PROJECT_ROOT", str(root))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "pkt")
    cat = root / ".bth" / "catalog"
    cat.mkdir(parents=True)
    assert bs.emit_at_work_label(side, run_id="r1", catalog_dir=cat) == "emitted"
    (lab,) = [r for r in _records(shadow_log) if r.get("name") == "shadow.label"]
    f = lab["fields"]
    assert f["subject_key"] == bs.sidecar_subject(
        "pkt", "scripts/grow.bth.toml", hashlib.sha256(side.read_bytes()).hexdigest()
    )
    assert f["after_first_confirmation"] is False  # the cluster catalog never saw the confirmation
    assert (
        f["project"] == "pkt"
    )  # what derive matches against the checking catalog's first confirmation


def _flag_false_labels(recs):
    for r in recs:
        if r.get("name") == "shadow.label":
            r["fields"]["after_first_confirmation"] = False
            r["fields"]["charter_sha256"] = "s" * 64
    return recs


@pytest.mark.usefixtures("no_pipeline")
def test_a_label_after_the_first_confirmation_counts_by_time_at_check():
    """A label the emitter flagged False (it could not see the confirmation) counts when its time is at or after
    the project's first confirmation in the checking catalog and its charter sha is one that catalog confirmed."""
    recs = _flag_false_labels(_perfect_corpus(n_packets=1))
    label_ts = min(r["ts"] for r in recs if r["name"] == "shadow.label")
    shas = {"pc": {"s" * 64}}

    def used(first, confirmed):
        return bs.derive(recs, project="pc", first_confirmed=first, confirmed_shas=confirmed)[
            "used"
        ]

    assert bs.derive(recs, project="pc")["used"] == []  # the flag alone: nothing counts
    assert len(used({"pc": label_ts - 1}, shas)) == 6  # 3 readers, each on both arms
    assert used({"pc": label_ts + 1}, shas) == []
    # another project's confirmation says nothing about this one's labels
    assert used({"elsewhere": 0.0}, {"elsewhere": shas["pc"]}) == []
    # Clock skew (owner 261112): in time, but under charter text this catalog never confirmed, does not count;
    # neither does a check that passes no confirmed shas at all.
    assert used({"pc": label_ts - 1}, {"pc": {"d" * 64}}) == []
    assert used({"pc": label_ts - 1}, None) == []


def test_first_confirmations_reads_the_catalog(planted):  # noqa: F811
    from datetime import datetime

    first = bs.first_confirmations(planted["cat"])
    assert set(first) == {"pkt"}
    assert abs(first["pkt"] - datetime.now().timestamp()) < 24 * 3600
    assert bs.first_confirmations(planted["tmp"] / "no_catalog") == {}
    from bathos.bearings.shadow import _confirmed_charter

    sha, _ = _confirmed_charter(planted["repo"], planted["cat"])
    assert bs.confirmed_charter_shas(planted["cat"]) == {"pkt": {sha}}
    assert bs.confirmed_charter_shas(planted["tmp"] / "no_catalog") == {}


# ── open items from review 261111 (owner choices 261112, AskUserQuestion) ─────────────────────────────────────────


def _verdict_payloads(recs):
    return [r["fields"]["verdict"]["payload"] for r in recs if r.get("name") == "shadow.verdict"]


def test_a_packet_with_an_unlabelled_sidecar_scores_everything_but_silent(planted, shadow_log):  # noqa: F811
    """``plain`` carries no label, so its work could serve a purpose the key calls silent."""
    _setup(planted)
    d = bs.derive(_records(shadow_log), project="pkt")
    assert [s["reason"] for s in d["skipped"]] == ["silent_unscored_unlabelled_sidecars:1"]
    assert len(d["used"]) == 3
    assert d["features"]["bearings.readers.silent"]["verdicts"] == []
    assert d["features"]["bearings.readers.agreement"]["verdicts"]
    assert d["features"]["bearings.readers.off_charter"]["verdicts"]


def test_the_payload_carries_audit_provenance(planted, shadow_log):  # noqa: F811
    _setup(planted, n_readers=1)
    (p,) = _verdict_payloads(_records(shadow_log))
    assert p["mapper_id"] == "mapper" and len(p["reading_id"]) == 16
    assert p["window"]["since"] and p["window"]["until"]
    assert len(p["run_ids"]) == 5 and p["n_unlabelled_sidecars"] == 1
    assert p["padded_sidecars"] == []


@pytest.mark.usefixtures("no_pipeline")
def test_a_charter_id_the_builder_cannot_match_makes_the_comparison_invalid(planted):  # noqa: F811
    out, sha = _packet(planted)
    from bathos.bearings.shadow import _confirmed_charter

    charter_sha, _ = _confirmed_charter(planted["repo"], planted["cat"])
    stored = {"packet_sha256": sha, "reader_id": "r", "purposes": []}
    charter = dict(planted["charter"]) | {"omega": {"state": "live", "text": "one word"}}
    payload = bs.comparison_payload(
        planted["repo"],
        out,
        stored,
        {"items": [], "reading_id": "x" * 16},
        charter,
        charter_sha256=charter_sha,
        project="pkt",
    )
    assert any("['omega'] cannot be token-matched" in v for v in payload["validity"])


def test_a_sidecar_with_more_than_two_purposes_is_disputed_gold():
    firm, disputed, padded = bs._cap_padded(
        {"a.toml": ["alpha-aim", "beta-aim", "gamma-aim"], "b.toml": ["alpha-aim", "off-charter"]},
        {"c.toml": ["delta-aim"]},
    )
    assert firm == {"b.toml": ["alpha-aim", "off-charter"]}
    assert disputed == {"c.toml": ["delta-aim"], "a.toml": ["alpha-aim", "beta-aim", "gamma-aim"]}
    assert padded == ["a.toml"]


@pytest.mark.usefixtures("no_pipeline")
def test_derived_items_name_the_raw_subjects_they_come_from():
    d = bs.derive(_perfect_corpus(n_packets=1), project="pc")
    lab = d["features"]["bearings.readers.silent"]["labels"][0]["fields"]
    assert lab["derived_from"] == ";".join(f"packet:pkt0|reader:reader-{j}" for j in range(3))


@pytest.mark.usefixtures("no_pipeline")
def test_a_sidecar_first_labelled_before_the_confirmation_never_counts():
    """F5: a later post-confirmation label of the same (unchanged) sidecar does not make it at-work."""
    recs = _perfect_corpus(n_packets=1)
    first = next(r for r in recs if r["name"] == "shadow.label")
    early = json.loads(json.dumps(first))
    early["ts"] = first["ts"] - 500
    early["fields"]["after_first_confirmation"] = False
    d = bs.derive([early, *recs], project="pc")
    assert d["used"] == []
    assert {s["reason"] for s in d["skipped"]} == {"labels_not_logged_at_work:1"}
    # by time, too: a check-time confirmation between the two labels still leaves the earliest one before it
    assert (
        bs.derive([early, *recs], project="pc", first_confirmed={"pc": first["ts"] - 1})["used"]
        == []
    )


@pytest.mark.usefixtures("no_pipeline")
def test_only_packets_with_disjoint_windows_are_scored():
    recs = _perfect_corpus(n_packets=3)
    for p in _verdict_payloads(recs):
        if p["packet_sha256"] == "pkt1":  # reaches back into pkt0's week
            p["window"] = {"since": "2026-11-05T00:00:00Z", "until": "2026-11-15T00:00:00Z"}
        elif p["packet_sha256"] == "pkt2":
            p["window"] = {"since": "2026-11-15T00:00:00Z", "until": "2026-11-22T00:00:00Z"}
    d = bs.derive(recs, project="pc")
    assert {s["reason"] for s in d["skipped"]} == {"window_overlaps:packet:pkt0"}
    assert {u.split("|")[0] for u in d["used"]} == {"packet:pkt0", "packet:pkt2"}


@pytest.mark.usefixtures("no_pipeline")
def test_a_packet_without_a_readable_window_is_not_scored():
    recs = _perfect_corpus(n_packets=1)
    for p in _verdict_payloads(recs):
        p["window"] = {"since": None, "until": "2026-11-08T00:00:00Z"}
    d = bs.derive(recs, project="pc")
    assert d["used"] == [] and {s["reason"] for s in d["skipped"]} == {"window_unreadable"}


@pytest.mark.usefixtures("no_pipeline")
def test_false_silent_is_not_read_below_eight_present_purposes(tmp_path):
    """F3: three packets with two present purposes each (6 present, 9 silent); silent is blocked by the floor alone."""
    logs = tmp_path / "logs"
    logs.mkdir()
    recs = _perfect_corpus(n_packets=3)
    for p in _verdict_payloads(recs):
        p["key"]["present"] = p["key"]["present"][:2]
    (logs / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    res = bs.shadow_check(
        records_dir=logs,
        mode_state_dir=MODE_STATE,
        out_dir=tmp_path / "d",
        project="pc",
        catalog_dir=tmp_path / "no_catalog",
    )
    for arm in bs.ARMS:
        silent = res["features"][f"bearings.{arm}.silent"]
        assert silent["n_present"] == 6 and silent["promotion_permitted"] is False
        # cisternal alone would pass silent (6 present + 9 silent, recall 1): the floor is its only reason
        assert silent["blocking_reasons"] == [f"bathos_n_present_below_floor:6<{bs.MIN_PRESENT}"]
        assert res["arms"][arm]["blocked_metrics"] == ["silent"]


def test_a_config_parent_that_is_not_a_table_raises(tmp_path):
    cfg = tmp_path / "pyproject.toml"
    cfg.write_text('[tool]\nbathos = "x"\n')
    with pytest.raises(bs.ShadowConfigError, match=r"\[tool.bathos\] .* must be a table"):
        bs._bearings_table(cfg, ("tool", "bathos", "bearings"))
