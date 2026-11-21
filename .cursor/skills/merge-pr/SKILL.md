---
name: merge-pr
description: "Merge one GitHub PR whose CI cannot run for reasons unrelated to the change (Actions billing or spending limit, exhausted minutes, runner outage): prove no job ran, run the workflow's steps locally in a clean worktree on the exact head commit, then merge pinned to that commit. Runs only when the user types /merge-pr, with a PR number, URL or branch; with none it uses the current branch's PR. For CI that ran and failed, use /ship-pr."
capability: "Merge a pull request on local CI evidence when GitHub Actions cannot start, within fixed limits."
side_effect_level: remote_write
approval_required: true
requires_tools: "git and gh; the commands in the repository's workflow files."
output_schema: "Why remote CI could not run (run ids and the annotation), a table of each local workflow step with its exit code, the merge commit, and anything left with the reason and a paste-ready command."
risk_class: high
disable-model-invocation: true
---

# Merge PR

`/merge-pr` means: **merge this PR on local evidence, because GitHub can't run its CI.** Typing it
approves merging this PR and nothing else. It is not standing approval, and it never pushes fixes. Never
run this skill on your own initiative.

One PR per run. If the user names none, use the current branch's PR. If that's ambiguous, stop and ask.

How this differs from `/ship-pr`: `/ship-pr` fixes CI failures and merges once CI is green. `/merge-pr`
applies only when no CI job could start. A job that started and failed is a real failure: stop and
point to `/ship-pr`.

## 1. Confirm the target

Read `gh pr view <n> --json state,author,baseRefName,headRefName,headRefOid,isDraft,mergeable,reviewDecision,body`
and `gh repo view --json visibility,nameWithOwner`.

- Already merged: report it and stop.
- Stop and report if it isn't the user's PR, is a draft, has changes requested, conflicts with its
  base, or another session is working on its branch (`ListAgents`). A PR based on another open PR waits
  for that one.
- **Review sign-off still applies.** If the project tracks work (an `.agent-flow/` folder) and the PR
  body says `Refs <ticket>`, that ticket must be approved (`done`) by a reviewer other than its author.
  If it's still in review, stop; this skill never stands in for a review.
- Record the head commit `<sha>`. Every later step is about that commit.

## 2. Prove CI could not run

List the runs for the head commit: `gh run list --commit <sha> --json databaseId,name,status,conclusion`.
`gh pr checks` and the check-runs API often return HTTP 403 with this token; use the runs API.

The case holds only when **all** of these are true:

- the head commit has `.github/workflows` (otherwise the repo has no CI, and `/ship-pr` already covers
  that case);
- at least one run exists for `<sha>`, and every run is `completed`. Pending or queued runs mean wait;
- for every run, no job executed a step: `gh run view <id> --json jobs` shows jobs with no steps, or
  `gh run view <id>` shows an annotation that the job was not started, such as "recent account payments
  have failed or your spending limit needs to be increased", "spending limit", or "You have exceeded
  the included minutes";
- the same cause failed a run on another recent commit, so it isn't specific to this change.

Quote the annotation and the run ids in the report. If any job ran a step and failed, stop: that is a
real failure for `/ship-pr`. Don't re-run jobs repeatedly: one rerun to confirm the cause is enough.

## 3. Run CI locally on the exact commit

Ignored files make a shared checkout lie (agent folders, caches, local config), so use a fresh,
detached worktree:

```bash
git fetch origin <headRefName>
git worktree add --detach <scratch>/merge-pr-<n> <sha>
```

Read every workflow that triggers on `pull_request` or `push` to the base branch. Run each job's `run:`
steps in order, with the same commands, working directory and environment values, from the worktree:

- **Never pipe or chain a check into anything that hides its exit code** (`| tail`, `; echo`). Record
  each step's exit code.
- Matrix jobs: run the entry that matches this host. List the other entries as not verified.
- `uses:` steps that only set up a toolchain (checkout, setup-python, setup-node) are covered by the
  local toolchain; say which version you used. Any other `uses:` step, or a step that needs secrets
  or a service you don't have, is not covered: list it. If a **required** check can't be reproduced
  at all, stop and report. Don't merge on partial evidence.
- Also run the project's declared test command if the workflow doesn't, plus any test suite the
  project's notes say CI misses.

Then review the PR in place of a reviewer's CI glance:

- the diff (`git diff <base>...<sha> --stat`) holds only the files the PR is meant to change;
- the added lines have no machine paths, identities or credentials.

Any failing step: stop and report it with its output. Don't fix it here; that's `/ship-pr` or new work.

## 4. Merge

Immediately before merging, re-read the PR and confirm:

- the head is still `<sha>`, with no new commits;
- it is mergeable with no conflicts, and no review has changed;
- the runs for `<sha>` still show only the not-started cause.

Use the merge method the repository's recent PRs used, pinned to the checked commit:

```bash
gh pr merge <n> --merge --delete-branch --match-head-commit <sha>
```

Run it from a clean checkout. `gh` switches the local branch after merging, and a modified tracked
file (for example an `.agent-flow/tickets.jsonl` snapshot) makes that local step fail after the remote
merge succeeded. If it fails, check `gh pr view <n> --json state,mergeCommit` before retrying anything.

## 5. Verify and clean up

- Confirm the PR shows as merged and record the merge commit. Note that the base branch's CI will also
  be blocked; say so rather than waiting for it.
- Return a shared checkout to its default branch, and fast-forward it only if it's clean.
- Remove the worktree this run created with `git worktree remove <path>`, but only if no step created
  links inside it (`node_modules` from a package install, junctions). Otherwise leave it and give the
  user the steps: remove the links first, then the worktree.

## Report

```markdown
**PR #<n>** merged as <merge sha> on local evidence. GitHub CI did not start: "<annotation>" (runs <ids>).

| Workflow step | Command | Exit |
|---|---|---|
| ci / lint | `python scripts/check_x.py` | 0 |

Not covered: <steps, or "none">
```

Say what CI would have covered that the local run didn't (other platforms, matrix entries, secrets),
and name what would make CI run again (for example, raising the Actions spending limit).

## Never

Leave these for the user, with a paste-ready command where one helps:

- Merging when any CI job actually ran and failed, or while runs are still pending.
- Merging past a review that isn't approved, or with admin rights to get past branch protection.
- Pushing commits to the PR, force-pushing, or editing workflows or required checks.
- Weakening a check: skipping tests, loosening assertions, raising timeouts.
- Editing permission policy, tagging, releasing or deploying.
- Merging any PR other than the one named.

## When to stop

Stop and report when a hook or deny rule blocks a command (**never reword a command to get past a
block**), the not-started cause can't be shown, a required step can't be reproduced locally, a local
step fails, or the PR's state changes under you. Stopping is a good result.
