# Item #137: Global Instruction Portability — Design Document

**Status:** Design / Exploration (not ready to implement)
**Backlog item:** #137 — Global instruction portability — composable snippets for multi-surface export
**Date:** 2026-06-02
**Author:** spec-agent (task 260602_bathos-v08-sprint)

---

## Problem Statement

bathos has a complete pipeline for exporting its *skill* (procedural how-to instructions for
agents) to Claude Code and Gemini CLI. It has no working pipeline for exporting *rules*
(behavioral constraints, hard patterns, anti-patterns) to the same surfaces.

The divergence is already observable:

- `agent_assets/snippets/rules.md` — `bth run` invocation discipline, DuckDB condition syntax,
  sync ownership policy (lives in the repo, scope=project per manifest)
- `~/.claude/rules/BATHOS.md` — measurement-pipeline skepticism heuristic (lives in user's
  global config, authored by hand, no automated path back to this repo)

These are two *different categories* of global instruction, placed in two *different locations*,
with zero automated relationship between them. The `--surface claude_code` flag in `cli.py` exists
as a stub that does nothing. `export.py` has no snippet exporter and no concept of a "rules
surface."

---

## What "Multi-Surface" Could Mean

The manifest declares `[[plugin.snippets]]` entries with a `scope` field. The `scope` field has
at least two values implied by the design (`project` and presumably `global`/`user`). The term
"multi-surface" in the item title is ambiguous — it could mean any combination of:

**Surface dimension (target tool):**
- Claude Code rules (`~/.claude/rules/<name>.md` or `.claude/rules/<name>.md`)
- AGENTS.md injection (prepended or appended block in a project-level AGENTS.md)
- Gemini CLI rules (equivalent path, e.g. `~/.gemini/rules/<name>.md` — not a known standard)
- A future generic "instructions" surface (tool-agnostic)

**Scope dimension (installation level):**
- `user` / `global` — written to `~/.claude/rules/` — always active regardless of project
- `project` / `workspace` — written to `.claude/rules/` (relative to cwd) — active only in
  this project

**Composition dimension (how snippets combine):**
- Option A: one snippet → one file (1:1, simplest, already implicit in the manifest schema)
- Option B: multiple snippets concatenated into a single rules file per surface
- Option C: snippets conditionally included based on `scope_detector` (e.g., only export
  `bathos-rules` when `scripts/**/*.bth.toml` files are present in the project)
- Option D: a registry/index file that points to individual snippet files (avoids concatenation
  conflicts but requires Claude Code to support directory-scoped rules)

---

## Design Space: Key Decisions

### Decision 1: What is the canonical source for `~/.claude/rules/BATHOS.md`?

**Current state:** The file was hand-authored with a measurement-pipeline heuristic. The repo's
`agent_assets/snippets/rules.md` has completely different content (invocation discipline). Neither
is "wrong" — they address different concerns.

**Option A — Single source, repo-owned:** Merge both into `agent_assets/snippets/rules.md`, make
that the canonical source, and auto-export overwrites `~/.claude/rules/BATHOS.md`. The user's
hand-authored heuristic either becomes a second snippet (`rules-research.md`) or a section in the
merged file.

**Option B — Two sources, different scopes:** Keep `~/.claude/rules/BATHOS.md` as a user-level
manually-maintained file (not touched by `bth export`). `agent_assets/snippets/rules.md` exports
to a *different* path (e.g., `~/.claude/rules/bathos-bth.md` or `.claude/rules/bathos-rules.md`
at project scope). The two files coexist and Claude Code merges them implicitly.

**Option C — Snippet registry with metadata:** Each snippet declares its own target path template
(e.g., `target = "~/.claude/rules/{plugin}-{name}.md"`), allowing granular placement without
collision.

**Trade-off:** Option A gives a single source of truth but requires merging philosophically
different content (invocation rules vs. epistemic heuristics). Option B respects their different
origins but adds two files competing for attention at the same scope level. Option C is the most
flexible but adds manifest schema complexity.

---

### Decision 2: How does `--surface` map to a concrete export action?

**Current gap:** `bth export --surface claude_code` is a no-op stub. It's called by the praxia
plugin system as a post-install hook. There is no definition of what it should *do*.

