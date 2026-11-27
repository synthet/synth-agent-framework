# Item spec template

Copy for each backlog ID into `items/<ID>.md`. Keep 80–180 lines; P3 research items are
spike-shaped (question, evidence to collect, decision output, timebox).

```markdown
---
id: <ID>
title: <title from the backlog>
priority: <P0|P1|P2|P3>
score: <n>
effort: <S|M|L|XL>
depends_on: [<IDs>]
milestone: <M0..Mn>
status: proposed
baseline: <default branch> <sha> (<date>)
---

# [<ID>] <title>

## Problem
Why it matters and the user-visible effect; cite the source section.

## Current state (<sha>)
| Status | What | Where |
|---|---|---|
| Exists / Partial / Missing / Conflict | ... | `path` + symbol |

## Proposed change
Modules that change, new types or functions, reuse of existing utilities by name and path,
feature flag if any. Proposed files are marked (new).

## Scope / non-goals

## Acceptance criteria
- **<ID>/AC-1**: When/While/If ..., the <system> shall ... (EARS, one "shall", testable, one line)

## Test plan
| Platform | How |
|---|---|
| Windows | ... |
| macOS | ... |
| Linux | ... |

## Security / license review

## Overlaps and existing work
Tickets, other epics' task IDs, specs, reports, branches. Reuse; never duplicate.

## Open questions (owner to confirm)
Recommendation, then the alternative.

## References
```

Write each AC on one line: agent-flow's importer reads AC text one line at a time, and the
checker joins wrapped lines only as a fallback.
