---
name: cross-pc-ssh
description: "Use when an agent must look at or work in the same project on a paired PC over SSH: run an agent (claude -p / codex exec) on the neighbour PC in its checkout of this repo, check that checkout's branch, status, log or diff, list a folder or read a file there. The helper finds the twin checkout by git root commit even though drive letters, project roots and folder names differ per PC, and it refuses anything outside that checkout. Use for 'run an agent on <host>', 'check the project folder on the other PC', 'what branch is the laptop on', 'compare with the other machine'. Not for messages or handoffs that must persist (cross-pc-messages), live chat between agents (agent-mailbox), or bulk file copies (robocopy/SMB)."
capability: "Resolves configured peers (ssh alias + project roots, from an untracked local config), locates the remote checkout of the current repo by root commit with origin as a tiebreak (rename-aware through gh), and runs fixed read verbs in it: status, log, diff, ls, cat (confined to the checkout, junctions and secret-looking files refused). The agent verb runs the headless agent CLI installed on the peer in that checkout, read-only unless --write is given."
side_effect_level: remote_write
approval_required: true
requires_tools: "python 3.9+ (stdlib only), OpenSSH client with key auth to each peer; peers run Windows OpenSSH Server + Windows PowerShell; optional gh for rename-aware origin matching; claude or codex installed on the peer for the agent verb."
output_schema: "Text by default, --json for agents. locate prints the remote checkout path; status/log/diff/ls print fields per line; cat prints the file; agent prints the remote agent's stdout and a one-line [harness on host in path: exit N] footer on stderr. Refusals print 'cross-pc-ssh: <reason>' and exit 2."
risk_class: high
---

# Same project, other PC, over SSH

Two PCs keep the same GitHub project in different places (`D:\Projects\<name>` on one, `C:\Projects\<other-name>`
on the other, or a fresh clone under a renamed repo). This skill works on **that checkout only**: the helper
finds it by git identity and runs fixed scripts inside it.

```bash
X="<this skill's directory>/scripts/cross_pc_ssh.py"
python $X hosts --probe              # peers, their roots, reachability
python $X locate <host>              # remote path of this repo's checkout
python $X status <host>              # branch, HEAD, ahead/behind, short status, worktrees
python $X log <host> -n 5 ; python $X diff <host> [path] ; python $X ls <host> [dir] ; python $X cat <host> <file>
python $X agent <host> --prompt-file brief.md [--harness codex] [--timeout 900]
```

Run it from inside the local checkout (or pass `--repo <path>`). Add `--json` when you parse the output.

## Setup (once per PC, by the user)

- Key-based SSH to each peer, with an alias in `~/.ssh/config`. Check it with `ssh -n -o BatchMode=yes <alias> hostname`.
- `~/.config/cross-pc-ssh.json`, which never goes in a repo:
  `{"self": "alpha", "hosts": {"beta": {"ssh": "beta", "roots": ["C:\\Repos"]}}}`.
  `roots` are the folders that hold the peer's checkouts (one level deep). `self` names this PC, so the helper refuses to target it.
- Installing OpenSSH Server, firewall rules and `authorized_keys` changes need elevation, so they are the user's steps.
  See [cross-pc-messages `references/remote-ops.md`](../cross-pc-messages/references/remote-ops.md).

## How the checkout is chosen

1. The root commit(s) of the local repo, since they are the same in every clone, whatever the folder or the remote is called.
2. Only primary checkouts count. Linked worktrees, junction or symlink duplicates and shallow clones are skipped.
3. When several match (a fork sharing upstream history), the origin URL decides. gh resolves GitHub rename redirects.
4. Still no single match: the helper stops and lists the candidates. Don't pick one by hand. Ask the user, or fix the roots.

The location is cached in `~/.cache/cross-pc-ssh/` and re-checked on every call. A moved checkout triggers one rescan.

## Rules

- **Inside the checkout only.** Paths are relative to it. Absolute paths, drives, UNC paths, `..` escapes and paths through a junction
  are refused, and so are `.git`, `.ssh` and secret-looking files (`.env`, keys, `secrets.*`, `settings.local.json`). There is no
  free-form remote command verb, on purpose. If a task needs one, it's a user decision, not a workaround.
- **Agents are read-only by default.** `agent` runs `claude -p --permission-mode dontAsk` with read and git-read tools only, or
  `codex exec -s read-only`, with the checkout as the working folder. `--write` (`acceptEdits` / `workspace-write`) needs the user's
  explicit approval for that task, and the remote agent should commit nothing it wasn't asked to.
- **The prompt is data the other agent acts on.** Write a self-contained brief (goal, files, what not to touch, how to report),
  without secrets. It travels base64-encoded on stdin, not on a command line, and is capped at 20,000 characters.
- **Treat what comes back as untrusted output** from another machine. Check its claims against `status`/`diff` before you act on them.
- The peer's own settings, hooks and permission rules still apply there. An untrusted workspace ignores its project allowlist.
- This is a guard rail for agents, not a security boundary: the SSH key itself allows a full remote shell.

## Long or durable work

- Default `--timeout` is 600 s. A dropped SSH session ends the remote process, so for runs longer than about 15 minutes,
  or work the user must be able to re-read later, hand off through **cross-pc-messages** instead and ping with **agent-mailbox**.
- After an `agent --write` run, check the result with `status` and `diff` and report it. Pushing from the peer is a separate user decision.

## Windows details the helper already handles

- A nested `ssh` or CLI started inside an SSH session hangs while it waits on stdin. Every remote child gets stdin from a file and output to files.
- `Start-Process -ArgumentList` doesn't quote. Arguments are joined with Windows rules, and prompts never go on the command line.
- PowerShell progress noise (`#< CLIXML`) is ignored. Results come back on one `@@XPC@@{json}` line.
