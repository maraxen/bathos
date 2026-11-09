---
name: bathos-progress-report
description: Prepare an advisor progress report from git history, project docs and bathos run records, then work with the researcher to decide what is worth raising and what is gated on the advisor's feedback
when_to_use: 'Triggers: progress report, advisor meeting, advisor update, weekly update, meeting with my PI, what should I raise, what is blocked on my advisor, status for my advisor, lab check-in, one-on-one prep.'
---
# bathos-progress-report

A progress report is not a changelog. It is a short document whose job is to **get decisions out of
the advisor** and keep him calibrated on where each project stands. Gathering is mechanical and
agents do it well. Selection (what is worth his time) is the researcher's call, and this skill
makes that step explicit instead of letting an agent decide by commit volume.

Works standalone. Uses maraxiom and affigit **only if installed** (see *Optional integrations*);
never require them.

## Inputs to settle first (one `AskUserQuestion` call)

1. **Audience**: advisor name and role, plus anyone else in the room. Record the name in
   `~/projects/project_status.toml` `[meta] advisor = "..."` if that file exists.
2. **Window**: since the last report (look for the newest file under `~/projects/status_history/`
   or a previous report doc) or a fixed span. Default: since the last meeting.
3. **Scope**: which projects. Default: research projects at P1/P2 plus anything the researcher
   names. Research software goes in one "tooling" line unless it produced a research result.
4. **Format**: Claude Doc (default; shareable and he can comment), Markdown memo, or a maraxiom
   `progress-report` deck (only if maraxiom is installed and there is time).

## Sources: gather per project, evidence for every line

Fan this out to read-only recon agents (one per 2–3 projects, Haiku). **Pick an agent type that
has Bash** (`general-purpose`, or `Explore` with Bash); `praxia:recon` has no Bash, so it cannot
run `git log` or `bth sql`, and falls back to reflogs and guesses (dogfood 261109: all four recon
agents came back partial). Every claim they return must carry a commit sha, a `file:line` or a run
id. **Spot-check every number by `bth sql` or grep before it goes in the report.** Haiku
over-claims on multi-step summaries, and so does the orchestrator: an "about 35" estimate was 54
when queried.

Run the catalog queries (sources 3–4) yourself first: they take seconds and tell you which
projects are worth a recon agent at all.

Work often lives in worktrees and branches, not on main: `fd -H -I` across
`<repo>/.claude/worktrees/*/.praxia/docs` finds docs that `main` does not have yet.

| # | Source | How | What it yields |
|---|---|---|---|
| 1 | **Main-line git** | `git log --first-parent <default> --no-merges --since=<start>` plus merged PRs | Themes of work. Never use `--all` commit counts as a measure of progress: agent branches inflate them 10×. |
| 2 | **Where work moved** | `git worktree list`; sibling dirs (`<proj>-wt-*`, `<proj>-esm`); `git log --all` only to *locate* work | A project that looks silent may have moved to a sibling repo. Check before calling it stalled. |
| 3 | **bathos run records** | `bth sql "SELECT project_slug, id, outcome, command FROM runs WHERE timestamp >= '<start>'"` | Findings: what passed, failed or came out marginal, by pre-registered sidecar. **Include negative results.** `observed`/`unknown` outcomes are exploratory, not findings (see `using-bathos`). |
| 4 | **Campaigns and claims** | `bth sql "SELECT project_slug,name,mode,status,concluded_at,outcome_label FROM campaigns WHERE started_at >= '<start>' OR concluded_at >= '<start>'"` | Concluded confirmatory campaigns are the headline results. A `confounded` label is a result too. |
| 5 | **Step-back holds** | `bth stepback status --campaign-id <id>` for open campaigns | Level 2/3 holds and `ESCALATE` recommendations are by definition items for the advisor (Level 3 = human chair). If the `stepback_*` tables are absent from the index, none were ever recorded; say so. |
| 6 | **Project docs** | `.praxia/docs/{decisions,research,audits,specs}/` and `.praxia/handoffs/` dated in the window | Decisions taken, findings written up, open questions. |
| 7 | **Gated items** | grep docs, handoffs, `ideas.jsonl`, backlog for `advisor`, the advisor's name, `PI decision`, `waiting on`, `needs decision`, `open question`, `blocked` | Candidate "needs your input" items with `file:line`. |
| 8 | **Portfolio table** | `~/projects/project_status.toml` and `PROJECT_STATUS.advisor.md` (rendered by praxia `scripts/projects/project_status.py --write --snapshot`) | Priority, people, end and reconsider dates, last stated "next". **Overdue `end_date`/`reconsider` dates are prime agenda items.** |
| 9 | **Previous report and action items** | last report doc or `status_history/*_advisor.md`; post-meeting triage outputs | Did we do what we said? Close the loop explicitly: done / not done / dropped, with reason. |
| 10 | **Collaborator deliverables** | `people`/`endstate_text` in the status table; docs mentioning partner labs | External dates and owed items (designs to a wet lab, data from a partner). |
| 11 | **Compute** | myxcel `list_jobs`, failed/preempted jobs in the window | Only if compute is a bottleneck or a request (allocation, GPU type). |
| 12 | **Effort vs. priority** | active days per repo (7/28 d) against `priority` | Be plain if tooling time dwarfed research time; the advisor will notice anyway. One sentence on why, and whether it is ending. |
| 13 | **Meeting notes and transcripts** | `.praxia/docs/research/*meeting*`, `docs/decisions/*meeting*` in the window | The best single source of commitments, owners and dates (who promised what to whom). Feeds the closed-loop table. |
| 14 | **Peer project sessions** | `ListAgents`; `SendMessage` the session that owns a project | Ask it for the decision *as it would pose it to the advisor*, with options and a recommendation, from existing work only, with a reply deadline. Treat the reply as unverified until checked against the file it cites. |
| 15 | **Run mix inside a repo** | `bth sql "... GROUP BY regexp_extract(command,'scripts/([a-z_]+)/')"` | A research repo can hide tooling work (dogfood: 54 of 107 TEV runs were a literature-review pipeline). Report it plainly. |
| 16 | **Figures ready to show** | `outputs/**/*.{png,svg}` registered as run `output_paths` in the window; affigit/maraxiom figure index if present | One figure per headline result, cited to its run id. |

