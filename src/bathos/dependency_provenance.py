"""Per-run provenance of named installed dependencies (debt #2946).

The runs table already records the project's own git state, ``script_sha256`` and a
whole-lockfile ``dependency_lock_sha256``. None of those say WHICH build of a named
dependency (e.g. aminx) executed: the lockfile hash only says "something drifted". A
project opts in by listing packages under ``track_packages``; every run then records,
per package, flat scalar keys in the ``dependency_provenance`` JSON column:

    <pkg>_version              installed distribution version
    <pkg>_wheel_sha256         sha256 of the wheel that was installed
    <pkg>_wheel_sha256_source  where that hash came from (direct_url | wheel_file | uv.lock)
    <pkg>_source_commit        VCS commit the code was built from
    <pkg>_module_path          file `import <module>` resolves to
    <pkg>_module_in_dist       true only if that file belongs to the distribution

plus ``probe_python`` (the interpreter probed) and ``probe_status``. A value that cannot be
established is the literal string ``"unknown"`` -- never guessed.

The probe has to run in the SCRIPT's interpreter, not bathos's: ``bth`` is usually a uv
tool with its own venv, so ``importlib.metadata`` in this process describes the wrong
environment entirely. The probe uses ``importlib.util.find_spec``, which resolves the
module ``import`` would load under the script's ``sys.path`` without executing it.

Why ``module_in_dist`` exists: ``importlib.metadata`` resolves by distribution NAME, not by
the module actually imported. With a dist installed while ``sys.path`` prefers a vendored or
source copy, ``version("aminx")`` reports the installed dist's version for code that never
ran (reference: tev_design ``scripts/tev_design/aminx_provenance.py::resolve_aminx_version``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"

#: Wall-clock ceiling for the probe subprocess. Generous because `uv run` may sync the
#: environment on first use; the script's own `uv run` then finds it already synced.
PROBE_TIMEOUT_S = 300.0


class DependencyProvenanceConfigError(ValueError):
    """``track_packages`` is malformed.

    Raised and never swallowed, like ``EnforcementConfigError``: a typo in a setting that
    opts INTO provenance must not silently record nothing.
    """


_ENTRY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(:[A-Za-z_][A-Za-z0-9_]*)?$")


def _parse_track_packages(value: object, where: str) -> list[str]:
    if not isinstance(value, list):
        raise DependencyProvenanceConfigError(
            f"{where}: track_packages must be a list of distribution names"
        )
    for entry in value:
        if not isinstance(entry, str) or not _ENTRY_RE.match(entry):
            raise DependencyProvenanceConfigError(
                f"{where}: track_packages entry {entry!r} must be a distribution name, "
                "optionally 'dist:module' when the import name differs (e.g. 'scikit-learn:sklearn')"
            )
    return list(value)


def _nearest(start: Path, name: str) -> Path | None:
    for directory in [start, *start.parents]:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _load_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raw = b""
        with contextlib.suppress(OSError):
            raw = path.read_bytes()
        if b"track_packages" in raw:
            # It evidently opted in, so an unreadable file must not silently record nothing.
            raise DependencyProvenanceConfigError(f"cannot parse {path}: {e}") from e
        logger.warning("cannot parse %s (%s); no track_packages setting is applied", path, e)
        return {}


def resolve_tracked_packages(sidecar_path: Path | None, start: Path) -> list[str]:
    """Union, in first-seen order, of ``track_packages`` from (1) the sidecar's
    ``[provenance]`` table, (2) the nearest ``.bth.toml`` ``[provenance]`` table and
    (3) the nearest ``pyproject.toml`` ``[tool.bathos]`` table, searched upward from
    ``start``. Raises :class:`DependencyProvenanceConfigError` on a malformed setting.
    """
    sources: list[tuple[str, object]] = []
    if sidecar_path is not None and sidecar_path.is_file():
        section = _load_toml(sidecar_path).get("provenance", {})
        if isinstance(section, dict) and "track_packages" in section:
            sources.append((f"{sidecar_path} [provenance]", section["track_packages"]))
    bth_toml = _nearest(start, ".bth.toml")
    if bth_toml is not None:
        section = _load_toml(bth_toml).get("provenance", {})
        if isinstance(section, dict) and "track_packages" in section:
            sources.append((f"{bth_toml} [provenance]", section["track_packages"]))
    pyproject = _nearest(start, "pyproject.toml")
    if pyproject is not None:
        section = _load_toml(pyproject).get("tool", {}).get("bathos", {})
        if isinstance(section, dict) and "track_packages" in section:
            sources.append((f"{pyproject} [tool.bathos]", section["track_packages"]))
    seen: dict[str, None] = {}
    for where, value in sources:
        for entry in _parse_track_packages(value, where):
            seen.setdefault(entry, None)
    return list(seen)


def key_prefix(dist_name: str) -> str:
    """PEP 503-normalised name with separators as ``_``: a JSON path segment DuckDB accepts."""
    return re.sub(r"[-_.]+", "_", dist_name).lower()


# ---------------------------------------------------------------------------
# Probe: stdlib-only source executed by the script's interpreter via `-c`.
# Inputs arrive through env vars, output goes to a file (never stdout, which a
# sitecustomize or a uv banner could pollute).
# ---------------------------------------------------------------------------

_PROBE_SOURCE = r"""
import json, os, sys
from pathlib import Path
path0 = os.environ.get("BTH_PROBE_PATH0")
# `python -c` puts '' (cwd) at sys.path[0]; the script would have had its own directory
# there instead. Mirror that, unless -P/-I (safe_path) means nothing is prepended at all.
if path0 and not getattr(sys.flags, "safe_path", False) and sys.path and sys.path[0] == "":
    sys.path[0] = path0
