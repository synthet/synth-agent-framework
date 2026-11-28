# Writer brief (one per area)

Fill the placeholders and pass it to a general-purpose subagent. Hand over files by path, not by
pasting: the source document, the approved plan with your verified findings, the item template,
and an example task table (`git show <branch>:<path>` into the scratchpad if it is on a branch).

```text
You are writing per-item engineering specs for <PROJECT> in <REPO PATH> (branch <BRANCH> is
checked out).

INPUTS
- Source document (read your IDs' sections and the shared sections <LIST>): <PATH>
- Approved plan with verified findings (read first): <PLAN PATH>
- Item template (follow exactly): <TEMPLATE PATH>
- Task-row example: <EXAMPLE PATH>

YOUR IDS: <IDS>

WRITE one file per ID at <REPO>/specs/<NAME>/items/<ID>.md, and ONE fragment at
<SCRATCHPAD>/tasks-<group>.md holding only table rows (no header):
| Task ID | Task | Owner role | Files / areas | Depends on | ACs | Verification |
Task IDs are <ID>.<n>, one PR each; P0/P1 items split into 2-5 tasks, P2/P3 into one task or a
spike. Depends-on cites task IDs where possible (a bare item ID means all its tasks and can create
cycles). ACs cite <ID>/AC-n. Work already covered by an existing ticket or another agent's epic
gets a row "Covered by <ref> - no new ticket" with owner role "existing".

RULES
- Ground everything in the code. Confirm each path exists (ls / rg) and grep each symbol before
  citing it. Prefer path + symbol over path:line.
- Files only on unmerged branches are cited as <branch>:<path>; read them with git show.
- Existing tickets and other epics: <LIST WITH IDS AND OWNERS>. Cite them; do not re-plan them.
- Recorded decisions to respect: <NAMES, IDS, POLICIES>.
- Mark recommendations "(owner to confirm)". Do not invent versions, CLI flags or benchmark
  numbers; write "verify at pinned version".
- Each AC on one line, one "shall".
- Edit only your item files and your fragment. No state-changing git, no code changes.
<AREA HINTS: paths and symbols the explorers found>

FINAL REPLY: files written, then at most 10 bullets that contradict or refine the plan's findings,
each with path evidence. Under 300 words.
```