## Selection: work it out with the researcher

After gathering, build a **candidate list**, one line per item:
`project · kind (result | decision-needed | blocker | FYI | overdue) · one-line claim · evidence`.

Then ask, in one `AskUserQuestion` call (max 4 questions, `multiSelect` where it fits):

- **Raise**: which results to lead with (pick ≤ 3 headlines; the rest go to an appendix or are dropped).
- **Gated on advisor**: confirm which items actually need his decision, input or introduction. For
  each, the report states the decision, the options, and a recommendation. A question without a
  recommendation wastes his time.
- **Overdue or reconsider dates**: close, extend (with a new date) or raise.
- **Sensitive or not-ready**: anything to hold back (unverified numbers, collaborator-confidential).

Rules for what is worth raising:
- Raise **decisions he alone can make**, **results that change a plan**, **negative results that
  kill a line of work**, and **slipped commitments**. Skip routine progress he does not need to act on.
- A number goes in only if it comes from a run record with an evaluated outcome. Exploratory
  numbers are labelled "exploratory, not pre-registered" or left out.
- Tooling and infrastructure work: one line, framed by what it unblocks for research.

Expect the researcher to reshape the candidates, not just pick from them. In the first dogfood they
added asks the sources never surfaced (TAC meeting date), re-weighted one (the RevivBio follow-up
mattered for enzyme screening and thermostability, not for the narrow question drafted), and widened a
framing beyond one project. Offer "Other" freely and rewrite the asks from their words.

Keep everything in a first draft and let the researcher prune or move items to the appendix
afterwards, rather than filtering silently up front.

## Report structure (any format)

1. **Asks**: the decisions and inputs needed from the advisor, each with options and a recommendation. Put these first.
2. **Headlines**: ≤ 3 results, each one sentence plus the run or campaign id and one figure if available.
3. **Per-project status**: a table of project · status · this window · next · blocker/date.
4. **Closed loop**: last meeting's action items and what happened to each.
5. **Risks and slipped dates.**
6. **Appendix**: evidence (run ids, docs, commits) so any line can be traced.

## After the meeting

Hand the researcher's notes to `praxia:triaging-meeting-notes` (ideas, debt, backlog), record the
advisor's decisions in the project's `.praxia/docs/decisions/`, and update `project_status.toml`
(`reviewed`, `next`, dates). Those files become source 9 of the next report.

## Optional integrations (detect, never require)

- **maraxiom** (`command -v mrx` or the `using-maraxiom` skill is listed): a deck with
  `profile: progress-report`; results shown on a review page go through `mrx review audit` before
  any claim binding (see `bathos-campaigns`).
- **affigit** (`using-affigit` listed): take figure pins from its ledger so the report cites the
  pinned figure version rather than a loose file.
- Neither installed: cite figure files by path and run id.

## Known gaps

bathos has no model of who the people are or who an item is waiting on (debt #3324). Until it does,
gated items come from grep and from the researcher. Literature scoping is debt #3325.

## Related

- `using-bathos`: verifying a run by its record before citing it
- `bathos-campaigns`: claims and conclusions
- `bathos-step-back`: `bth stepback brief <campaign>`, which renders a per-campaign advisor brief to attach to an ask
