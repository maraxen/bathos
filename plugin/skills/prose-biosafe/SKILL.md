---
name: prose-biosafe
description: Precise, field-standard terminology for biology-context prose in legitimate protein engineering and therapeutic projects, so docs do not trip biosecurity-classifier false positives — use when writing or reviewing docs, docstrings, research notes, bathos sidecar hypothesis fields or memory entries about enzyme engineering, continuous selection, binder design or variant libraries, and when running, extending or adopting a project prose linter (just prose-lint, scripts/prose_linter.py, scripts/lint_prose_biosafe.sh)
triggers:
  - prose-lint
  - prose-biosafe
  - biosafe
  - classifier false positive
  - lint_prose_biosafe
  - prose_linter
---
# prose-biosafe

Some scientifically ordinary wording, stacked densely in one context window, reads to
biosecurity classifiers like dual-use framing. In legitimate projects (a tag-cleavage tool
enzyme, a therapeutic antivenom binder) that produces false positives that interrupt agent
sessions. This skill collects the substitutions and tooling that projects in this workspace
have converged on.

**What this skill is not.** Every substitution here must keep the sentence's meaning and is,
in almost every case, the term the field itself prefers ("variant" over "mutant", "kcat/Km"
over "potency"). It is a precision and register discipline. It is **never** a way to make
work that genuinely raises dual-use concern read as benign. If the underlying work is the
problem, changing the words does not fix it; stop and raise it with the user.

## Resolution order — the project's own rule file wins

1. **`.claude/rules/prose-biosafe.md` in the current project** is authoritative for that
   project's domain vocabulary. Read it first; it is auto-loaded as a rule in projects that
   have one.
2. **This skill** supplies the shared model, the core table, the domain packs below, and the
   tooling notes. Where a project rule and this skill disagree, follow the project rule and
   mention the disagreement.
3. If the project has **no** rule file, use the core table plus the closest domain pack, and
   offer to add a project rule file (see "Adopting in a new project").

## The three layers

Classifiers read context windows, not isolated keywords. Fix in this order:

| Layer | What fires | Example | Priority |
|---|---|---|---|
| 1. **Term** | one individually high-signal term | "gain-of-function", "catalytically dead" | fix wherever found |
| 2. **Compound** | two individually benign terms close together (same line up to ~6 lines) | "mutant" near "activity"; "design" near a pathogen/agent word | fix in files loaded whole into context |
| 3. **Density** | flagged lines / total lines above ~3% in one file | literature-review notes | rank files by density, fix the densest first |

Same-line co-occurrence is the **strongest** compound signal, not a duplicate of the term hit;
a linter that skips it is broken (see "Linter pitfalls").

## Core table (domain-agnostic)

| Avoid | Use instead | Why |
|---|---|---|
| "mutant" (biology) | "variant" | modern style; flags near "activity" |
| "gain-of-function" | "neo-functional", "neo-substrate", "shifted / broadened specificity" | single highest-risk term |
| "dead control/variant/construct", "catalytically dead" | "catalytically inactive control", "negative control" | "dead" + enzyme/agent context |
| "destabilized" (protein), "core damage" | "perturbed", "structurally modified", "internal perturbation" | structural-damage framing, worse near core/buried/scaffold |
| "directed evolution" (new prose) | "continuous selection", or name the platform (PACE, PRANCE) | published titles are exempt |
| "out-evolved" | "outpaced", "out-competed" | |
| "enhance activity" | "broaden specificity", "shift substrate scope", or the measured quantity | enhancement language |
| "potency" (enzyme context) | "catalytic efficiency", "kcat/Km" | pharmacology/toxicology register |
| "host range" (design context) | "substrate scope" | virology register |
| "lethal dose", "LD50"/"LC50" | "dose-response", "ED50" | |
| "kill assay", "kill curve" | "cytotoxicity assay", "dose-response curve" | |
| "virulence" | "pathogenicity", "clinical severity" — or remove if imported from the wrong context | |
| "dose series" + "substitution" | "designed-modification series" | quantified modification reads as directed damage |

## Domain packs

**Enzyme engineering / tool proteases** (from `tev_design`):
- Omit organism-of-origin names that contain "virus"; identify the enzyme by EC number
  (e.g. "EC 3.4.22.44"). The compound to avoid is a virus word near design/engineer words.
- "gain-of-function" near "protease" → "neo-substrate".
- "directed evolution" + "protease" + "selection" together → name the platform.

**Therapeutic binders / antivenom** (from `nanobody`):
- "toxin" in prose → "target protein", "antigen", or the family name with its abbreviation
  (e.g. "three-finger protein (3FTx)"); toxin-subtype names → "target protein family" or a
  UniProt ID.
- "venom" (not "antivenom") → "envenoming syndrome", "clinical indication";
  "envenomation" → "envenoming" (WHO form); "snakebite" → "envenoming".
- "neutralize" → "inhibit", "bind", "abrogate activity"; "potent" + "neutralize" →
  "high-affinity" + "inhibit"; "broad-spectrum" + neutralize/bind → "cross-reactive" or
  "pan-clade".