import importlib.util
from importlib.metadata import PackageNotFoundError, distribution

def top_level_modules(dist):
    try:
        text = dist.read_text("top_level.txt")
    except Exception:
        text = None
    if text:
        return [line.strip() for line in text.splitlines() if line.strip()]
    names = []
    for f in dist.files or []:
        parts = f.parts
        if not parts or parts[0].endswith((".dist-info", ".data")) or parts[0] in ("..", "__pycache__"):
            continue
        name = parts[0][:-3] if len(parts) == 1 and parts[0].endswith(".py") else parts[0]
        if len(parts) == 1 and not parts[0].endswith(".py"):
            continue
        if name.isidentifier() and name not in names:
            names.append(name)
    return names

def probe(entry):
    dist_name, _, module = entry.partition(":")
    out = {"dist": dist_name}
    try:
        dist = distribution(dist_name)
    except PackageNotFoundError:
        dist = None
    norm = dist_name.replace("-", "_").replace(".", "_").lower()
    if not module:
        candidates = top_level_modules(dist) if dist is not None else []
        public = [c for c in candidates if not c.startswith("_")]  # pyyaml: yaml, not _yaml
        if norm in candidates or not candidates:
            module = norm
        else:
            module = (public or candidates)[0]
    out["module"] = module
    try:
        spec = importlib.util.find_spec(module)
    except Exception as e:
        spec = None
        out["find_spec_error"] = f"{type(e).__name__}: {e}"
    locations = []
    if spec is not None:
        if spec.origin and spec.origin not in ("namespace", "built-in", "frozen"):
            locations = [spec.origin]
        elif spec.submodule_search_locations:
            locations = list(spec.submodule_search_locations)  # every namespace portion
    origin = locations[0] if locations else None
    out["module_path"] = str(Path(origin).resolve()) if origin else None
    if dist is None:
        out["installed"] = False
        return out
    out["installed"] = True
    out["version"] = dist.version
    try:
        out["direct_url"] = json.loads(dist.read_text("direct_url.json") or "null")
    except Exception:
        out["direct_url"] = None
    try:
        wheel = dist.read_text("WHEEL") or ""
    except Exception:
        wheel = ""
    out["wheel_tags"] = [l.split(":", 1)[1].strip() for l in wheel.splitlines() if l.startswith("Tag:")]
    try:
        out["record"] = dist.read_text("RECORD")
    except Exception:
        out["record"] = None
    # None (unknown) when the dist lists no files at all (egg-info, distro packages):
    # absence from an empty RECORD is not evidence of shadowing.
    in_record = None
    files = dist.files or []
    if locations and files:
        installed = set()
        for f in files:
            try:
                installed.add(Path(str(dist.locate_file(f))).resolve())
            except Exception:
                continue
        in_record = False
        for loc in locations:
            target = Path(loc).resolve()
            if target in installed or any(target in p.parents for p in installed):
                in_record = True
                break
    out["module_in_record"] = in_record
    return out

def safe_probe(entry):
    try:
        return probe(entry)
    except Exception as e:
        return {"dist": entry.partition(":")[0], "probe_error": f"{type(e).__name__}: {e}"}

