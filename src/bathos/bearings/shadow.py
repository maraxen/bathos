"""Bearings in cisternal shadow mode (spec 261104 T7; plan .praxia/docs/plans/261111_bearings-slice3-shadow.md).

Nothing here shows anything to anyone or changes what a reader sees. It records, and it scores.

- ``resolve_shadow_log_dir`` / ``ensure_shadow_pipeline``: where shadow records go, and starting cisternal's pipeline
  for them (owner 261111: a separate shadow log, off unless configured; the ``CISTERNAL_TELEMETRY`` cutover pipeline
  is reused when it is already running).
- ``emit_at_work_label``: called by ``bth run``; one ``shadow.label`` (source ``at_work``) per labelled sidecar, keyed
  on the sidecar alone, because a label written at work time cannot know any packet or reader.
- ``comparison_payload`` / ``emit_comparison``: owner-side, after a comparison; one raw ``shadow.verdict`` per
  (packet, reader) carrying what ``ac9`` needs to score it. Raw verdicts are never scored directly.
- ``derive``: expands raw verdicts into the binary gate features below, with labels derived from the at-work labels
  (owner 261111). A raw verdict is used only when every label it rests on was logged at work against the same
  confirmed charter, with the same slugs.
- ``shadow_check``: ``derive`` then ``cisternal.shadow.check_mode_state`` per gate feature. Read-only.

Gate features, per arm (``readers`` or ``mapper_only``), matching ``ac9.pooled_metrics`` count for count:

- ``bearings.<arm>.agreement``: one item per (packet, reader, reading purpose, target); verdict true; label true when
  the target is in the reading purpose's gold. Precision is gold agreement.
- ``bearings.<arm>.off_charter``: one item per (packet, reader, reading purpose with labelled evidence); verdict true
  when the reading purpose is left unmapped; label true when its gold is exactly ``off-charter``. Recall is off-charter
  work left unmapped.
- ``bearings.<arm>.silent``: one item per (packet, live purpose present or silent); verdict true when no reader maps
  it (T7(b)); label true when it is silent in the answer key. Recall is silent recall; the false-positive rate is
  false-silent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_VAR = "BTH_SHADOW_LOG_DIR"
LABEL_FEATURE = "bearings.purpose_label"
ARMS = ("readers", "mapper_only")
METRICS = ("agreement", "off_charter", "silent")
_DISABLED = {"", "none", "off", "0", "false", "no"}


def raw_feature(arm: str) -> str:
    if arm not in ARMS:
        raise ValueError(f"arm must be one of {ARMS}, got {arm!r}")
    return f"bearings.{arm}.raw"


def gate_feature(arm: str, metric: str) -> str:
    if arm not in ARMS or metric not in METRICS:
        raise ValueError(f"unknown gate feature {arm!r}/{metric!r}")
    return f"bearings.{arm}.{metric}"


GATE_FEATURES = tuple(gate_feature(a, m) for a in ARMS for m in METRICS)


class ShadowConfigError(ValueError):
    """A malformed shadow configuration, or a shadow log dir that a reader could see."""


# ── where shadow records go ──────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ShadowLogDir:
    path: Path | None
    source: str  # "argument" | "env" | "pyproject:<path>" | "user_config:<path>" | "off" | "disabled:<layer>"


def _nearest_pyproject(start: Path) -> Path | None:
    for d in (start, *start.parents):
        p = d / "pyproject.toml"
        if p.is_file():
            return p
    return None


def _toml_value(path: Path, keys: tuple[str, ...]) -> Any:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ShadowConfigError(f"cannot read {path}: {e}") from e
    node: Any = data
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def _as_dir(raw: Any, base: Path, where: str) -> Path | None:
    if not isinstance(raw, str):
        raise ShadowConfigError(
            f"shadow_log_dir in {where} must be a string, got {type(raw).__name__}"
        )
    if raw.strip().lower() in _DISABLED:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (base / p)


def resolve_shadow_log_dir(
    explicit: Path | str | None = None,
    *,
    start: Path | None = None,
    user_config: Path | None = None,
) -> ShadowLogDir:
    """Explicit argument > ``BTH_SHADOW_LOG_DIR`` (``none``/empty disables) > ``[tool.bathos] shadow_log_dir`` in the
    nearest ``pyproject.toml`` > ``shadow_log_dir`` in ``${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml`` > off.
    A malformed file raises ``ShadowConfigError``."""
    if explicit is not None:
        return ShadowLogDir(Path(explicit).expanduser(), "argument")
    if ENV_VAR in os.environ:
        raw = os.environ[ENV_VAR]
        if raw.strip().lower() in _DISABLED:
            return ShadowLogDir(None, "disabled:env")
        return ShadowLogDir(Path(raw).expanduser(), "env")
    here = Path(start) if start is not None else Path.cwd()
    pyproj = _nearest_pyproject(here)
    if pyproj is not None:
        raw = _toml_value(pyproj, ("tool", "bathos", "shadow_log_dir"))
        if raw is not None:
            p = _as_dir(raw, pyproj.parent, str(pyproj))
            return ShadowLogDir(p, f"pyproject:{pyproj}" if p else f"disabled:pyproject:{pyproj}")
    if user_config is None:
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        user_config = Path(xdg) / "bathos" / "config.toml"
    if user_config.is_file():
        raw = _toml_value(user_config, ("shadow_log_dir",))
        if raw is not None:
            p = _as_dir(raw, user_config.parent, str(user_config))
            return ShadowLogDir(
                p, f"user_config:{user_config}" if p else f"disabled:user_config:{user_config}"
            )
    return ShadowLogDir(None, "off")


def _git_toplevel(path: Path) -> Path | None:
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(path if path.is_dir() else path.parent),
                "rev-parse",
                "--show-toplevel",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (
        Path(out.stdout.strip()).resolve() if out.returncode == 0 and out.stdout.strip() else None
    )


def ensure_shadow_pipeline(
    explicit: Path | str | None = None, *, start: Path | None = None
) -> dict[str, Any]:
    """Make sure a cisternal pipeline can take shadow records. Returns ``{active, pipeline, path, source}``.

    ``pipeline`` is ``existing`` when one is already running (the cutover, or an earlier call), ``shadow`` when this
    call started one into the resolved dir, ``off`` when nothing is configured. A dir inside the project's git work
    tree is refused (a reader's packet is built from that tree)."""
    from cisternal.telemetry.pipeline import get_pipeline

    if get_pipeline() is not None:
        return {"active": True, "pipeline": "existing", "path": None, "source": "existing"}
    res = resolve_shadow_log_dir(explicit, start=start)
    if res.path is None:
        return {"active": False, "pipeline": "off", "path": None, "source": res.source}
    path = res.path.resolve()
    top = _git_toplevel(Path(start) if start is not None else Path.cwd())
    if top is not None and (path == top or top in path.parents):
        raise ShadowConfigError(
            f"shadow log dir {path} is inside the project work tree {top}; packets are built from it, so shadow "
            "records (which carry purpose labels) must live outside it"
        )
    import cisternal

    path.mkdir(parents=True, exist_ok=True)
    cisternal.init(
        log_dir=path
    )  # heartbeat stays at its 30 s default (0 would spin: self_obs sleeps `interval`)
    return {
        "active": get_pipeline() is not None,
        "pipeline": "shadow",
        "path": str(path),
        "source": res.source,
    }


# ── at-work labels (bth run) ─────────────────────────────────────────────────────────────────────────────────────


def sidecar_subject(project: str, rel_path: str, sha256: str) -> str:
    return f"sidecar:{project}:{rel_path}@{sha256}"


def _confirmed_charter(root: Path, catalog_dir: Path | None) -> tuple[str, dict[str, Any]] | None:
    """(charter_sha256, purposes) of the project's confirmed charter, or None when it is not confirmed."""
    from bathos.charter import (
        CharterNotFoundError,
        _resolve_catalog_dir,
        _resolve_project_slug,
        status,
    )
    from bathos.index import connect_read

    try:
        st = status(root, catalog_dir=catalog_dir)
    except CharterNotFoundError:
        return None
    if st.get("state") != "confirmed":
        return None
    con = connect_read(_resolve_catalog_dir(catalog_dir))
    try:
        row = con.execute(
            "SELECT charter_sha256, purposes_json FROM charter_confirmations WHERE project = ? "
            "ORDER BY ts DESC, eid DESC LIMIT 1",
            [_resolve_project_slug(root)],
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return str(row[0]), json.loads(row[1] or "{}")


def emit_at_work_label(
    sidecar_path: Path | str,
    *,
    run_id: str = "",
    catalog_dir: Path | None = None,
    log_dir: Path | str | None = None,
) -> str:
    """Emit the sidecar's ``[charter] purpose`` as an at-work label. Returns what happened, never raises:
    ``emitted``, ``off`` (no shadow log), ``no_label``, or ``skipped:<reason>``."""
    try:
        return _emit_at_work_label(Path(sidecar_path), run_id, catalog_dir, log_dir)
    except Exception as e:  # a shadow emit must never change a run
        logger.warning("bearings at-work label not emitted: %s: %s", type(e).__name__, e)
        return f"skipped:error:{type(e).__name__}"


def _emit_at_work_label(path: Path, run_id: str, catalog_dir: Path | None, log_dir: Any) -> str:
    from bathos.bearings.purpose_label import OFF_CHARTER, PurposeLabelError, read_purpose_label
    from bathos.charter import _resolve_project_slug

    raw = path.read_bytes()
    try:
        slugs = read_purpose_label(tomllib.loads(raw.decode("utf-8")))
    except (PurposeLabelError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return _skip("malformed_label", path)
    if not slugs:
        return "no_label"
    root = _git_toplevel(path)
    if root is None:
        return _skip("not_in_git", path)
    charter = _confirmed_charter(root, catalog_dir)
    if charter is None:
        return _skip("charter_unconfirmed", path)
    charter_sha, purposes = charter
    unknown = sorted(set(slugs) - set(purposes) - {OFF_CHARTER})
    if unknown:
        return _skip(f"unknown_slug:{','.join(unknown)}", path)
    pipe = ensure_shadow_pipeline(log_dir, start=root)
    if not pipe["active"]:
        return "off"
    import cisternal.shadow as cs

    project = _resolve_project_slug(root)
    rel = path.resolve().relative_to(root).as_posix()
    ok = cs.emit_label(
        feature=LABEL_FEATURE,
        subject_key=sidecar_subject(project, rel, hashlib.sha256(raw).hexdigest()),
        label=sorted(slugs),
        label_source="at_work",
        labeller="bth-run",
        charter_sha256=charter_sha,
        project=project,
        run_id=run_id,
    )
    return "emitted" if ok else _skip("pipeline_refused", path)


def _skip(reason: str, path: Path) -> str:
    logger.info("bearings at-work label skipped (%s): %s", reason, path)
    return f"skipped:{reason}"


# ── raw verdicts (owner side, after a comparison) ────────────────────────────────────────────────────────────────


def _blob_bytes(repo: Path, blob: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", blob],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout


def _required_labels(repo: Path, packet_dir: Path, project: str) -> dict[str, list[str]]:
    """Every labelled sidecar blob the answer key reads (shown, excluded, or a withheld run's), by at-work subject."""
    from bathos.bearings.ac9 import _label_of, _manifest

    blobs: set[tuple[str, str]] = set()
    index = json.loads((packet_dir / "reader" / "index.json").read_text(encoding="utf-8"))
    blobs |= {(e["path"], e["blob_sha"]) for e in index["sidecars"]}
    manifest = _manifest(packet_dir)
    cands = {str(e.get("path", "")) for e in manifest.get("exclusions", [])}
    cands |= {str(p) for p in manifest.get("withheld_run_sidecars", [])}
    for c in cands:
        if c.startswith("sidecar:") and "@" in c:
            rel, _, blob = c[len("sidecar:") :].rpartition("@")
            blobs.add((rel, blob))
    out: dict[str, list[str]] = {}
    for rel, blob in sorted(blobs):
        raw = _blob_bytes(repo, blob)
        slugs = _label_of(raw.decode("utf-8", errors="replace"), f"sidecar:{rel}@{blob[:12]}")
        if slugs:
            out[sidecar_subject(project, rel, hashlib.sha256(raw).hexdigest())] = sorted(slugs)
    return out


def comparison_payload(
    repo: Path | str,
    packet_dir: Path | str,
    reading: dict[str, Any],
    comparison: dict[str, Any],
    charter_purposes: dict[str, dict[str, Any]],
    *,
    charter_sha256: str,
    project: str,
) -> dict[str, Any]:
    """What ``derive`` needs to score one comparison, frozen at emit time (the packet dir may be gone later)."""
    from bathos.bearings.ac9 import answer_key, reading_gold

    repo_p, pkt = Path(repo), Path(packet_dir)
    key = answer_key(repo_p, pkt, charter_purposes)
    keep = ("kind", "reading_purpose_id", "charter_purpose_id", "matched_retired", "budget_limited")
    return {
        "project": project,
        "packet_sha256": reading["packet_sha256"],
        "reader_id": reading["reader_id"],
        "charter_sha256": charter_sha256,
        "key": {
            k: key[k] for k in ("present", "silent", "revived", "indeterminate", "budget_limited")
        },
        "gold": reading_gold(pkt, reading, key["labels"]),
        "maybe": reading_gold(pkt, reading, key["ambiguous_labels"]),
        "items": [{k: it[k] for k in keep if k in it} for it in comparison["items"]],
        "required_labels": _required_labels(repo_p, pkt, project),
    }


def emit_comparison(payload: dict[str, Any], *, arm: str, mapper_version: str) -> bool:
    """One raw ``shadow.verdict`` for a (packet, reader). ``feature_version`` pins the mapper and the charter, so
    verdicts from a changed mapper or charter never pool with earlier ones. False when no pipeline is running."""
    import cisternal.shadow as cs

    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return cs.emit_verdict(
        feature=raw_feature(arm),
        subject_key=f"packet:{payload['packet_sha256']}|reader:{payload['reader_id']}",
        verdict={"value": "recorded", "payload": payload},
        stage="shadow",
        feature_version=f"{mapper_version}-{payload['charter_sha256'][:16]}",
        inputs_sha256=hashlib.sha256(canon.encode()).hexdigest(),
        project=payload["project"],
    )


# ── expansion into gate features ─────────────────────────────────────────────────────────────────────────────────


def _unwrap(rec: dict[str, Any]) -> tuple[str, float, dict[str, Any]]:
    fields = rec.get("fields", rec)
    if not isinstance(fields, dict):
        fields = rec
    ts = rec.get("ts", fields.get("ts", 0.0))
    try:
        ts_f = float(ts)
    except (TypeError, ValueError):
        ts_f = 0.0
    return str(rec.get("name", "")), ts_f, fields


def read_records(files: Iterable[Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            text = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def derive(
    records: Iterable[dict[str, Any]],
    *,
    project: str | None = None,
    feature_version: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Binary gate records from raw verdicts and at-work labels.

    ``feature_version`` maps an arm to the version to score; an arm left out uses the version of its latest raw
    verdict. Returns ``{"features": {feature: {"verdicts": [...], "labels": [...]}}, "feature_version": {...},
    "skipped": [...], "used": [...]}``."""
    from bathos.bearings.ac9 import OFF_CHARTER, _silent_reported, _targets_by_reading_purpose

    at_work: dict[tuple[str, str], tuple[float, list[str]]] = {}
    raw: dict[str, dict[tuple[str, str], tuple[float, dict[str, Any]]]] = {a: {} for a in ARMS}
    isolation: list[dict[str, Any]] = []
    for rec in records:
        name, ts, f = _unwrap(rec)
        feat = f.get("feature")
        if name == "shadow.isolation" and feat in GATE_FEATURES:
            isolation.append(rec)
        elif (
            name == "shadow.label" and feat == LABEL_FEATURE and f.get("label_source") == "at_work"
        ):
            k = (str(f.get("subject_key")), str(f.get("charter_sha256", "")))
            if k not in at_work or ts >= at_work[k][0]:
                at_work[k] = (ts, sorted(f.get("label") or []))
        elif name == "shadow.verdict":
            for arm in ARMS:
                if feat == raw_feature(arm):
                    k = (str(f.get("subject_key")), str(f.get("feature_version", "")))
                    if k not in raw[arm] or ts >= raw[arm][k][0]:
                        raw[arm][k] = (ts, f)

    features: dict[str, dict[str, list[dict[str, Any]]]] = {
        g: {"verdicts": [], "labels": []} for g in GATE_FEATURES
    }
    for rec in isolation:
        features[_unwrap(rec)[2]["feature"]]["verdicts"].append(rec)
    chosen: dict[str, str] = {}
    skipped: list[dict[str, str]] = []
    used: list[str] = []

    for arm in ARMS:
        if not raw[arm]:
            continue
        fv = (feature_version or {}).get(arm)
        if fv is None:
            fv = max(raw[arm].items(), key=lambda kv: kv[1][0])[0][1]
        chosen[arm] = fv
        packets: dict[str, list[tuple[float, dict[str, Any]]]] = {}
        for (subject, v), (ts, f) in sorted(raw[arm].items()):
            if v != fv:
                continue
            payload = (f.get("verdict") or {}).get("payload") or {}
            if project is not None and payload.get("project") != project:
                continue
            bad = [
                s
                for s, slugs in payload.get("required_labels", {}).items()
                if at_work.get((s, payload.get("charter_sha256", "")), (0, None))[1] != slugs
            ]
            if bad:
                skipped.append(
                    {"subject": subject, "reason": f"labels_not_logged_at_work:{len(bad)}"}
                )
                continue
            packets.setdefault(payload["packet_sha256"], []).append((ts, payload))
            used.append(subject)

        def put(
            metric: str, subject: str, verdict: bool, label: bool, ts: float, **extra: Any
        ) -> None:
            feat = gate_feature(arm, metric)
            features[feat]["verdicts"].append(
                {
                    "name": "shadow.verdict",
                    "ts": ts,
                    "fields": {
                        "feature": feat,
                        "subject_key": subject,
                        "verdict": verdict,
                        "stage": "shadow",
                        "feature_version": fv,
                    },
                }
            )
            features[feat]["labels"].append(
                {
                    "name": "shadow.label",
                    "ts": ts,
                    "fields": {
                        "feature": feat,
                        "subject_key": subject,
                        "label": label,
                        "label_source": "at_work",
                        "labeller": "bearings-derive",
                        **extra,
                    },
                }
            )

        for pkt, readings in sorted(packets.items()):
            keys = {json.dumps(p["key"], sort_keys=True) for _, p in readings}
            if len(keys) != 1:
                skipped.append({"subject": f"packet:{pkt}", "reason": "answer_keys_differ"})
                continue
            key = readings[0][1]["key"]
            ts_max = max(ts for ts, _ in readings)
            derived_from = f"packet:{pkt}"
            if not key["budget_limited"]:
                combined = set.intersection(*[_silent_reported(p) for _, p in readings])
                for pid in sorted(set(key["silent"]) | set(key["present"])):
                    put(
                        "silent",
                        f"packet:{pkt}|purpose:{pid}",
                        pid in combined,
                        pid in key["silent"],
                        ts_max,
                        derived_from=derived_from,
                    )
            for ts, p in readings:
                base = f"packet:{pkt}|reader:{p['reader_id']}"
                targets = _targets_by_reading_purpose(p["items"])
                gold, maybe = p["gold"], p.get("maybe") or {}
                for rp, g in sorted(gold.items()):
                    if not g:
                        continue  # cites no labelled work: nothing to say about off-charter
                    if OFF_CHARTER in g and (
                        g != [OFF_CHARTER] or set(maybe.get(rp, [])) - {OFF_CHARTER}
                    ):
                        continue  # mixed or disputed: AC-9 does not score it either
                    put(
                        "off_charter",
                        f"{base}|rp:{rp}",
                        not targets.get(rp),
                        g == [OFF_CHARTER],
                        ts,
                        derived_from=derived_from,
                    )
                for rp, ts_set in sorted(targets.items()):
                    for t in sorted(ts_set):
                        if t not in gold.get(rp, []) and t in maybe.get(rp, []):
                            continue  # found only among disputed labels: unscored (round 4)
                        put(
                            "agreement",
                            f"{base}|rp:{rp}|target:{t}",
                            True,
                            t in gold.get(rp, []),
                            ts,
                            derived_from=derived_from,
                        )
    return {"features": features, "feature_version": chosen, "skipped": skipped, "used": used}


def write_derived(derived: dict[str, Any], out_dir: Path | str) -> dict[str, tuple[Path, Path]]:
    """One verdicts file and one labels file per gate feature."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, tuple[Path, Path]] = {}
    for feat, recs in derived["features"].items():
        v, lab = out / f"{feat}.verdicts.jsonl", out / f"{feat}.labels.jsonl"
        v.write_text("".join(json.dumps(r) + "\n" for r in recs["verdicts"]), encoding="utf-8")
        lab.write_text("".join(json.dumps(r) + "\n" for r in recs["labels"]), encoding="utf-8")
        paths[feat] = (v, lab)
    return paths


def _record_files(records_dir: Path) -> list[Path]:
    return sorted(p for p in records_dir.rglob("*.jsonl*") if p.is_file())


def shadow_check(
    *,
    records_dir: Path | str | None = None,
    mode_state_dir: Path | str,
    out_dir: Path | str,
    project: str | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """``derive`` the shadow log into ``out_dir``, then evaluate every gate feature's mode-state. Read-only on the
    log and on the mode-state files. A feature with no records reports its refusal instead of a result."""
    from cisternal.shadow.mode_state import GateCheckError, check_mode_state

    if records_dir is None:
        res = resolve_shadow_log_dir()
        if res.path is None:
            raise ShadowConfigError(f"no shadow log configured ({res.source}); set {ENV_VAR}")
        records_dir = res.path
    msd = Path(mode_state_dir).resolve()
    if repo_root is None and msd.name == "mode_state" and msd.parent.name == ".praxia":
        repo_root = (
            msd.parent.parent
        )  # the enforcer path in the TOMLs is relative to the repo holding them
    derived = derive(read_records(_record_files(Path(records_dir))), project=project)
    files = write_derived(derived, out_dir)
    results: dict[str, Any] = {}
    for feat, (vf, lf) in files.items():
        toml = Path(mode_state_dir) / f"{feat}.toml"
        try:
            r = check_mode_state(toml, verdicts_file=vf, labels_file=lf, repo_root=repo_root)
            results[feat] = r.to_dict()
        except GateCheckError as e:
            results[feat] = {"refused": str(e), "exit_code": e.exit_code}
    return {
        "records_dir": str(records_dir),
        "feature_version": derived["feature_version"],
        "skipped": derived["skipped"],
        "n_used": len(derived["used"]),
        "features": results,
    }