**Option A — Surface is an alias for (tool, artifact-type):**
`claude_code` → `(tool="claude", artifact="rules")`, driving a new `export_snippet()` function
that writes to the rules target path. The `--level` flag (user/workspace/system) still applies.

**Option B — Surface is a profile that bundles multiple artifacts:**
`claude_code` → export skill + export rules + register MCP (all three). Makes `--surface
claude_code` a "full install" command, and the individual `--tool`/`--level` flags become
overrides. Simpler UX (one command to set up everything) but harder to dry-run granularly.

**Option C — Surface is only used by the plugin system, not the CLI:**
The `--surface` flag exists purely so praxia can call `bth export --surface claude_code` as a
hook. It never grows a user-facing meaning. The hook implementation does a fixed set of actions.
This is the simplest path to unblocking praxia integration.

---

### Decision 3: Should snippet export respect `scope_detector`?

The manifest currently declares:

    scope_detector = "glob:scripts/**/*.bth.toml"

This implies: "only activate/export this snippet when the project has bth sidecar files." But
*what context is this check run in?* The project root? The user's home directory? The current
working directory at `bth export` time?

**Option A — Scope-detector gates project-scoped export only:** When `bth export` runs for
`level=workspace`, it checks the detector against `cwd`. If no `.bth.toml` sidecars exist, skip
this snippet. For `level=user`, ignore the detector (user-level rules are always active).

**Option B — Scope-detector is advisory metadata only:** `bth export` always writes the snippet
regardless of detector state. The detector is used only by the praxia plugin system to decide
whether to *display* or *activate* the snippet in an agent context.

**Option C — Scope-detector is not implemented in export at all:** The manifest attribute is
reserved for future use. `bth export` ignores it.

The risk in Option A is that `bth export` becomes stateful (depends on cwd contents) and
surprising to users who run it from a non-project directory. Option B and C are safer defaults
until the semantics are clearer.

---

### Decision 4: Should `export.py` be aware of `manifest.toml`?

Currently `export.py` knows nothing about `manifest.toml`. It reads source paths directly
(hardcoded relative to the package root). The manifest declares snippet paths and scopes,
but no export code reads it.

**Option A — Keep export.py manifest-unaware:** Hardcode snippet source paths in `export.py`
(same pattern as `get_skill_source_path()`). Add a `get_snippet_source_path(name)` function
that resolves against the package root. Simple, but brittle if snippet paths change.

**Option B — Teach export.py to read manifest.toml:** `export.py` loads
`agent_assets/manifest.toml` (relative to the package root) and resolves snippet paths from
`[[plugin.snippets]]` entries. Source of truth is the manifest, not hardcoded paths. More
flexible, but adds coupling between two components that are currently independent.

**Option C — New `snippets.py` module as mediator:** A thin `snippets.py` module owns the
manifest-reading and path-resolution logic. `export.py` imports from it. `cli.py` imports from
both. Keeps `export.py` focused on write mechanics.

---

### Decision 5: Target path convention for rules snippets

For skill export, the target path is:
- User: `~/.claude/skills/using-bathos/SKILL.md`
- Workspace: `.claude/skills/using-bathos/SKILL.md`

For rules export, Claude Code reads from `~/.claude/rules/*.md`. The naming convention is:
- Option A: `{plugin}-{snippet-name}.md` → `bathos-rules.md`
- Option B: `{plugin}.md` → `bathos.md` (one file per plugin, all snippets merged)
- Option C: `{snippet-name}.md` → `bathos-rules.md` (same as A but without plugin prefix)

The risk of Option B (merging) is that a future second snippet from the bathos plugin would
require regenerating the merged file. Option A gives each snippet its own file and is
independently updatable. Option C omits the plugin prefix and risks collisions with other tools'
snippets using the same snippet name.

---

## The Divergence Problem in Detail

The two rule files that currently exist for bathos have no automated relationship:

| File | Content | Location | Scope | Maintained by |
|---|---|---|---|---|
| `agent_assets/snippets/rules.md` | Invocation discipline (`uv run`, DuckDB SQL, `--no-sidecar` policy) | repo | project | bathos repo |
| `~/.claude/rules/BATHOS.md` | Measurement pipeline skepticism heuristic | user home | global | hand-authored |

