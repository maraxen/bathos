# bathos Integration — Claude Code

bathos provides a skill (`using-bathos`) that teaches Claude Code how to:
- Track experiments via `bth run`
- Query results via `bth ls`, `bth find`, `bth sql`
- Validate runs via `bth check` _(coming in v0.2 — not yet available)_
- Decide when to dispatch vs. run CLI directly

## Install

```bash
# User-level (available in all projects)
bth export --tool claude --level user

# Workspace-level (current project only)
bth export --tool claude --level workspace
```

This writes `using-bathos.md` to `~/.claude/skills/` or `.claude/skills/` respectively.

## Verify

```bash
ls ~/.claude/skills/using-bathos.md
```

The skill is loaded automatically at session start in Claude Code. In a new session, the agent will have bathos commands available in its routing table.

## What the skill teaches agents

- **When to call `bth run`** vs. executing scripts directly
- **How to create sidecars** (`.bth.toml`) for pre-registration
- **Outcome evaluation** — how `bth ls` OUTCOME column maps to pass/marginal/fail
- **SLURM integration** — using `_bth_env.sh` in batch scripts
- **Cross-project queries** — `bth sql` across all projects

## MCP tool reference (if FastMCP is enabled)

| MCP Tool | CLI equivalent | Status |
|----------|---------------|--------|
| `run` | `bth run` | ✅ Available |
| `list_runs` | `bth ls` | ✅ Available |
| `get_run` | `bth show <id>` | ✅ Available |
| `find_runs` | `bth find` | ✅ Available |
| `run_sql` | `bth sql` | ✅ Available |
| `compact_catalog` | `bth compact` | ✅ Available |
| `check` | `bth check` | ✅ Available |
| `archive` | `bth archive` | ✅ Available |
| `campaign_create` | `bth campaign create` | ✅ Available |
| `campaign_list` | `bth campaign ls` | ✅ Available |
| `campaign_review` | `bth campaign review` | ✅ Available |
| `campaign_conclude` | `bth campaign conclude` | ✅ Available |

## Update

```bash
bth export --tool claude --level user
```

Re-running overwrites with the latest skill version (stamped with bathos version and timestamp in first line).
