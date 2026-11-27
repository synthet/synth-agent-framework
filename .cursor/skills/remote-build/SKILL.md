---
name: remote-build
description: "Build the current project on a paired build PC over SSH, bring the verified artifacts back, and hand each target PC its project-procedure install: resolve the build host by hostname, pre-flight it (reachable, no other heavy build, RAM and disk, clean checkout fast-forwarded to the exact commit), start the build detached so a dropped SSH session can't kill it, wait on a done-file, check the artifacts' commit, size, SHA-256 and version output, copy them into a checkout at the same commit on each target, then run that target's install only after the user approves that PC. Runs only when the user types /remote-build. For a build on this PC use /build; for an install on this PC only use /deploy."
capability: "Build a project on a remote build PC over SSH, verify the artifacts, distribute them to target PCs and install each one only with per-PC approval."
side_effect_level: remote_write
approval_required: true
requires_tools: "git and an OpenSSH client on this PC; key-based ssh to the build host and target PCs by alias; git, the project's toolchain and PowerShell on the build host; the project's install procedure on each target."
output_schema: "The build host and how it was resolved, the pre-flight numbers, the commit built, the build command and its exit code, each artifact's path, size, SHA-256 and version output, where it was copied and the hash read back there, the install result per target PC (or why it was skipped), and anything left with a paste-ready command."
risk_class: medium
disable-model-invocation: true
---

# Remote build

`/remote-build` means: **build this project on another PC, then put the result on the PCs the user
names.** Typing it approves the build on the build host and copying artifacts to the targets. It does not
approve installing: each target PC's install is its own yes, because an install stops the running app
there.

How this differs from neighbours:
- `/build` builds on this PC. `/remote-build` builds on a paired build host.
- `/deploy` installs on this PC only. `/remote-build` runs the same project procedure on each target, one
  approved PC at a time.
- `cross-pc-ssh` reads a peer's checkout and runs read-only agents there. `/remote-build` writes build
  output on the build host and artifacts on the targets.
- `cross-pc-messages` is for asking the other PC's agent or owner to do a step, such as fixing its SSH
  server. Use it when SSH itself is the problem.

## 1. Resolve the hosts

- **By hostname, not IP.** DHCP moves addresses; a hard-coded `HostName 192.168.x.y` times out the day the
  router hands out another. Check that the name resolves (`Resolve-DnsName <HOST>`; Windows answers
  LLMNR and `.local`), then connect with `-o HostName=<HOST>`. Add `-o HostKeyAlias=<alias>` so the
  known-hosts entry stays the same when the address changes.
- `~/.ssh/config` and `known_hosts` are the user's. Agent hooks often block reading or editing them;
  pass the options on the command line, and give the user the two lines to add.
- **First contact:** `ssh -n -o BatchMode=yes <host> hostname` must print the host's name. Stop on a
  password prompt or a host-key failure; key setup and host keys are the user's.
- **A timeout is not proof the PC is down.** Try another channel before concluding (agent mail delivery,
  a ping by name). If the PC is up and only port 22 fails, ask its agent or owner through
  `cross-pc-messages` to check `sshd`, the firewall rule and the network profile; don't guess.
- **Know each host's SSH shell.** Windows OpenSSH starts `cmd.exe` unless `DefaultShell` names PowerShell.
  Check with `ssh <host> "echo %COMSPEC%"` and write commands for that shell. For anything with `$`,
  quotes or several statements, send PowerShell as `powershell -NoProfile -EncodedCommand <base64>`
  (UTF-16LE): a nested `powershell -Command "..."` gets its `$variables` expanded by the outer
  PowerShell first.

## 2. Pre-flight the build host

Report the numbers. Every check is read-only except the fast-forward pull, which is the one write to
the build host's checkout:

- Logical CPUs, free RAM and free disk on the drive holding the checkout and its target folder.
- **No other heavy build running** (`Get-Process cargo,rustc,msbuild,node`). Another session may be
  building there; don't start a second one beside it.
