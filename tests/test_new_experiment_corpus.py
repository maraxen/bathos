"""Tests for corpus-aware scaffolding (build-order step 5)."""

from __future__ import annotations

import tomllib

from bathos.new_experiment import scaffold_experiment


def _scaffold(tmp_path):
    (tmp_path / "scripts" / "experiments").mkdir(parents=True)
    scaffold_experiment("fit_thing", tmp_path)
    return (tmp_path / "scripts" / "experiments" / "fit_thing.bth.toml").read_text()


def test_scaffold_still_parses_as_toml(tmp_path):
    """Guidance is appended as comments — it must not break the file it annotates."""
    tomllib.loads(_scaffold(tmp_path))


def test_scaffold_lists_the_shipped_cards(tmp_path):
    from bathos.corpus import load_corpus

    text = _scaffold(tmp_path)
    for card in load_corpus().cards:
        assert card.id in text, f"{card.id} missing from scaffolded guidance"


def test_scaffold_points_at_the_targeted_command(tmp_path):
    text = _scaffold(tmp_path)
    assert "bth ref show" in text
    assert "bth ref applicable" in text


def test_scaffold_survives_an_unreadable_corpus(tmp_path, monkeypatch):
    """A scaffold must not fail because the corpus cannot be read."""
    import bathos.corpus as corpus_mod

    def boom(*_a, **_k):
        raise RuntimeError("corpus unavailable")

    monkeypatch.setattr(corpus_mod, "load_corpus", boom)
    text = _scaffold(tmp_path)
    tomllib.loads(text)  # still valid, just without guidance