entries = json.loads(os.environ["BTH_PROBE_PACKAGES"])
result = {"python": sys.executable, "packages": [safe_probe(e) for e in entries]}
with open(os.environ["BTH_PROBE_OUT"], "w") as fh:
    json.dump(result, fh)
"""

_PYTHON_RE = re.compile(r"^python(\d+(\.\d+)?)?$")
# Python options that consume the NEXT token as their value.
_PYTHON_VALUE_FLAGS = frozenset({"-W", "-X", "-Q"})
# `uv run` options that consume the NEXT token as their value (mirrors runner._find_script_path).
_UV_VALUE_FLAGS = frozenset(
    {
        "--with",
        "--with-editable",
        "--with-requirements",
        "--project",
        "--directory",
        "--python",
        "-p",
        "--extra",
        "--group",
        "--only-group",
        "--no-group",
        "--package",
        "--from",
        "--index",
        "--default-index",
        "--index-url",
        "--extra-index-url",
        "--env-file",
        "--config-file",
        "--cache-dir",
        "--prerelease",
        "--resolution",
    }
)


_INLINE_METADATA_RE = re.compile(r"^# /// script\s*$", re.MULTILINE)


def _has_inline_metadata(script: Path) -> bool:
    try:
        return bool(
            _INLINE_METADATA_RE.search(script.read_text(encoding="utf-8", errors="replace"))
        )
    except OSError:
        return False


def _python_tail(tokens: list[str], cwd: Path) -> tuple[list[str], str | None] | None:
    """For the tokens after a python interpreter, return (options to keep, sys.path[0])."""
    keep: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-m":
            return keep, str(cwd)  # `python -m mod` prepends the cwd
        if tok == "-c":
            return None  # inline code: no script whose environment we can mirror
        if tok in _PYTHON_VALUE_FLAGS:
            keep += tokens[i : i + 2]
            i += 2
            continue
        if tok.startswith("--"):
            keep.append(tok)
            i += 1
            continue
        if tok.startswith("-"):
            # A short-option cluster. A joined `-mpkg.mod` / `-cCODE` / `-um mod` must never be
            # kept: appended before `-c <probe>` it would run the user's module a second time.
            # `-Wignore` / `-Xdev` carry their value joined and are safe to keep.
            if tok[1:2] not in ("W", "X") and ("m" in tok[1:] or "c" in tok[1:]):
                if tok.startswith("-m"):
                    return keep, str(cwd)  # `python -mpkg.mod`
                return None
            keep.append(tok)
            i += 1
            continue
        script = Path(tok) if Path(tok).is_absolute() else cwd / tok
        return keep, str(script.resolve().parent)
    return None


def probe_command(argv: list[str], cwd: Path) -> tuple[list[str], str | None] | None:
    """Rebuild ``argv`` so it runs the probe under the same interpreter and launcher.

    Returns (command prefix to which ``-c <probe>`` is appended, value for sys.path[0]),
    or None when the command is not a recognisable Python invocation.
    """
    from bathos.runner import _strip_wrapper_prefix

    if not argv:
        return None
    # Keep wrapper prefixes (`env FOO=1`, `timeout 900`, nested): env assignments can change sys.path.
    core = argv
    while (wrapped := _strip_wrapper_prefix(core)) is not None and wrapped != core:
        core = wrapped
    prefix = argv[: len(argv) - len(core)]
    if not core:
        return None
    exe = Path(core[0]).name
    if _PYTHON_RE.match(exe.lower()):
        tail = _python_tail(core[1:], cwd)
        if tail is None:
            return None
        keep, path0 = tail
        return prefix + [core[0], *keep], path0
    if exe.lower() == "uv":
        if len(core) < 2 or core[1] != "run":
            return None
        i = 2
        while i < len(core):
            tok = core[i]
            if tok == "--":
                i += 1
                continue
            if tok in _UV_VALUE_FLAGS:
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            break
        uv_prefix = [tok for tok in core[:i] if tok != "--"]
        if i >= len(core) or "--script" in uv_prefix or "-s" in uv_prefix:
            return None
        # A PEP 723 script runs in its own ephemeral environment; `uv run python -c` would
        # probe the project environment instead and record a plausible wrong answer.
        script_tok = next((t for t in core[i:] if t.endswith(".py")), None)
        if script_tok is not None and _has_inline_metadata(
            Path(script_tok) if Path(script_tok).is_absolute() else cwd / script_tok
        ):
            return None
        if _PYTHON_RE.match(Path(core[i]).name.lower()):
            tail = _python_tail(core[i + 1 :], cwd)
            if tail is None:
                return None
            keep, path0 = tail
            return prefix + uv_prefix + [core[i], *keep], path0
        if core[i].endswith(".py"):
            script = Path(core[i]) if Path(core[i]).is_absolute() else cwd / core[i]
            return prefix + uv_prefix + ["python"], str(script.resolve().parent)
        return None
    if core[0].endswith(".py"):
        script = Path(core[0]) if Path(core[0]).is_absolute() else cwd / core[0]
        try:
            first = script.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        except (OSError, IndexError):
            return None
        if not first.startswith("#!"):
            return None
        shebang = first[2:].split()
        if not shebang:
            return None
        if Path(shebang[0]).name == "env":
            shebang = shebang[1:]
        if len(shebang) == 1 and _PYTHON_RE.match(Path(shebang[0]).name.lower()):
            return prefix + [shebang[0]], str(script.resolve().parent)
        return None
    return None


def _run_probe(
    command: list[str], path0: str | None, entries: list[str], cwd: Path
) -> tuple[dict | None, str]:
    with tempfile.TemporaryDirectory(prefix="bth-depprobe-") as tmp:
        out_path = Path(tmp) / "probe.json"
        env = os.environ.copy()
        env["BTH_PROBE_PACKAGES"] = json.dumps(entries)
        env["BTH_PROBE_OUT"] = str(out_path)
        if path0:
            env["BTH_PROBE_PATH0"] = path0
        try:
            proc = subprocess.run(
                [*command, "-c", _PROBE_SOURCE],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=PROBE_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, f"timeout after {PROBE_TIMEOUT_S:.0f}s"
        except OSError as e:
            return None, f"failed to launch: {type(e).__name__}: {e}"
        if proc.returncode != 0:
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
            return None, f"exit {proc.returncode}" + (f": {tail[0]}" if tail else "")
        try:
            return json.loads(out_path.read_text(encoding="utf-8")), "ok"
        except (OSError, json.JSONDecodeError) as e:
            return None, f"unreadable probe output: {e}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 16):
            h.update(chunk)
    return h.hexdigest()


def _file_url_path(url: str) -> Path | None:
    if not url.startswith("file://"):
        return None
    return Path(unquote(urlparse(url).path))


def _wheel_tags_from_filename(filename: str) -> set[str]:
    stem = filename[:-4] if filename.endswith(".whl") else filename
    parts = stem.split("-")
    if len(parts) < 5:
        return set()
    py, abi, plat = parts[-3], parts[-2], parts[-1]
    return {f"{a}-{b}-{c}" for a in py.split(".") for b in abi.split(".") for c in plat.split(".")}


def _payload_record(text: str) -> dict[str, str]:
    """RECORD rows outside ``*.dist-info/`` (path -> hash): the installed payload itself.

    dist-info rows are excluded because installers add INSTALLER, REQUESTED and
    direct_url.json there, and the RECORD row cannot hash itself. Rows with no hash
    (compiled .pyc an installer adds) and ``.data/`` rows (relocated on install, e.g. to
    ``../../bin/``) cannot be paired either side, so they are excluded too.
    """
    rows: dict[str, str] = {}
    for line in text.splitlines():
        path, _, rest = line.partition(",")
        digest = rest.partition(",")[0]
        top = path.split("/", 1)[0]
        if not path or not digest or top.endswith((".dist-info", ".data")) or top == "..":
            continue
        rows[path] = digest
    return rows


def _wheel_matches_install(wheel_path: Path, installed_record: str | None) -> bool | None:
    """True/False if the wheel's payload RECORD equals the installed one; None if unverifiable."""
    import zipfile

    if not installed_record:
        return None
    try:
        with zipfile.ZipFile(wheel_path) as zf:
            names = [n for n in zf.namelist() if n.endswith(".dist-info/RECORD")]
            if len(names) != 1:
                return None
            wheel_record = zf.read(names[0]).decode("utf-8")
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError):
        return None
    return _payload_record(wheel_record) == _payload_record(installed_record)


