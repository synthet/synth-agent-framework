---
name: repo-inventory
description: "Use when comparing, syncing or updating checkouts of the same repositories on two or more PCs: which repos are missing or stale on a PC, local branches and unpushed commits, worktrees, uncommitted changes, stashes, conflicts or an unfinished merge or rebase, ahead/behind origin. Also for 'what is out of sync between my PCs', 'inventory my projects folder', 'which repos need a pull on <host>', 'plan a sync of my checkouts'. Matches repos by normalized remote, so folder names may differ, and renamed GitHub repos join through an alias map. Not for files or an agent inside one repository on a peer (cross-pc-ssh), or for messages between PCs (cross-pc-messages, agent-mailbox)."
capability: "Snapshots every checkout under a projects folder to JSON with read-only git (only --fetch updates remote-tracking refs), writes it atomically to the cross-PC share or prints a digest under 16 KB for agent mail, reads a peer's snapshot over ssh, compares hosts per repo (missing clones, folder names, HEAD vs origin default, stale fetches, branches on one host, unpushed commits, dirty, conflicted and in-progress state) and prints a per-host sync plan that it never runs."
side_effect_level: remote_write
approval_required: false
requires_tools: "python 3.9+ (stdlib only) and git on every PC; for --ssh an OpenSSH client, key-based ssh to the peer, and this skill installed on the peer."
output_schema: "snapshot prints JSON (schema repo-inventory, schema_version 1) or the --summary digest; compare prints a table or --json and exits 1 on drift; plan prints SAFE / NEEDS HUMAN / note lines per host, or --json. Exit 2 on usage errors."
risk_class: medium
---

# Repo inventory across PCs

One snapshot per PC lists every checkout under its projects folder. Compare two or more snapshots to see what
drifted, then print a plan of sync steps for each PC. The helper never changes a working tree, and it never runs
the plan.

```bash
R="<this skill's directory>/scripts/repo_inventory.py"   # e.g. under ~/.claude/skills or ~/.agents/skills
python -I $R snapshot --root <projects folder> --share     # this PC -> <share>/private/host-profile/repos-<host>.json
python -I $R compare                                      # every repos-<host>.json in the share; exit 1 on drift
python -I $R plan                                         # per-host steps: SAFE commands and NEEDS HUMAN items
```

## Snapshot

`snapshot [--root DIR] [--depth 2] [--fetch] [--out FILE] [--share] [--summary] [--max-paths 20]`

- Finds checkouts up to `--depth` folder levels below the root (default 2). It records a checkout whatever its
  name, but doesn't search inside hidden folders, `node_modules`, virtualenvs or build output. Linked worktrees, including those under `<repo>.worktrees`
  folders, are recorded under their main repository, not as separate repos. A folder that is a junction or
  symlink to another checkout is scanned once: the real folder's entry lists it in `aliases`, next to its
  `real_path`. Links are never searched inside.
- Per repo it records: folder and path; remote URL (credentials stripped) and its key; default branch
  (`origin/HEAD`, else main or master) with the origin SHA and commit date; HEAD branch, SHA or detached;
  each local branch with upstream, ahead/behind, gone upstream, merged-into-default and unpushed commit count;
  worktrees (path, branch, HEAD, dirty counts, locked, prunable); staged, unstaged, untracked and unmerged counts
  with up to `--max-paths` paths; stash count; a merge, rebase, am, cherry-pick, revert or bisect in progress
  with its unmerged paths; last commit date. A repo git can't read is kept with an `error`.
- Read-only: every git call runs with `GIT_OPTIONAL_LOCKS=0` (no index refresh), no prompts and a timeout
  (`--timeout`, 30 s). Only `--fetch` writes, by running `git fetch --prune` on each repo's remote.
  Without it, "behind" means behind as of that PC's last fetch.
- The root comes from `--root`, then `REPO_INVENTORY_ROOT`, then `root` in the config.

## Exchange snapshots, in this order

1. **Shared folder (default).** `snapshot --share` writes `<share>/private/host-profile/repos-<host>.json`
   to a temp file and renames it over the previous one. That file belongs to its host, and only that host
   replaces it. This is the one exception to the cross-pc-messages "only add files" rule. `compare` and
   `plan` with no inputs read that folder. The share root comes from `--share-root`, `CROSS_PC_SHARE`,
   `~/.config/cross-pc-mail.json`, then the usual Resilio/Syncthing folders. The host id comes from
   `--host`, `CROSS_PC_HOST`, then `COMPUTERNAME` lowercased, matched to the share's host table.
