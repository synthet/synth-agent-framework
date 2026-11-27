# Gotchas (from the run that produced this skill)

## agent-flow framework importer (`flow_import_tasks` / `agent-flow import-tasks`)

- It takes the **first table whose ID column has `T-n` rows**. `<ID>.<n>` task IDs give
  `no T-n task table found`. Generate `tasks-import.md` with `spec_checks.py import-table`
  instead of renaming the source tasks.
- Only cells matching `AC-n` exactly count, and AC **text** comes from `AC-n` lines (or
  `| AC-n | text |` rows) in the spec or tasks file, one line each. Item-scoped `<ID>/AC-n` is
  ignored and wrapped AC lines arrive truncated. The generator renumbers to global `AC-n`, keeps the
  item ID in the text (`[AGT-02/AC-3] …`) and joins wrapped lines.
- Dependencies that are not `T-n` in the same table are dropped with a warning. External ticket
  IDs survive only as text, so the generator appends "(also needs: …)" to the title.
- Dry runs create no tickets. MCP activity can still rewrite the git-tracked
  `.agent-flow/tickets.jsonl` snapshot; commit it only together with a confirmed import.
- `.agent-flow/STOP` halts the autonomous loop only, not an import.
- On Windows the CLI is `agent-flow.ps1` under npm: run it from PowerShell, not Git Bash, or use
  the MCP tools.

## Exploration and verification

- An Explore agent's "X is fixed" or "Y exists" is a lead. The run found four claims that changed
  on first-hand reading: an upstream sync was needed after all, a validator rejected exactly the
  inputs the plan wanted to reuse it for, a "fix" commit only retried a failure, and a baseline
  marked "exists" was gated on a cloud feature.
- Writer subagents report contradictions only when asked for them explicitly; ask, and budget for
  folding them into the spec.
- LLM wiki statuses: use `reviewed` and `candidate`; `current` is rejected as an unknown status.

## Writing to files from a JS REPL

- Python or Markdown containing backticks breaks a JS template literal, and some harnesses refuse
  `\uXXXX` escapes. Keep backticks out of generated source (`chr(96)`), use `chr(0x2014)` for
  special characters, or write the file from a separate source.

## Is a redeploy needed after this?

- An elaboration changes docs only. To answer "do we need to redeploy", compare the install's
  recorded source commit (for example a `SOURCE.txt` beside the executable) with
  `git log <sha>..<default> -- <code paths>`. No code commits after that sha means no redeploy.