def _direct_url(facts: dict) -> dict:
    """``direct_url.json`` as a dict; anything malformed reads as absent."""
    value = facts.get("direct_url")
    return value if isinstance(value, dict) else {}


def _url(direct_url: dict) -> str:
    url = direct_url.get("url")
    return url if isinstance(url, str) else ""


def _wheel_sha256(facts: dict, lock: dict | None) -> tuple[str, str]:
    direct_url = _direct_url(facts)
    url = _url(direct_url)
    archive = direct_url.get("archive_info")
    if isinstance(archive, dict):
        # archive_info hashes whatever archive was installed, which may be an sdist.
        if not urlparse(url).path.endswith(".whl"):
            return UNKNOWN, "not_a_wheel_install"
        hashes = archive.get("hashes")
        if isinstance(hashes, dict) and isinstance(hashes.get("sha256"), str):
            return hashes["sha256"], "direct_url"
        legacy = archive.get("hash")
        if isinstance(legacy, str) and legacy.startswith("sha256="):
            return legacy.split("=", 1)[1], "direct_url"
    wheel_path = _file_url_path(url)
    if wheel_path is not None and wheel_path.suffix == ".whl" and wheel_path.is_file():
        # The file at that URL is what exists NOW; a wheel rebuilt in place after install
        # would hash to a plausible wrong value. Trust it only if its RECORD matches the
        # installed one.
        match = _wheel_matches_install(wheel_path, facts.get("record"))
        if match is False:
            return UNKNOWN, "wheel_file_changed_since_install"
        try:
            return _sha256_file(wheel_path), "wheel_file" if match else "wheel_file_unverified"
        except OSError:
            pass
    if direct_url:
        # Not an index install (a local dir, VCS or URL): the lock's registry wheels describe
        # some other artifact, even at the same version string.
        return UNKNOWN, UNKNOWN
    if lock is not None:
        installed = set(facts.get("wheel_tags") or [])
        name = key_prefix(facts["dist"])
        for pkg in lock.get("package", []):
            if key_prefix(str(pkg.get("name", ""))) != name:
                continue
            if str(pkg.get("version")) != facts.get("version"):
                continue
            # Only a wheel whose tags match the installed WHEEL tags is attributable; a lone
            # wheel for another platform (installed here from the sdist) is not.
            matching = [
                w
                for w in pkg.get("wheels", [])
                if isinstance(w, dict)
                and isinstance(w.get("hash"), str)
                and w["hash"].startswith("sha256:")
                and installed
                & _wheel_tags_from_filename(
                    Path(urlparse(str(w.get("url") or w.get("path") or "")).path).name
                    or str(w.get("filename", ""))
                )
            ]
            if len(matching) == 1:
                return matching[0]["hash"].split(":", 1)[1], "uv.lock"
    return UNKNOWN, UNKNOWN


