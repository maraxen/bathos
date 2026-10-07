"""prose_linter_smoke_test.py — verify prose_linter rules against known fixtures.

Each test fixture is a (text, expected_rule_ids, should_be_clean) tuple.
Run directly:
    python scripts/prose_linter_smoke_test.py

Exits 0 if all pass, 1 if any fail.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Allow running from repo root without install
sys.path.insert(0, str(Path(__file__).parent))
from prose_linter import IssueKind, lint_file

PASS = "✓"
FAIL = "✗"


def run_fixture(
    label: str,
    text: str,
    expect_rules: set[str],
    expect_clean: bool = False,
) -> bool:
    """Run lint_file on text, check expected rules fire (or nothing fires)."""
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
        f.write(text)
        path = Path(f.name)

    issues = lint_file(path, window=6, density_threshold=0.03)
    path.unlink()

    found_rules = {i.rule_id for i in issues if i.kind != IssueKind.DENSITY}

    if expect_clean:
        ok = len(found_rules) == 0
        status = PASS if ok else FAIL
        missed = found_rules
        print(f"  {status} [clean] {label}")
        if not ok:
            print(f"       unexpected hits: {missed}")
        return ok

    missing = expect_rules - found_rules
    unexpected = found_rules - expect_rules
    ok = not missing  # unexpected hits are tolerated (supersets are fine)
    status = PASS if ok else FAIL
    print(f"  {status} {label}")
    if missing:
        print(f"       MISSING rules: {missing}")
    if unexpected:
        print(f"       extra rules (ok): {unexpected}")
    return ok


def main() -> int:
    results: list[bool] = []

    print("\n── TERM rules ──────────────────────────────────────────────────────")

    results.append(run_fixture(
        "dead-bio: 'catalytically dead' fires",
        "The catalytically dead variant was used as a negative control.",
        {"dead-bio"},
    ))
    results.append(run_fixture(
        "dead-bio: 'dead code' does NOT fire",
        "This is dead code that was never called.",
        set(),
        expect_clean=True,
    ))
    results.append(run_fixture(
        "dead-bio: 'dead-end' does NOT fire",
        "This optimization turned out to be a dead-end path.",
        set(),
        expect_clean=True,
    ))
    results.append(run_fixture(
        "bio-variant-term: 'mutant' fires",
        "The S219V mutant showed increased resistance to autoproteolysis.",
        {"bio-variant-term"},
    ))
    results.append(run_fixture(
        "bio-variant-term: 'single-mutant' exempt",
        "single-mutant coverage was computed across all positions.",
        set(),
        expect_clean=True,
    ))
    results.append(run_fixture(
        "neo-substrate-framing fires",
        "This approach constitutes gain-of-function engineering of the enzyme.",
        {"neo-substrate-framing"},
    ))
    results.append(run_fixture(
        "evolution-verb fires",
        "The WT was out-evolved by the new design in under 10 generations.",
        {"evolution-verb"},
    ))
    results.append(run_fixture(
        "viral-origin fires",
        "Tobacco Etch Virus protease cleaves the recognition sequence ENLYFQ/S.",
        {"viral-origin"},
    ))
    results.append(run_fixture(
        "viral-origin: bibliography line exempt",
        "| A13 | Directed Evolution of Tobacco Etch Virus Protease | doi.org/10.1234 |",
        set(),
        expect_clean=True,
    ))
    results.append(run_fixture(
        "structural-perturbation fires",
        "Core residues were destabilized by the S219V substitution.",
        {"structural-perturbation"},
    ))
    results.append(run_fixture(
        "specificity-framing fires",
        "Our goal is to enhance the activity of the protease toward new substrates.",
        {"specificity-framing"},
    ))
    results.append(run_fixture(
        "perturbation-scale fires on 'core damage'",
        "Core damage was assessed by measuring the thermal denaturation temperature.",
        {"perturbation-scale"},
    ))
    results.append(run_fixture(
        "directed-evolution-framing fires",
        "We used directed evolution to identify variants with shifted specificity.",
        {"directed-evolution-framing"},
    ))
    results.append(run_fixture(
        "directed-evolution-framing: blockquote exempt",
        "> Directed evolution has previously been applied to alter TEV protease specificity.",
        set(),
        expect_clean=True,
    ))
    results.append(run_fixture(
        "directed-evolution-framing: citation exempt",
        "Esvelt et al. (2011) introduced directed evolution for biomolecules.",
        set(),
        expect_clean=True,
    ))
    results.append(run_fixture(
        "catalytic-efficiency fires",
        "The potency of this enzyme variant exceeds WT by threefold.",
        {"catalytic-efficiency"},
    ))

    print("\n── COMPOUND rules ──────────────────────────────────────────────────")

    results.append(run_fixture(
        "compound:viral-origin+design: 'virus' + 'design' within window",
        "\n".join([
            "TEV protease (from Tobacco Etch Virus) is well characterised.",
            "We therefore chose to design new variants of this enzyme.",
        ]),
        {"compound:viral-origin+design"},
    ))
    results.append(run_fixture(
        "compound:neo-substrate+protease fires",
        "\n".join([
            "Gain-of-function activity was detected on three non-canonical substrates.",
            "The protease was then characterized kinetically.",
        ]),
        {"compound:neo-substrate+protease"},
    ))
    results.append(run_fixture(
        "compound:bio-variant-term+activity fires",
        "\n".join([
            "The S219V mutant retained 85% of WT-level activity.",
        ]),
        {"compound:bio-variant-term+activity"},
    ))
    results.append(run_fixture(
        "compound:structural-perturbation+core fires",
        "\n".join([
            "The buried core was destabilized by three substitutions.",
        ]),
        {"compound:structural-perturbation+core"},
    ))
    results.append(run_fixture(
        "compound: terms outside window do NOT fire",
        "\n".join(
            ["The virus was used as a template."]
            + ["Unrelated content line." for _ in range(10)]
            + ["We designed new sequences here."]
        ),
        set(),
        expect_clean=True,
    ))

    print("\n── DENSITY ─────────────────────────────────────────────────────────")

    # 10 lines, 4 flagged = 40% → well above 3% threshold
    dense_text = "\n".join([
        "The S219V mutant showed gain-of-function neo-substrate activity.",
        "This catalytically dead control was destabilized at the core.",
        "Directed evolution outpaced rational design by 2x.",
        "The Tobacco Etch Virus protease was out-evolved by our design.",
        "Filler line.",
        "Filler line.",
        "Filler line.",
        "Filler line.",
        "Filler line.",
        "Filler line.",
    ])
    dense_issues = []
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
        f.write(dense_text)
        dense_path = Path(f.name)
    dense_issues = lint_file(dense_path, density_threshold=0.03)
    dense_path.unlink()
    density_fired = any(i.kind == IssueKind.DENSITY for i in dense_issues)
    print(f"  {'✓' if density_fired else '✗'} DENSITY fires on high-density text")
    results.append(density_fired)

    # Clean text — density should NOT fire
    clean_text = "\n".join(["Filler line with no flags." for _ in range(20)])
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
        f.write(clean_text)
        clean_path = Path(f.name)
    clean_issues = lint_file(clean_path, density_threshold=0.03)
    clean_path.unlink()
    density_clean = not any(i.kind == IssueKind.DENSITY for i in clean_issues)
    print(f"  {'✓' if density_clean else '✗'} DENSITY does NOT fire on clean text")
    results.append(density_clean)

    print()
    passed = sum(results)
    total = len(results)
    if all(results):
        print(f"✓ All {total} smoke tests passed.")
        return 0
    else:
        print(f"✗ {total - passed}/{total} smoke tests FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
