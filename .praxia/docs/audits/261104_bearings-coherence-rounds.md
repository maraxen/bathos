# Adversarial Spec-Coherence Round Log

**Target File**: `.praxia/docs/specs/261104_bearings-multiscale-goal-awareness.md`  
**Task ID**: `261104_multiscale-goal-awareness`  
**Workflow**: Adversarial spec-coherence cycles (Challenger → Defender → Adjudicator → Editor)

---

## Round 1

**Audit Date**: 2026-11-04  
**Status**: **NOT CONVERGED** (7 MAJOR findings accepted)

### 1. Executive Summary

Round 1 evaluated the initial draft of `261104_bearings-multiscale-goal-awareness.md`. Challenger identified 10 findings (2 claimed FATAL, 5 MAJOR, 3 MINOR) and 2 Owner Questions. Defender evaluated each finding against repository source code and prior research, conceding real textual and coverage gaps while rebutting FATAL severity designations. Adjudicator reviewed all findings, source code citations, git logs, and arguments, ruling that 7 findings were MAJOR and 3 were MINOR (with both claimed FATAL findings downgraded to MAJOR). Editor applied all 10 accepted findings cleanly to the target specification.

Because 7 MAJOR findings were accepted in this round, Round 1 did not achieve convergence.

---

### 2. Challenger Findings (Round 1)

1. **R1-C01: Unreconciled Pivot Contradiction: Refusal vs Recording**
   - **Section**: Winner (line 9), Wrapper (line 150) vs Design Item 3 (line 106) and AC-2 (line 127).
   - **Severity**: FATAL (claimed).
   - **Quoted Text**: Winner/Wrapper line 9/150: `"A pivot that skips this is recorded as unreconciled."` vs Design Item 3 line 106: `"Skipping a live purpose is refused. This is a completeness rule, not a judgment."` and AC-2 line 127: `"Given a confirmed charter with live purposes P1 and P2, when bth charter pivot names only P1, then it is refused and lists P2."`
   - **Evidence**: `.praxia/docs/specs/261104_bearings-multiscale-goal-awareness.md:9, 106, 127, 150`.
   - **Description**: Winner and Wrapper specify that skipping a live purpose is "recorded as unreconciled", whereas Design Item 3 and AC-2 specify it is strictly "refused" and halted without recording.

2. **R1-C02: AC-7 Positive Control Self-Invalidation via Over-Broad Negative Grep (`drift`, `identity`)**
   - **Section**: AC-7 (lines 136-138) vs Design Item 4 (line 111).
   - **Severity**: FATAL (claimed).
   - **Quoted Text**: AC-7 lines 136-138: `"Negative check: a grep of the built packet for charter, refoundation, drift, identity and purpose ledger must return nothing. If it does, the control is contaminated and the run is invalid."`
   - **Evidence**: Commit `323e782a609428e309cc5943a750f279f8325eda` (2026-06-11) in colliculix git history reachable from cut `14c3804` contains `"drift-pinning research"` in its subject. Design Item 4 includes git log subjects in the packet.
   - **Description**: The unanchored grep for common words like `"drift"` and `"identity"` causes the positive control to unconditionally fail and flag as contaminated.

3. **R1-C03: Design Item 7 (`bth bearings due` / Cadence) Completely Untested by Any AC**
   - **Section**: Design Item 7 (line 119) vs Acceptance Criteria AC-1..AC-8 (lines 122-143).
   - **Severity**: MAJOR.
   - **Quoted Text**: Design Item 7 line 119: `"7. Cadence. bth bearings due lists projects where a reading is due: active in the last 28 days and either 20 new runs or 7 days since the last reading. These numbers are starting values with no empirical basis, and T1 revisits them."`
   - **Evidence**: Exhaustive audit of AC-1..AC-8 shows zero coverage of `bth bearings due` or cadence parameters.
   - **Description**: Requirement 1 mandates every Design item 1-7 is tested by at least one AC. Design Item 7 had no corresponding test assertion.

4. **R1-C04: Contradictory Status of Argument & Multiverse Seam (Out of v1 / Deferred vs AC-3 & Design Items 1 & 5)**
   - **Section**: Winner (line 9), Decision Log (line 60) vs Design Items 1 & 5 (lines 100, 115) and AC-3 (line 128).
   - **Severity**: MAJOR.
   - **Quoted Text**: Winner line 9 / Decision Log line 60: `"the argument and multiverse seam"` listed as Out of v1 / deferred, while Design Items 1 & 5 and AC-3 specify and test preserving the seam (`[purpose.<id>.ext]` tables and `atom:` citations).
   - **Evidence**: `.praxia/docs/specs/261104_bearings-multiscale-goal-awareness.md:9, 60, 100, 115, 128`.
   - **Description**: Terminology conflation between the passive *seam* (which is in v1) and downstream *features* (which are deferred). Violates rule that no AC depends on something out of v1.