# setuptools-scm / hatch-vcs local label: `+g<sha>`, plus `.dYYYYMMDD` when built dirty.
_LOCAL_GIT_RE = re.compile(r"\+(?:.*\.)?g([0-9a-f]{7,40})(?:\.d(\d{8}))?(?:\.|$)")


def _git_head(src: Path) -> str:
    try:
        head = subprocess.run(
            ["git", "-C", str(src), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if head.returncode != 0:
            return UNKNOWN
        # Untracked files count: a new, uncommitted module can be what gets imported.
        status = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(src), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return UNKNOWN
    sha = head.stdout.strip()
    if status.returncode != 0:
        return UNKNOWN
    return f"{sha}-dirty" if status.stdout.strip() else sha


def _editable_source(facts: dict) -> Path | None:
    direct_url = _direct_url(facts)
    dir_info = direct_url.get("dir_info")
    if not (isinstance(dir_info, dict) and dir_info.get("editable")):
        return None
    return _file_url_path(_url(direct_url))


def _source_commit(facts: dict) -> str:
    vcs_info = _direct_url(facts).get("vcs_info")
    commit = vcs_info.get("commit_id") if isinstance(vcs_info, dict) else None
    if isinstance(commit, str) and commit:
        return commit
    src = _editable_source(facts)
    if src is not None and src.is_dir():
        return _git_head(src)
    m = _LOCAL_GIT_RE.search(str(facts.get("version", "")))
    if m:
        return f"{m.group(1)}-dirty" if m.group(2) else m.group(1)
    return UNKNOWN


def _module_in_dist(facts: dict) -> bool | str:
    module_path = facts.get("module_path")
    if not module_path:
        return UNKNOWN
    if not facts.get("installed"):
        return False  # importable, but no distribution of that name is installed
    if facts.get("module_in_record"):
        return True
    # PEP 660 editable install: the RECORD lists a .pth/finder and the code stays in the
    # source tree. Accept only the package's own location there (flat or src layout), not
    # anything under the checkout: a vendored or build/lib copy inside it is still shadowing.
    src = _editable_source(facts)
    module = str(facts.get("module") or "")
    if src is not None and module:
        root = src.resolve()
        path = Path(module_path)
        for base in (root, root / "src"):
            if path == base / f"{module}.py" or path.is_relative_to(base / module):
                return True
    if facts.get("module_in_record") is None:
        return UNKNOWN
    return False


def _unknown_keys(entry: str) -> dict[str, object]:
    p = key_prefix(entry.partition(":")[0])
    return {
        f"{p}_version": UNKNOWN,
        f"{p}_wheel_sha256": UNKNOWN,
        f"{p}_wheel_sha256_source": UNKNOWN,
        f"{p}_source_commit": UNKNOWN,
        f"{p}_module": UNKNOWN,
        f"{p}_module_path": UNKNOWN,
        f"{p}_module_in_dist": UNKNOWN,
    }


def _load_lock(cwd: Path, probe_python: str) -> dict | None:
    """``cwd/uv.lock``, but only if the probed interpreter is that project's own venv.

    A lock describes the environment uv builds for its project. An interpreter elsewhere
    (``uv run --project X``, another venv, a system python) is not described by it.
    """
    lock_path = cwd / "uv.lock"
    if not lock_path.is_file() or not Path(probe_python).is_relative_to(cwd.resolve()):
        return None
    try:
        return tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def _apply_facts(result: dict[str, object], facts: dict, lock: dict | None) -> None:
    p = key_prefix(str(facts["dist"]))
    if facts.get("probe_error"):
        result[f"{p}_probe_error"] = facts["probe_error"]
        return
    result[f"{p}_module"] = facts.get("module") or UNKNOWN
    result[f"{p}_module_path"] = facts.get("module_path") or UNKNOWN
    result[f"{p}_module_in_dist"] = _module_in_dist(facts)
    if not facts.get("installed"):
        return
    result[f"{p}_version"] = facts.get("version") or UNKNOWN
    sha, source = _wheel_sha256(facts, lock)
    result[f"{p}_wheel_sha256"] = sha
    result[f"{p}_wheel_sha256_source"] = source
    result[f"{p}_source_commit"] = _source_commit(facts)


def capture_dependency_provenance(
    entries: list[str], argv: list[str], cwd: Path
) -> dict[str, object] | None:
    """Flat provenance dict for ``entries`` (see module docstring), or None if none tracked.

    Never raises: provenance capture must not be able to abort the run it describes.
    Any internal failure is recorded as ``probe_status = "error: ..."`` with every value
    left ``"unknown"``.
    """
    if not entries:
        return None
    result: dict[str, object] = {}
    for entry in entries:
        result.update(_unknown_keys(entry))
    try:
        cmd = probe_command(argv, cwd)
        if cmd is None:
            result["probe_python"] = UNKNOWN
            result["probe_status"] = "no_python_interpreter_in_command"
            return result
        command, path0 = cmd
        probed, status = _run_probe(command, path0, entries, cwd)
        result["probe_status"] = status
        if probed is None:
            result["probe_python"] = UNKNOWN
            return result
        probe_python = str(probed.get("python") or "")
        result["probe_python"] = probe_python or UNKNOWN
        lock = _load_lock(cwd, probe_python) if probe_python else None
        for facts in probed.get("packages", []):
            _apply_facts(result, facts, lock)
    except Exception as e:  # noqa: BLE001 - see docstring
        fresh: dict[str, object] = {}
        for entry in entries:
            fresh.update(_unknown_keys(entry))
        fresh["probe_python"] = result.get("probe_python", UNKNOWN)
        fresh["probe_status"] = f"error: {type(e).__name__}: {e}"
        return fresh
    return result


def shadowed_packages(provenance: dict[str, object] | None, entries: list[str]) -> list[str]:
    """Tracked packages whose imported module is NOT the installed distribution's."""
    if not provenance:
        return []
    return [
        e.partition(":")[0]
        for e in entries
        if provenance.get(f"{key_prefix(e.partition(':')[0])}_module_in_dist") is False
    ]