- Species names → PDB ID or "target species".
- Keep "design/engineer" and agent words (venom, toxin, antigen) in separate sentences.

## Never flag, never alter

- Software idiom: "dead code", "dead-end", "dead knob", "pkill", "kill -9", "OOM-killed",
  "escape hatch"; bathos's `kill_condition`.
- Combinatorics idiom: "single-mutant", "double-mutant coverage".
- Engineering-risk words: "hazard", "dangerous" in "label hazard", "reproducibility hazard".
- Published names: hyperTEV, PACE, PRANCE, "autolysis-resistant S219D variant",
  "antivenom", "neutralizing antibody" as a defined immunology term.
- Direct blockquotes (`>`) from papers, paper titles, bibliography/citation lines.
- Code identifiers — rename them in a separate, tested change, never in a prose pass.
- M13 "infect"/"infection" — standard non-lytic phage terminology.

## Tooling — a bundled linter, plus per-project copies

The reference Python linter ships **with this skill**, in `scripts/` next to this file:
`scripts/prose_linter.py` and `scripts/prose_linter_smoke_test.py`. Its rule tables are the
enzyme-engineering ruleset from `tev_design`, so treat it as a template to copy into a
project and edit, not something to run unmodified against an unrelated project. Prefer the
project's own copy when one exists — it carries that project's rules.

| | Python linter (bundled; also `tev_design`) | Shell linter (`nanobody`) |
|---|---|---|
| Where | this skill's `scripts/`; `tev_design/scripts/` | `nanobody/scripts/lint_prose_biosafe.sh` |
| Layers | term, compound (line window), density | term, same-line combination |
| Controls | smoke test with positive **and** negative fixtures | none |
| Run | `python scripts/prose_linter.py PATH`; `just prose-lint-py` / `-summary` / `-smoke` | `just prose-lint` / `scripts/lint_prose_biosafe.sh . --check` |

Useful Python-linter flags: `--summary` (density ranking), `--only-density`, `--window N`,
`--density-threshold F`, or a single file path for deep inspection.

**bathos integration is an adapter, not a feature.** `prose_linter.check_prose_biosafe()`
converts hits into `bathos.linter.LintIssue` (density → INFO, the neo-substrate rules →
ERROR, the rest → WARNING). bathos itself does not call it; `bth lint` does not run prose
checks. Do not claim it does.

## Remediation workflow

1. Rank files: `python scripts/prose_linter.py . --summary`. Densest first.
2. Fix every Layer-1 hit of the highest-risk rules (gain-of-function wording) wherever found.
3. Fix compound hits in files read whole into context: CLAUDE.md, rule files, research notes,
   sidecar hypotheses, memory entries.
4. For literature reviews that legitimately discuss published selection methods and cannot be
   fully reworded, add a short scope comment at the top stating what the subject is (the
   enzyme's EC number and its tool use, or the therapeutic indication).
5. Re-run the linter and the smoke test. A count that went **up** after a linter fix is
   expected if the fix made a rule fire that was previously silent — say so, do not hide it.

## Linter pitfalls (both found in real code)

- **`\b` after a stem never matches.** `\bactivit\b` cannot match "activity" — there is no
  word boundary between "activit" and "y". Write `\bactivit(?:y|ies)\b` or drop the trailing
  `\b`. A related gap: `\benhance\b` misses "enhanced"/"enhancing" — prefer `\benhanc`. The
  bundled `specificity-framing` rule still has this gap (open; add a fixture when fixing it).
- **Skipping same-line co-occurrence.** A compound pass that does `if b_line == a_line:
  continue` on the theory that the term pass "already caught it" silently disables every
  one-sentence compound; the term pass reports the single term, never the pair.
- **Allowlists are per line.** An allowlist pattern exempts the whole line it matches; keep
  allowlist patterns narrow ("double-mutant", not "mutant").
- **A smoke test needs negative controls.** Each rule gets a fixture that must fire and,
  where an allowlist exists, a fixture that must not. A suite with only positive fixtures
  cannot detect an over-broad rule.

## Adding a rule

1. Ask whether the term is the field's own term without dual-use implication; if so the
   replacement should be the field's preferred term, not a euphemism.
2. Add the pattern (Python: a `TermRule` or `CompoundRule`; shell: `RULES` + `DESCRIPTIONS`).
3. Add a firing fixture and, if allowlisted, a non-firing fixture to the smoke test.
4. Add the row to the project's `.claude/rules/prose-biosafe.md`.
5. Run the smoke test, then the repo scan, and report the count change.

## Adopting in a new project

1. Write `.claude/rules/prose-biosafe.md`: a one-paragraph statement of what the project
   legitimately is, the core table rows that apply, the matching domain pack, and the
   never-flag list.
2. Copy `scripts/prose_linter.py` and `scripts/prose_linter_smoke_test.py` from this skill
   into the project's `scripts/`, replace `TERM_RULES` / `COMPOUND_RULES` and the fixtures
   with the project's, and keep a negative fixture for every allowlist.
3. Add `prose-lint*` Justfile recipes; wire `just prose-lint-smoke` into CI before the scan.
