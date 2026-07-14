"""Self-contained (stdlib-only) subprocess adapters spawned via bathos.runner.run_script.

Modules in this package deliberately do NOT ``import bathos`` — they are
designed to execute inside a *target project's* uv-managed venv (via
``sys.executable``, no dependency resolution needed for the adapter itself),
not bathos's own. This mirrors the existing "target script writes JSON to
$BTH_RESULTS_PATH, never imports bathos" convention used throughout
bathos.runner (see run_script's docstring / _read_result_emission).
"""