5. **R1-C05: Non-Existent and Conflicting Citations for Excess-Spend Metric (`261027:146` vs `261027 §8`)**
   - **Section**: Decision Log (line 58) and AC-8 (line 142).
   - **Severity**: MAJOR.
   - **Quoted Text**: Decision Log line 58 cites `(261027:146)` and AC-8 line 142 cites `(261027 §8, including chains that paid off)`.
   - **Evidence**: `261027:146` discusses silently inert gates/knobs; `261027 §8` discusses reviewer independence governance. Neither defines excess spend. The actual source is `src/bathos/stepback/analysis.py:667-675`.
   - **Description**: Phantom citations fail to resolve at source.

6. **R1-C06: Inclusion of Handoffs in Winner & Wrapper Contradicts Open TBD T2 and Design Item 4**
   - **Section**: Winner (line 9), Wrapper (line 150) vs TBDs (line 88) and Design Item 4 (line 110).
   - **Severity**: MAJOR.
   - **Quoted Text**: Winner/Wrapper lines 9/150 list `handoffs` as primary records read by cold reader; TBD T2 line 88 reserves inclusion of narrative handoffs as an open Owner question; Design Item 4 line 110 scopes docs `"scope per T2"`.
   - **Evidence**: `.praxia/docs/specs/261104_bearings-multiscale-goal-awareness.md:9, 88, 110, 150`.
   - **Description**: Winner and Wrapper prematurely settled an open owner decision.

7. **R1-C07: AC-4 Depends on Open TBD T2 without Naming T2**
   - **Section**: AC-4 (lines 129-134) vs TBD T2 (line 88).
   - **Severity**: MAJOR.
   - **Quoted Text**: AC-4 specifies packet construction and determinism without mentioning T2, despite T2 stating it must be settled before Packet builder (AC-4).
   - **Evidence**: `.praxia/docs/specs/261104_bearings-multiscale-goal-awareness.md:88, 129-134`.
   - **Description**: Rule 2 requires any AC depending on an open TBD to explicitly name that TBD.

8. **R1-C08: `bth charter status` Tested in AC-1 but Not Defined in Design**
   - **Section**: AC-1 (line 126) vs Design Items 1-3 (lines 97-107).
   - **Severity**: MINOR.
   - **Quoted Text**: AC-1 line 126 tests `bth charter status` reports `unconfirmed` and names the last confirmed sha256, but `bth charter status` was not explicitly introduced in Design Items 1-3.
   - **Evidence**: `.praxia/docs/specs/261104_bearings-multiscale-goal-awareness.md:97-107, 126`.
   - **Description**: AC-1 tests an interface command not defined in Design.

9. **R1-C09: Human-Only Authorization Check on `bth bearings label` (Design Item 6) Untested in AC-6**
   - **Section**: Design Item 6 (line 118) vs AC-6 (line 134).
   - **Severity**: MINOR.
   - **Quoted Text**: Design Item 6 line 118 specifies `bth bearings label` appends an owner-only event using the same check as Item 2, but AC-6 line 134 only tests event appending without mentioning authorization.
   - **Evidence**: `.praxia/docs/specs/261104_bearings-multiscale-goal-awareness.md:118, 134`.
   - **Description**: The human authorization check precondition on labeling was omitted in AC-6.

10. **R1-C10: AC-8 Lacks Explicit Testable Condition for Assumption A10**
    - **Section**: Assumptions Table (line 80) vs AC-8 (line 142).
    - **Severity**: MINOR.
    - **Quoted Text**: Assumptions Table line 80 asserts A10 is measured by AC-8, but AC-8 lacked a structured Given/When/Then testable assertion.
    - **Evidence**: `.praxia/docs/specs/261104_bearings-multiscale-goal-awareness.md:80, 142`.
    - **Description**: AC-8 was descriptive rather than phrased as an executable test for A10.

---

### 3. Defender Responses (Round 1)

