from typer.testing import CliRunner

runner = CliRunner()


def test_decorator_records_run(tmp_path, monkeypatch):
    """@bth.experiment writes a Run to the catalog."""
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "test_proj")
    monkeypatch.chdir(tmp_path)

    from bathos.decorators import experiment
    from bathos.query import list_runs

    @experiment
    def my_fn():
        return 42

    result = my_fn()
    assert result == 42

    runs = list_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].project_slug == "test_proj"
    assert runs[0].status == "completed"
    assert runs[0].exit_code == 0


def test_decorator_records_git_dirty_content_id_and_provenance_source(tmp_path, monkeypatch):
    """@bth.experiment must populate git_dirty_content_id/git_provenance_source on the
    recorded Run, not just git_hash/git_branch/git_dirty -- these two v15 fields come from
    the same GitState capture_git_state() already returns, so omitting them from the Run(...)
    call here would silently lose data every decorator-based run captures correctly
    (task 260820_bathos-git-sync)."""
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "test_proj")
    monkeypatch.chdir(tmp_path)

    monkeypatch.setenv("MYXCEL_PROVENANCE_SCHEMA", "1")
    monkeypatch.setenv("MYXCEL_PROVENANCE_STATUS", "git")
    monkeypatch.setenv("MYXCEL_GIT_SHA", "a" * 40)
    monkeypatch.setenv("MYXCEL_GIT_BRANCH", "feature")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY", "1")
    monkeypatch.setenv("MYXCEL_GIT_DIRTY_CONTENT_ID", "tree:" + "b" * 40)
    monkeypatch.setenv("MYXCEL_PROVENANCE_ROOT", str(tmp_path))

    from bathos.decorators import experiment
    from bathos.query import list_runs

    @experiment
    def my_fn():
        return 42

    my_fn()

    runs = list_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].git_provenance_source == "myxcel-env"
    assert runs[0].git_dirty_content_id == "tree:" + "b" * 40


def test_decorator_records_failure(tmp_path, monkeypatch):
    """@bth.experiment records failed runs on exception."""
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "test_proj")

    from bathos.decorators import experiment
    from bathos.query import list_runs

    @experiment
    def bad_fn():
        raise ValueError("boom")

    from contextlib import suppress

    with suppress(ValueError):
        bad_fn()

    runs = list_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].exit_code == 1


def test_decorator_captures_function_name(tmp_path, monkeypatch):
    """@bth.experiment uses function name as command."""
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "proj")

    from bathos.decorators import experiment
    from bathos.query import list_runs

    @experiment
    def run_nvt_stability():
        pass

    run_nvt_stability()

    runs = list_runs(tmp_path)
    assert "run_nvt_stability" in runs[0].command


def test_decorator_preserves_function_name():
    """@bth.experiment preserves __name__ and __doc__."""
    from bathos.decorators import experiment

    @experiment
    def my_fn():
        """My docstring."""
        pass

    assert my_fn.__name__ == "my_fn"
    assert my_fn.__doc__ == "My docstring."


def test_decorator_no_project_slug_skips_recording(tmp_path, monkeypatch):
    """@bth.experiment skips recording (warns) if BTH_PROJECT_SLUG not set."""
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path))
    monkeypatch.delenv("BTH_PROJECT_SLUG", raising=False)

    from bathos.decorators import experiment
    from bathos.query import list_runs

    @experiment
    def my_fn():
        return 99

    result = my_fn()
    assert result == 99
    assert list_runs(tmp_path) == []


def test_bth_experiment_importable():
    """import bathos; bathos.experiment is the decorator."""
    import bathos

    assert hasattr(bathos, "experiment")
    assert callable(bathos.experiment)