2. **Live over ssh.** `compare --ssh <peer> [--local]` runs the snapshot on the peer and reads its stdout.
   `--local` adds a fresh snapshot of this PC. The peer needs this skill installed. The default remote
   command is `python -I .claude/skills/repo-inventory/scripts/repo_inventory.py snapshot`, relative to the
   remote home folder, which is where an ssh session starts on Windows OpenSSH (cmd.exe) and on POSIX. The peer
   takes its root from its own `REPO_INVENTORY_ROOT` or config. To change the command, pass `--ssh-command`
   or set `ssh_command`, or `ssh_commands: {"<peer>": "..."}` per peer, in the config. Example:
   `py -3 -I .agents\skills\repo-inventory\scripts\repo_inventory.py snapshot --root D:\src`.
   ssh runs with `BatchMode=yes`. If it asks for a password or a host key, that setup is the user's step.
3. **Summary for agent mail.** `snapshot --summary` prints a digest under 16 KB: one line per repo that has
   something to report, then the keys of the quiet repos. Use it as a message body when neither of the above
   is available. It is for a reader, not for `compare`.

Inputs can also be explicit files or folders of `repos-<host>.json`: `compare a.json b.json`.

## Compare

`compare [INPUT...] [--ssh PEER]... [--local] [--aliases FILE] [--json] [--all]`

Repos are grouped by **key**, the remote path lowercased without `.git`. https, ssh and scp forms of the
same remote agree, so on GitHub the key is `owner/name`. A local-path origin uses its last two folders. A
repo without a remote is listed per host and is never reported missing. Renamed repos: GitHub redirects
the old URL, so the PCs can disagree. Pass `--aliases` with a JSON object `{"old-name": "new-name"}`, or
set `aliases` in the config. Keys can be `owner/name` or bare names (the owner is kept).

**Drift**, reported per repo and host. Any drift makes the exit code 1:

- `missing`: no clone on that host.
- `duplicate-clones`: two checkouts of the same repo, at different real paths, on one host. Entries with the
  same real path (a linked folder in an older snapshot) are merged into one checkout with an alias instead.
- `folder-differs`: the folder name is not the same on every host.
- `default-differs`: the default branch is not the same on every host.
- `stale-origin` / `origin-differs`: the hosts' origin SHAs differ. The host with the older origin commit
  needs a fetch.
- `detached`, `off-default`: HEAD is not on the default branch.
- `ahead`, `behind`, `diverged`: the default branch compared with origin.
- `branch-only`: local branches that some other host lacks.
- `unpushed`: branches with commits on no remote, including branches without an upstream or with a gone
  upstream.
- `dirty`, `conflict`, `in-progress`, `worktree-dirty`, `worktree-prunable`, `error`.

Stash and worktree counts are shown in the table but are not drift. `--all` also shows repos in sync.

## Plan

`plan` takes the same inputs and prints per host:

- **SAFE** `git -C "<path>" fetch --prune` for every repo with a remote.
- **SAFE** `git -C "<path>" pull --ff-only`, only when the repo is on its default branch, has no staged,
  unstaged or unmerged changes and no operation in progress, is not ahead, and is behind or its origin is older
  than another host's. Untracked files don't block it, because git refuses a pull that would overwrite them.
- **NEEDS HUMAN** with the reason for anything else. Each fact is reported on its own, so a dirty tree, a
  conflict, the branch state, divergence and unpushed commits (the current branch included) all appear together: dirty, diverged, unpushed commits, conflict or an operation
  in progress, detached HEAD, a branch other than the default, gone upstreams, dirty or prunable worktrees, an
  error. A missing clone gets a suggested `git -C "<root>" clone -- "<url>" "<folder>"` (a local-path origin is
  written with forward slashes).
- Paths, URLs or folder names with a quote, backslash, `$`, `%`, `!` or control characters, or a leading `-`, are
  never printed as commands. They become NEEDS HUMAN. Snapshots read from the share or over ssh are
  validated, and control characters in their strings are replaced, so they can't forge output lines.

Run SAFE steps on their own host only after the user agrees. NEEDS HUMAN items are for the user to decide.
Running them through cross-pc-ssh is a separate, approved task.

## Privacy

Snapshots hold paths, branch names, SHAs, counts and at most `--max-paths` file paths per repo. They never
hold file contents, environment variables, or credentials from remote URLs: http userinfo and query strings
are dropped, ssh keeps only the user, and git error text is scrubbed the same way. The share's
`private/` folder is the right home. Don't copy snapshots into repos, issues or external tools.

## Limits

- The merged flag compares commits, so a branch merged by a squash or rebase on GitHub still counts as not
  merged and can count as unpushed.
- Without `--fetch`, ahead and behind are only as fresh as each PC's last fetch. Compare the origin SHAs and
  dates (`stale-origin`), or snapshot with `--fetch`.
- Submodules are not inventoried separately.

Tests: `python -I -m unittest discover -s <this skill's directory>/scripts -p "test_*.py"`

Resolve helper paths from this skill directory. Run project commands from the target workspace.