- **R1-C01**: Conceded drafting contradiction between Winner/Wrapper and Design Item 3 / AC-2. Rebutted FATAL severity (downgrade to MAJOR) because Design Item 3 and AC-2 are unified and settle the completeness rule (refusal). Proposed replacing "recorded as unreconciled" with "Skipping a live purpose is refused."
- **R1-C02**: Conceded gap in grep assertion. Rebutted FATAL severity (downgrade to MAJOR) because replay methodology is sound and the defect is an over-broad regex token matching historical commits (e.g. `323e782`). Proposed targeting specific diagnosis artifacts (`colliculix-drift-full-diagnosis`, `identity-charter`, `refoundation handoff`, `purpose ledger`, `.bth/charter.toml`).
- **R1-C03**: Conceded. Proposed folding test coverage for Design Item 7 (`bth bearings due`) into AC-8 without violating the 8 AC limit.
- **R1-C04**: Conceded terminology conflation between seam (in v1) and downstream features (deferred). Rebutted scope conflict. Proposed replacing "seam" with "features" in Winner, Wrapper, and Decision Log.
- **R1-C05**: Conceded. Verified true implementation is at `src/bathos/stepback/analysis.py:667-675`. Proposed updating citations in Decision Log and AC-8.
- **R1-C06**: Conceded drafting discrepancy. Proposed removing premature `handoffs` from Winner and Wrapper to align with Design Item 4 and TBD T2.
- **R1-C07**: Conceded. Proposed explicitly naming T2 in the title and Given clause of AC-4.
- **R1-C08**: Partially conceded. Proposed adding explicit mention of `bth charter status` to Design Item 2.
- **R1-C09**: Rebutted duplicate security testing; partially conceded on test completeness. Proposed adding authorization session reference to AC-6.
- **R1-C10**: Rebutted that empirical human assumptions cannot be unit-tested synthetically; partially conceded on Given/When/Then formatting. Proposed integrating explicit measurement condition into AC-8.

---

### 4. Adjudicator Rulings (Round 1)

| Finding ID | Adjudicator Ruling | Assigned Severity | Summary of Rationale |
|---|---|---|---|
| **R1-C01** | Accept with Modifications | **MAJOR** | Contradiction is real, but operational contract in Design 3 & AC-2 is clear. Downgraded from FATAL to MAJOR. Align Winner, Wrapper, T5, and Pre-mortem with refusal rule. |
| **R1-C02** | Accept with Modifications | **MAJOR** | Reachable git history proves grep for "drift" fails unconditionally. Method is sound; token list is defective. Downgraded from FATAL to MAJOR. Scope to specific diagnosis artifacts. |
| **R1-C03** | Accept with Modifications | **MAJOR** | Design Item 7 had zero test coverage. Folding into AC-8 satisfies coverage while honoring the 8-AC ceiling. |
| **R1-C04** | Accept | **MAJOR** | Terminology conflation contradicted AC-3. Correcting "seam" to "features" resolves conflict and validates AC-3. |
| **R1-C05** | Accept | **MAJOR** | Citations in spec 261027 are phantom. Real source verified at `src/bathos/stepback/analysis.py:667-675`. Updated both citations. |
| **R1-C06** | Accept | **MAJOR** | Pre-judged open Owner TBD T2. Removing "handoffs" and referencing T2 restores consistency with Design Item 4. |
| **R1-C07** | Accept | **MAJOR** | Mandatory rule requires ACs depending on open TBDs to name them. AC-4 updated to explicitly cite T2. |
| **R1-C08** | Accept | **MINOR** | Design Item 2 omitted `bth charter status` command name. Added definition to line 104. |
| **R1-C09** | Accept with Modifications | **MINOR** | Clarified in AC-6 that labeling reuses the authorization check from AC-1. |
| **R1-C10** | Accept with Modifications | **MINOR** | Integrated explicit testable condition measuring A10 into AC-8 Given/When/Then assertion. |

---

### 5. Owner Questions Evaluation

- **OQ-01: Reconciling Pivot Refusal vs "Unreconciled" Recording**
  - **Evaluation**: Does NOT require owner escalation. The Owner confirmed the observe-only v1 charter and purpose ledger design. Design Item 3 and AC-2 already dictate the operational completeness rule: skipping a live purpose is refused. The phrase "recorded as unreconciled" in Winner and Wrapper was an obsolete drafting artifact that was aligned with Design Item 3 and AC-2 without changing owner decisions.
