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


def test_a_card_title_with_a_newline_cannot_corrupt_the_sidecar(tmp_path, monkeypatch):
    """An embedded newline would split the comment line, leaving a second line with no '#'."""
    from bathos.corpus import Card, CorpusLoad

    evil = Card(
        id="EVIL-1",
        title="line one\nline two = broken",
        path=tmp_path / "x" / "EVIL-1.md",
        body="b",
        severity="warning",
    )
    monkeypatch.setattr("bathos.corpus.load_corpus", lambda *_a, **_k: CorpusLoad(cards=[evil]))
    text = _scaffold(tmp_path)
    tomllib.loads(text)  # must still parse
    assert "line two = broken" in text and "\n line two" not in text
