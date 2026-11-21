---
name: deploy
description: "Install a fresh build of the current project on this machine and set it up for local use: run /build, then the project's own install or upgrade procedure if it has one, else copy the artifacts to a per-user install folder outside the checkout (Windows %LOCALAPPDATA%\\Programs\\<app>), put CLI tools on the user's PATH folder, back up and repoint desktop shortcuts and their icons, and read back the version, shortcut and icon. Follows the project's .agent/deploy.json recipe when present. This machine only, never a server, production or another PC. Runs only when the user types /deploy."
capability: "Install a verified local build of the current project on this machine, with shortcuts and a backup of what it replaces."
side_effect_level: local_write
approval_required: true
requires_tools: "git; the project's toolchain (via /build); PowerShell on Windows for shortcuts; the app-icons skill for shortcut icons."
output_schema: "What was built (sha, artifacts), where it was installed, which shortcuts and PATH entries changed, the read-back checks, the backup locations and the undo steps, and anything left with a paste-ready command."
risk_class: medium
disable-model-invocation: true
---

# Deploy

`/deploy` means: **put a fresh build of this project into use on this machine.** Typing it approves
building, installing it for this user, and updating its desktop shortcuts. It is not standing approval,
and it covers this machine only: never a server, production, a cloud service or another PC.

How this differs from neighbours:
- `/build` produces artifacts and stops. `/deploy` starts with it.
- `/fix-it` fixes one problem a report named, which may be a stale install; `/deploy` is the whole
  install.
- `app-icons` designs and renders icons; `/deploy` only installs the ones the project ships.

## 1. Read the recipe

Read `.agent/deploy.json` if the project has one; its format is in
[`references/recipe.md`](references/recipe.md). Without one, work out the same fields from the project
(its README, release script, existing shortcuts) and say they were inferred.

- **`"deploy": "none"`** (libraries, bots that run elsewhere, services deployed by other means): say so
  and stop. A project with nothing to install is a valid answer.
- **A project procedure wins.** If the recipe or the project's docs name an install or upgrade procedure
  (a packaging script, a cutover runbook, a platform installer), follow it instead of the generic steps.
  It knows the steps a copy would miss, such as stopping services or migrating data.

## 2. Build

Run `/build` for the recipe's artifacts. Deploy only what that build just produced, from a known commit.
If the tree is dirty, say so in the report.

## 3. Check what is running

Before replacing anything, look for processes running from the install folder, and for files that are
locked. A running app, an MCP server held by an open session, a tray app or a scheduled task can't be
replaced safely.

- If the recipe has `stop` steps the user approved for this project, run them.
- Otherwise stop and hand that step to the user: name what is running and the command to stop it.

## 4. Install

Locations per OS are in [`references/recipe.md`](references/recipe.md).

1. **Back up first.** If the install folder exists, move it to `<folder>.bak-<date>`, so the previous
   version still runs if the new one doesn't. Keep the backup; deleting it is the user's call.
2. **Copy** the artifacts (and the files they need, such as `plugins/` or runtime folders) into the
   install folder. Install outside any git checkout, so a branch switch or a clean can't break what the
   user launches.
3. **CLI tools:** copy or link each one the recipe lists into the user's PATH folder. Never edit PATH or
   other environment settings.

## 5. Shortcuts and icons

For each shortcut in the recipe:

- Copy its icon to the per-user icon folder under a **new file name** whenever the image changed. Windows
  Explorer keeps showing a cached icon until the shortcut points at a different file. Icon rules are in
  the `app-icons` skill.
- Back up the existing shortcut, then point it at the installed executable and the icon.
- On Windows, refresh the icon cache (`ie4uinit.exe -show`).

## 6. Verify what the user will see

Read everything back; a write that returned no error isn't proof.

- The installed executable's own version or help output names the new version.
- Each shortcut's target, working folder and icon read back as set.
- The icon the executable embeds is the intended one, when the project ships one.
- Running `/deploy` again on the same commit changes nothing except the backup.

## Never

- Deploy anywhere but this machine, or push, publish, tag or release.
- Change MCP registrations, scheduled tasks or services, harness settings, permission policy or PATH.
- Stop a process the user didn't approve stopping, or replace a file that is locked.
- Delete a backup or an old install.

## Report

```markdown
**Deployed** <project> <version> (<sha>) to <install folder>.

| Change | Before | After | Check |
|---|---|---|---|
| Install folder | <old version or none> | <new> | `<exe> --version` → <output> |
| Shortcut <name> | <old target/icon> | <new> | read back |

Undo: <restore the backup folder and shortcuts>
```