Before any implementation can proceed, a decision is needed: are these two files *the same
thing* (and the divergence is a bug to fix) or *two different things* (invocation rules vs.
epistemic rules) that should coexist at different scopes?

If they are the same: the hand-authored content should be moved into the repo and exported.

If they are different: the export pipeline should produce `bathos-invocation.md` (or similar)
and leave `BATHOS.md` alone, and users must understand they manage the epistemic file manually.

---

## Open Questions

1. **Authoritative content question:** Is `~/.claude/rules/BATHOS.md` (measurement heuristic)
   content that belongs in the bathos plugin and should be exported from the repo, or is it
   user-personal content that happens to live in the rules directory? This determines whether
   `bth export` should ever touch that file path.

2. **Scope semantics:** What should `scope=project` mean operationally for export? Should
   `bth export --level user` unconditionally export all snippets, or should project-scoped
   snippets be excluded from user-level export?

3. **Composition model:** Is "composable snippets" referring to (a) multiple `.md` files that
   coexist in the rules directory, (b) a single assembled file that concatenates multiple source
   snippets, or (c) something else? The word "composable" implies assembly, but the manifest's
   1:1 snippet-to-file design implies coexistence.

4. **Gemini rules equivalent:** Does Gemini CLI have a rules/instructions directory analogous
   to `~/.claude/rules/`? If not, is the Gemini surface out of scope for snippet export?

5. **AGENTS.md injection:** Should `bth export` be able to inject a block into a project's
   `AGENTS.md`? This would be a different mechanism than writing to `~/.claude/rules/` — it
   modifies an existing file rather than writing a new one. Is this in scope?

6. **Idempotency and version stamps:** The skill export stamps a version header so users can
   see when the file was last exported. Should snippet exports carry similar stamps? If snippets
   are concatenated, where does the stamp go?

7. **Conflict detection:** If the user has manually edited `~/.claude/rules/bathos-rules.md`
   and then runs `bth export`, should the export overwrite silently, warn, or refuse? There is
   no conflict detection in the current `export_skill()` implementation.

8. **Testing surface:** The `--surface` stub currently exits with code 0 after printing a
   diagnostic. Tests that call `bth export --surface claude_code` will silently pass even
   though nothing was written. What should the test surface look like once the stub is replaced?

9. **Plugin hook contract:** The praxia manifest calls `bth export --surface claude_code --level
   user` as a post-install hook. Should this command be idempotent (safe to re-run), and should
   it return a non-zero exit code if anything goes wrong? The current stub always exits 0.

10. **Multi-plugin collision:** If two bathos snippets both write to the `~/.claude/rules/`
    directory and a future bathos plugin also writes rules there, what prevents filename
    collisions? Is there a plugin namespace convention that should be standardized now?

---

## Relationship to Other Backlog Items

- **Item #136** (`bth-migrate` praxia workflow) depends on understanding how snippets are
  classified and what paths they write to. A decision on the rules target path convention
  (Decision 5 above) feeds directly into how migrated files are placed.
- **Item #142** (results management) is independent — no cross-dependency.
- **Item #143** (threshold epistemic hygiene) may produce a second snippet
  (`adversarial-check-rules.md` or similar) that would exercise whatever snippet export
  infrastructure this item builds. Worth considering as a concrete second use case when
  evaluating the composition model.

---

## Suggested Next Step

Before writing an implementation spec, the user should answer the three highest-priority
questions above:

1. **Content authority** (Q1): Is the measurement-heuristic content in `BATHOS.md` repo-owned
   or user-owned? This determines whether `bth export` ever touches that file path.
2. **Composition model** (Q3): One merged file or many individual files in the rules directory?
3. **Surface scope** (Q2 + Q4): Should Gemini and AGENTS.md be in scope for v0.8, or is this
   purely a Claude Code rules export for now?

Once those three are decided, the design space collapses to a tractable implementation: a
`export_snippet()` function in `export.py`, a target path resolver for the rules surface, a
thin handler in the `--surface` stub, and tests mirroring the existing skill-export test
patterns. The core mechanics are straightforward — the blocking uncertainty is entirely about
intent and scope.