- The checkout is clean (`git status --short` is empty) and on the default branch. If it's dirty or on
  another branch, stop and report it; never stash or reset someone's work.
- Fast-forward to the exact commit the user wants: `git pull --ff-only origin <default>`, then check
  `git rev-parse HEAD`. Build only a merged commit unless the user names another.
- Use the project's build wrapper on that host if one exists (named in the project's notes or
  `.agent/deploy.json`). It knows the toolchain paths, feature set and job count that host can afford.

## 3. Start the build detached

An SSH session can drop in the middle of a 30-minute build, and its child processes die with it.

- Start the build as its own process on the host, for example
  `Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='powershell.exe -NoProfile -ExecutionPolicy Bypass -File <wrapper>.ps1'}`.
  Record the returned process id.
- The wrapper writes a log and, last, a **done-file** holding the exit code (`exit=<n>`). Delete an old
  done-file before starting, so a stale one can't read as success.
- Keep the job count the host can afford; the wrapper's default is usually right. Don't raise it to go
  faster.

## 4. Wait

- Poll for the done-file from a background loop (every minute or so, with an overall cap), not by holding
  an SSH session open. Do independent work meanwhile.
- When it appears: `exit=0` goes on; anything else, read the tail of the log and stop. Don't retry a
  failed build blindly; report the error.

## 5. Check the artifacts on the build host

- Each artifact the procedure names exists, with a timestamp after the build started.
- Its commit is the one you fast-forwarded to (the build's own version string or source record, when it
  carries one).
- Record its size and SHA-256 (`Get-FileHash`), and run its version or help flag. A binary that doesn't
  start isn't built.

## 6. Distribute

- On each target, use a checkout **at the same commit**: a fresh detached worktree
  (`git worktree add --detach <path> <sha>`) when the main checkout is on another commit or dirty. The
  project's install script expects its own repository files (assets, notices, scripts) next to the build.
- Copy the artifacts into that checkout's build output folder (for Cargo, `target/release/`) with `scp`,
  then compare the SHA-256 on the target with the build host's. A copy that returned no error isn't proof.
- Large artifacts go PC to PC over SSH, not through a synced share.

## 7. Install, one approved PC at a time

For each target the user named, state what the install will do there (stop the running app, replace the
install folder, update shortcuts), then wait for the user's yes for **that** PC.

- Run the project's own install procedure in that target's checkout, in its "use the existing build" mode
  (`deploy.ps1 -SkipBuild` and the like). It wins over a hand copy, as in `/deploy`.
- **This PC:** don't run an install that replaces the app hosting this session; it kills the session
  mid-install. Give the user the command to run from another terminal.
- **Over SSH:** the install runs without the user's desktop. Use the procedure's no-restart option, and
  tell the user to start the app themselves. Shortcuts and per-user registry entries land for the SSH
  user, so check that it's the same account that uses the app.
- Read back each install: the installed version string and the executable's SHA-256 against step 5.

## 8. Report

```markdown
**Built** <project> at <sha> on <host> (resolved as <name> → <ip>): exit 0 in <minutes> min.

| Artifact | Size | SHA-256 | Version |
|---|---|---|---|

| Target | Copied (hash match) | Installed | Read back |
|---|---|---|---|
```

List anything left with a paste-ready command: an install the user should run from another terminal, an
SSH config line to add, a target that was unreachable.

## Never

- Install on a PC the user didn't approve in this run, or on a server or production machine.
- Stash, reset, clean or switch branches in a checkout you didn't create.
- Start a second heavy build beside one already running on the host.
- Edit `~/.ssh/config`, `known_hosts`, `sshd_config`, firewall rules or another PC's settings; hand
  those to the user or that PC's owner.
- Put keys, tokens or full config files in messages, logs or the report.
- Delete the build host's previous artifacts or a target's old install yourself; the project's install
  procedure keeps its own backup.