- **OQ-02: Resolution of Handoffs in Blind Packet (T2)**
  - **Evaluation**: Does NOT require owner escalation. T2 remains an open Owner-owned question. Removing premature inclusion of "handoffs" from Winner and Wrapper aligns the high-level summary with Design Item 4 (`scope per T2`), preserving Owner jurisdiction.

---

### 6. Exact Edits Applied by Editor (Round 1)

1. **Winner (line 9)**:
   - Replaced `"A pivot that skips this is recorded as unreconciled."` with `"Skipping a live purpose is refused."`
   - Replaced `"(runs, sidecars, ADRs, specs, handoffs, backlog)"` with `"(runs, sidecars, ADRs, specs, backlog; docs scope per T2)"`
   - Replaced `"the argument and multiverse seam"` with `"the argument and multiverse features"`
2. **Decision Log (line 58)**:
   - Replaced `(261027:146)` with `(src/bathos/stepback/analysis.py:667-675)`
3. **Decision Log (line 60)**:
   - Replaced `"argument and multiverse seam (part 8)"` with `"argument and multiverse features (part 8); the seam itself (ext tables and reserved atom: kind) is preserved per items 1 and 5"`
4. **TBD T5 (line 91)**:
   - Replaced `"Whether a run-end reminder for an unreconciled pivot ships after shadow."` with `"Whether a run-end reminder for unconfirmed charter edits ships after shadow."`
5. **Design Item 2 (line 104)**:
   - Added: `"bth charter status reports whether the charter is confirmed or unconfirmed, and names the last confirmed sha256."`
6. **AC-4 (line 129)**:
   - Updated title and Given clause to:
     `- **AC-4 The packet is blind by content, deterministic, and scoped per T2.**`
     `- Given a project with a confirmed charter and doc scope resolved per T2, when bth bearings packet runs twice over the same window, then both packets have the same sha256.`
7. **AC-6 (line 134)**:
   - Updated to: `Every bearings brief render appends a bearings.delivered event listing the item ids shown. Labeling reuses the human-only check from AC-1; given an authorized session, every label appends a bearings.labelled event. Precision for any window can be computed from events alone with no other state.`
8. **AC-7 (line 138)**:
   - Replaced over-broad negative grep with specific diagnosis artifacts:
     `Negative check: a grep of the built packet for colliculix-drift-full-diagnosis, identity-charter, refoundation handoff, purpose ledger and .bth/charter.toml must return nothing. If it does, the control is contaminated and the run is invalid.`
9. **AC-8 (line 142)**:
   - Updated to:
     `- **AC-8 Cadence and shadow exit are testable and pre-registered.** Given a set of projects, when bth bearings due runs, it lists exactly those active in the last 28 days with >= 20 new runs or >= 7 days since their last reading. Before the first live reading, an evaluation sidecar fixes T1 and the excess-spend metric (src/bathos/stepback/analysis.py:667-675, including chains that paid off). It also counts confirmed charters among active projects, testing A10.`
10. **Pre-mortem (line 145)**:
    - Replaced `"emit a run-end reminder only for an unreconciled pivot"` with `"emit a run-end reminder only for unconfirmed charter edits"`
11. **Acceptance Criteria Wrapper (line 150)**:
    - Replaced `"A pivot that skips this is recorded as unreconciled."` with `"Skipping a live purpose is refused."`
    - Replaced `"(runs, sidecars, ADRs, specs, handoffs, backlog)"` with `"(runs, sidecars, ADRs, specs, backlog; docs scope per T2)"`
    - Replaced `"the argument and multiverse seam"` with `"the argument and multiverse features"`

---

### 7. Round 1 Conclusion & Status

**Status**: **NOT CONVERGED**

**Open Items for Round 2**:
- Adversarial review of revised specification to ensure all 10 edits are cohesive and no new or subtle incoherences, terminology misalignments, or citation gaps remain.
- Full verification of all AC conditions against Design items 1-8.
- Confirmation of convergence criteria in Round 2.

---

## Round 2

**Audit Date**: 2026-11-04  
**Status**: **CONVERGED** (0 findings accepted)

### 1. Executive Summary

