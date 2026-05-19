# bathos Integration — Gemini CLI

bathos provides a skill (`using-bathos`) that teaches Gemini CLI how to:
- Track experiments via `bth run`
- Query results via `bth ls`, `bth find`, `bth sql`
- Validate runs via `bth check` _(coming in v0.2 — not yet available)_
- Decide when to dispatch vs. run CLI directly

## Install

```bash
# User-level (available in all projects)
bth export --tool gemini --level user

# Workspace-level (current project only)
bth export --tool gemini --level workspace
```

This writes `using-bathos.md` to `~/.gemini/skills/` or `.gemini/skills/` respectively.

## Verify

```bash
ls ~/.gemini/skills/using-bathos.md
```

Gemini CLI loads skills from these paths at session start via its skill discovery mechanism.

## What the skill teaches agents

- **When to call `bth run`** vs. executing scripts directly
- **How to create sidecars** (`.bth.toml`) for pre-registration
- **Outcome evaluation** — how `bth ls` OUTCOME column maps to pass/marginal/fail
- **SLURM integration** — using `_bth_env.sh` in batch scripts
- **Cross-project queries** — `bth sql` across all projects

## Tool name mapping

Gemini CLI tools use different names from Claude Code. The using-bathos skill is written for Claude Code tool names. For Gemini CLI, the following equivalents apply:

| Skill tool name | Gemini CLI equivalent |
|-----------------|-----------------------|
| `Bash` | `run_shell_command` |
| `Read` | `read_file` |
| `Edit` | `replace_in_file` |
| `Write` | `write_file` |

The SKILL.md uses Claude Code names; adapt accordingly when the Gemini CLI skill loader doesn't auto-translate.

## Update

```bash
bth export --tool gemini --level user
```

Re-running overwrites with the latest skill version.
