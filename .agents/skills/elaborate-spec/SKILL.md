---
name: elaborate-spec
description: "Use when the user asks to elaborate, expand, ground, deepen or 'make real' a spec, roadmap, PRD or consolidated plan document (often an external .md from Downloads or another chat) against an existing repository: verify each backlog item against the code, record what exists, what is partial and what is missing, correct the document's wrong assumptions, link existing tickets and branches instead of duplicating them, and produce a repo spec (spec.md), one spec per item (items/<ID>.md), PR-sized tasks (tasks.md) and an optional agent-flow import. Not for writing a first spec from a bare idea (framework-spec), for turning an already-grounded spec into tickets (flow-plan), or for mining a document into skills (mine)."
capability: "Grounds an external spec in a repo with parallel area explorers and writers, first-hand verification of load-bearing claims, a deterministic path/coverage/graph checker and a T-n import-table generator for agent-flow."
side_effect_level: local_write
approval_required: true
requires_tools: "git; python 3.9+ (stdlib) for scripts/spec_checks.py; Agent/subagents for fan-out; optional agent-flow MCP (flow_list, flow_import_tasks) and llm-wiki."
output_schema: "specs/<NAME>/{spec.md, items/<ID>.md, tasks.md, README.md, tasks-import.md}, a single-file copy next to the source, checker JSON reports, and a list of corrections and owner decisions."
risk_class: medium
---

# Elaborate a spec against the repo

The input is a document someone wrote *without* reading the code: every item is "proposed" and
every path is a guess. Elaborating means pinning each item to the code at a known commit, with
evidence, and saying where the document is wrong. It does not mean adding prose.

## 1. Pin the input and the baseline

- Read the whole source. **Count its backlog IDs with a script**, not by eye; a summary count
  that is off by one propagates into every later check.
- Record the baseline: `git rev-parse --short HEAD` on the default branch. Every "exists" claim is
  "at <sha>".
- Ask one question if the deliverable is unclear (repo spec / spec + tickets / expanded copy /
  per-item specs; which part of the backlog in depth). "All" is a valid answer.

## 2. Find prior work before writing anything

Most of the repo's answers are not on the default branch. Check all of these:

| Where | How |
|---|---|
| Unmerged branches | `git branch -a`; `git diff --stat <default>...<branch>` for doc/spec branches; `git show <branch>:<path>` |
| Tracker | `flow_list` with `include_closed` (local DB only: another PC's tracker is invisible; look for its snapshot `.agent-flow/tickets.jsonl` on a branch) |
| Specs, reports, plans | `specs/`, `docs/reports/`, `docs/planning/`, `docs/architecture/` |
| Recorded decisions and ownership | memory files, the LLM wiki (`search` statuses are `reviewed` / `candidate`, not `current`), CLAUDE.md rules |
| Upstream (forks) | `git log --grep '#<pr>' <default>` for PRs the document cites. A local `upstream` remote may point at the fork itself; check its URL, or fetch upstream by URL |

Anything found becomes an **overlap**: the item references it, and no duplicate task is planned.
Work another agent owns is cited by its task ID, never re-planned.

## 3. Explore by area, then verify what you will assert

Fan out one Explore agent per area (two or three areas), each told to return, per item:
exists / partial / missing / conflicts, with paths and symbols.

Explore summaries locate code; they do not audit it. **Before you write a correction into the
spec, read the code yourself** (`grep -n`, `sed -n`) for each load-bearing claim: feature
flags, the actual permission behaviour, where keys go, persistence semantics, which upstream PRs
are present. Write the plan or spec only after that.

## 4. Deliverables

Under `specs/<NAME>/` on a new branch, following the repo's spec format:

| File | Content |
|---|---|
| `spec.md` | Direction, a **baseline table** (area → exists → gap), the backlog with Baseline and "Covered by" columns linking `items/<ID>.md`, architecture mapped to real modules, milestones and exit gates, spec-level ACs, verification matrix with real commands, open decisions as recommendations marked *owner to confirm*, risks |
| `items/<ID>.md` | One per backlog ID, from [references/item-template.md](references/item-template.md) |
| `tasks.md` | AC traceability, one task per PR (`<ID>.<n>`), rows grouped by milestone, "Covered by <ref>" rows for existing work |
| `README.md` | Index, items by milestone, overlap map, conventions |

Also write a single-file copy next to the source document (spec + items + tasks). **Never
overwrite the source.**

Conventions: cite files that exist only on a branch as `<branch>:<path>`; mark proposed files
"(new)"; prefer `path` + symbol over `path:line`; the current product name only.

## 5. Write items in parallel

Write `spec.md` yourself (it holds the cross-cutting decisions). Give the items to 4–5
general-purpose writers, grouped by area, using [references/writer-brief.md](references/writer-brief.md).
Each writes its item files plus a task-row fragment in the scratchpad and replies with at most ten
**contradictions** of your findings, with evidence. Fold every contradiction back into
`spec.md`: they are the most valuable output of the whole run.

## 6. Assemble and check mechanically

Merge the fragments into `tasks.md`, then run the checker from the repo root:

```bash
H="<this skill's directory>/scripts/spec_checks.py"
python -I $H coverage specs/<NAME>      # every item has a file and tasks; every <ID>/AC-n covered
python -I $H graph specs/<NAME>         # unknown refs, cycles (item-level deps expand to all tasks)
python -I $H paths specs/<NAME>         # cited paths exist; --fix-short expands unique short paths
```

- **Cycles** almost always come from a dependency on a bare item ID (`UI-01`) where only one of its
  tasks was meant. Replace it with the specific task (`UI-01.2`).
- **Missing paths**: expand short forms (`acp/mod.rs`) with `--fix-short`; mark proposed files "(new)"
  on *every* line that cites them; the rest are errors.
- External refs (ticket IDs) are reported, not failed.

## 7. Tracker import (optional, two steps)

`python -I $H import-table specs/<NAME> --title "<epic>"` writes `tasks-import.md`. Then dry-run
`flow_import_tasks` (format `framework`, `spec` = the same file, `backlog: true`), show the counts
and warnings, and **stop for a separate confirmation** before `confirmed: true`. Why the extra file
is needed: [references/gotchas.md](references/gotchas.md).

## 8. Finish

- Commit on the branch. No push or PR unless asked; for forks name the target repo explicitly.
- Report: deliverables with paths, the corrections the code forced, owner decisions, checker results,
  what is left (import confirmation, PR).
- Did code change? A docs-only elaboration needs no rebuild or redeploy; say so if asked.

## Never

- Present the source's claims as facts, or Explore summaries as verified.
- Plan duplicates of tickets or epics another agent owns.
- Import tickets, push, or open a PR without its own confirmation.