Round 2 evaluated the revised specification `.praxia/docs/specs/261104_bearings-multiscale-goal-awareness.md` following the integration of all 10 accepted findings from Round 1. Challenger conducted an exhaustive review across all 6 verification areas (Design coverage & AC implication, out-of-v1/TBD constraints, assumptions coverage, document-wide alignment, code/git citations, and terminology consistency) and concluded that all checks PASS with zero incoherences identified. Defender independently examined each area and issued full, unconditional concurrence. Adjudicator performed an independent audit of the target specification and verified all 6 checks, ruling that 0 findings were accepted (0 FATAL, 0 MAJOR, 0 MINOR). Under the governing rule that *"CONVERGED is a round in which no FATAL or MAJOR finding is accepted"*, the verification process achieved convergence in Round 2. No specification edits were required in Round 2.

---

### 2. Challenger Findings (Round 2)

- **Verdict**: **NO INCOHERENCES FOUND: All checks PASS**
- **Findings Reported**: 0 (0 FATAL, 0 MAJOR, 0 MINOR)
- **Scope of Audit**:
  - Confirmed all 10 Round 1 accepted findings (R1-C01 through R1-C10) were cleanly and accurately integrated without secondary contradictions or regressions.
  - Verified bidirectional implication between Design items 1–7 and AC-1..AC-8.
  - Confirmed that no AC depends on deferred out-of-v1 features (Item 8) or open TBDs without naming them.
  - Confirmed Assumptions A9 and A10 are explicitly cross-referenced and tested in AC-7 and AC-8 respectively.
  - Confirmed complete consistency across Winner, Decision Log, Frame-Deviation entry, Wrapper, and Design.
  - Verified all source code paths, line numbers, functions, error codes (`STEPBACK_INSUFFICIENT_EVIDENCE`), commit hashes (`14c3804`, `24ef9a6`, `588d175`), and base-rate calculations at source.
  - Confirmed semantic consistency of all 7 core terms (`charter`, `purpose`, `pivot`, `reading`, `packet`, `brief`, `mapped`/`unmapped`/`silent`).

---

### 3. Defender Responses (Round 2)

- **Verdict**: **FULL CONCURRENCE WITH CHALLENGER (ZERO INCOHERENCES / PASS)**
- **Response Details**:
  - Verified bidirectional implication between Design Items 1–7 and AC-1..AC-8 (exactly 8 ACs maintained).
  - Confirmed clean decoupling of out-of-v1 features (run-end push, detours, decision-follow, drift signals, portfolio view, rival/fork/argument features) while preserving passive seams (`[purpose.<id>.ext]`, `atom:` citations).
  - Confirmed test coverage of Assumptions A9 (tested in AC-7) and A10 (measured in AC-8).
  - Confirmed alignment across Winner, Wrapper, Decision Log, Frame-Deviation, and Design (completeness rule on pivots, T2 doc scope, observe-only v1).
  - Confirmed empirical verification of all source code citations and git commit hashes.
  - Concurred unconditionally with the Challenger's verdict and recommended a formal ruling of convergence.

---

### 4. Adjudicator Rulings (Round 2)

- **Rulings Summary**:
  - Accepted Findings: 0 (FATAL: 0, MAJOR: 0, MINOR: 0)
  - Rejected Findings: 0
  - All 6 Verification Checks Verified Independent of Challenger/Defender:
    1. **Check 1 (Design Coverage & AC Implication)**: PASS. Design items 1–7 map bidirectionally to AC-1..AC-8.
    2. **Check 2 (Out of v1 & Open TBD Constraints)**: PASS. Zero dependencies on deferred v1 features; T1 and T2 explicitly named in AC-8 and AC-4; T3–T5 cleanly bounded.
    3. **Check 3 (Assumptions A9 & A10)**: PASS. A9 tested in AC-7; A10 measured in AC-8.
    4. **Check 4 (Decision Log, Frame-Deviation, Winner, Wrapper Alignment)**: PASS. Refusal rule, observe-only mode, and T2 scoping uniformly applied.
    5. **Check 5 (Code, Git, and Arithmetic Citations)**: PASS. All 15 cited code files, line numbers, error codes, and git commits resolve at source.
    6. **Check 6 (Terminology Consistency)**: PASS. All 7 domain terms used uniformly.
- **Formal Ruling**: Specification is coherent, complete, and verified. Convergence criteria satisfied.

---

### 5. Edits Applied by Editor (Round 2)

- **Target Specification (`.praxia/docs/specs/261104_bearings-multiscale-goal-awareness.md`)**:
  - **None (specification unchanged from Round 1).**
  - The specification has 0 accepted findings in Round 2 and remains in its clean, verified state established at the end of Round 1.

---

### 6. Round 2 Conclusion & Status

CONVERGED after 2 rounds
