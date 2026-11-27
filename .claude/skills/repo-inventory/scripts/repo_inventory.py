#!/usr/bin/env python3
"""Inventory the git checkouts on this PC and compare them with other PCs.

Repos are matched across PCs by their normalized remote (owner/name, lowercase, no .git; https and ssh agree),
not by folder name, and an alias map ({"old-name": "new-name"}) joins repos renamed on GitHub.

Commands:
  snapshot [--root DIR] [--depth 2] [--fetch] [--out FILE] [--share] [--summary]
      Walk a projects folder and print (or write) a JSON snapshot of every checkout: remote, default branch and
      its origin SHA, HEAD, local branches with upstream and ahead/behind, worktrees, dirty counts, stashes,
      in-progress merge/rebase and unmerged paths. Read-only; only --fetch updates remote-tracking refs.
      --share writes <share>/private/host-profile/repos-<host>.json atomically. --summary prints a digest
      under 16 KB (repos with something to report) for an agent-mail body.
  compare [INPUT...] [--ssh PEER]... [--local] [--json] [--all]
      INPUT is a snapshot file or a folder of repos-<host>.json; with no INPUT, the share's host-profile folder.
      --ssh runs snapshot on the peer over ssh and reads its stdout. Exit 1 when anything drifts.
  plan [INPUT...] [--ssh PEER]... [--local] [--json]
      Per-host steps: safe ones (git fetch, git pull --ff-only when clean, on the default branch and not ahead)
      and NEEDS HUMAN items with the reason. Prints commands; never runs them.

Share root: --share-root, env CROSS_PC_SHARE, ~/.config/cross-pc-mail.json {"root"}, "share" in the config,
then common Resilio/Syncthing locations. Host id: --host, env CROSS_PC_HOST, then COMPUTERNAME/HOSTNAME
lowercased (matched against the share's host table when there is one).
Config: env REPO_INVENTORY_CONFIG or ~/.config/repo-inventory.json:
  {"root": "...", "depth": 2, "aliases": {"old": "new"}, "ssh_command": "...", "ssh_commands": {"peer": "..."}}

Stdlib only. Snapshots hold paths, branch names, SHAs, counts and at most --max-paths file paths per repo:
never file contents, environment variables or credentials from remote URLs.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VERSION = "1"
SCHEMA = "repo-inventory"
SCHEMA_VERSION = 1
SUMMARY_LIMIT = 16 * 1024
PROFILE_DIR = Path("private/host-profile")  # relative to the share root
CANDIDATES = ("~/Resilio Sync", "C:/Resilio Sync", "D:/Resilio Sync", "~/Sync", "~/Syncthing")
MAIL_CONFIG = Path("~/.config/cross-pc-mail.json")
CONFIG = Path("~/.config/repo-inventory.json")
PAIR = re.compile(r"^([a-z0-9][a-z0-9-]*?)-to-([a-z0-9][a-z0-9-]*)$")
HOST_ROW = re.compile(r"^\|\s*`([a-z0-9][a-z0-9-]*)`\s*\|\s*([^|]+?)\s*\|")
HOST_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
PEER = re.compile(r"[A-Za-z0-9][\w.@-]*")
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "env", "target", "dist", "build", "vendor", "bower_components"}
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
# An ssh session starts in the remote user's home folder on Windows OpenSSH (cmd.exe) and on POSIX, so a path
# relative to home works with either shell. Override with --ssh-command or the config.
DEFAULT_SSH_COMMAND = "python -I .claude/skills/repo-inventory/scripts/repo_inventory.py snapshot"
GIT_CFG = ["-c", "core.quotepath=off", "-c", "core.fsmonitor=false", "-c", "color.ui=never"]
# Characters that a shell would expand or that end a double-quoted argument; such values are not printed as commands.
# A backslash is refused too: at the end of a value it would escape the closing quote.
UNSAFE_ARG = re.compile(r'["`$%!\\\x00-\x1f\x7f\ufffd]')
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
OPERATIONS = (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("CHERRY_PICK_HEAD", "cherry-pick"),
              ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect"))


class UsageError(Exception):
    pass


class GitError(Exception):
    pass


# ---------------------------------------------------------------- pure logic (unit-tested)

def strip_credentials(url: str | None) -> str | None:
    """Drop user:password@ (and any http userinfo, which may be a token) plus http query strings."""
    if not url:
        return url
    u = url.strip()
    m = re.match(r"^([a-z][a-z0-9+.-]*)://([^@/]*)@(.*)$", u, re.I)
    if m:
        scheme, user, rest = m.groups()
        if scheme.lower() in ("http", "https", "ftp", "ftps") or not user.split(":")[0]:
            u = f"{scheme}://{rest}"
        else:
            u = f"{scheme}://{user.split(':')[0]}@{rest}"
    if re.match(r"^(https?|ftps?)://", u, re.I):
        u = re.split(r"[?#]", u, 1)[0]
    return u


def scrub(text: str) -> str:
    """Remove userinfo from any URL inside free text (git error messages echo the remote URL)."""
    return re.sub(r"([a-z][a-z0-9+.-]*://)[^\s/@'\"]+@", r"\1", text, flags=re.I)


def parse_remote(url: str | None) -> tuple[str | None, str | None]:
    """(host, key) for a remote URL. key is the repo path, lowercase, without .git: owner/name on GitHub.
    Local paths and file:// URLs use their last two segments, so <dir>/acme/widget.git is acme/widget."""
    if not url or not url.strip():
        return None, None
    u = url.strip()
    host, path = None, None
    if not re.match(r"^[A-Za-z]:[\\/]", u):
        m = re.match(r"^([a-z][a-z0-9+.-]*)://(?:[^@/]*@)?([^/:?#]*)(?::\d+)?(/[^?#]*)?", u, re.I)
        if m and m.group(1).lower() != "file":
            host, path = m.group(2).lower(), m.group(3) or ""
        elif m:
            u = u[len("file://"):]
        else:
            m = re.match(r"^(?:[^@/]+@)?([^/:\\]+):(?!//)(.+)$", u)
            if m:
                host, path = m.group(1).lower(), m.group(2)
    if host is None:
        parts = [p for p in re.split(r"[\\/]+", u) if p and p not in (".", "..")]
        path, host = "/".join(parts[-2:]), "local"
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return host, (path.lower() or None)


def normalize_aliases(raw: dict | None) -> dict[str, str]:
    out = {}
    for k, v in (raw or {}).items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            out[_nogit(k.strip().lower().strip("/"))] = _nogit(v.strip().lower().strip("/"))
    return out


def _nogit(s: str) -> str:
    return s[:-4] if s.endswith(".git") else s


def apply_alias(key: str | None, aliases: dict[str, str]) -> str | None:
    """Follow old -> new names. An alias is owner/name or a bare name (then the owner is kept)."""
    if not key:
        return key
    seen = set()
    while key not in seen:
        seen.add(key)
        owner, _, name = key.rpartition("/")
        new = aliases.get(key) or aliases.get(name)
        if not new:
            break
        key = new if "/" in new or not owner else f"{owner}/{new}"
    return key


def parse_status(raw: str, max_paths: int = 20) -> dict:
    """Parse git status --porcelain=v2 --branch -z."""
    st = {"sha": None, "branch": None, "detached": False, "upstream": None, "ahead": None, "behind": None,
          "dirty": {"staged": 0, "unstaged": 0, "untracked": 0, "unmerged": 0},
          "paths": [], "paths_truncated": False, "unmerged_paths": []}
    tokens = raw.split("\0")
    i = 0

    def add_path(p):
        if len(st["paths"]) < max_paths:
            st["paths"].append(p)
        else:
            st["paths_truncated"] = True

    while i < len(tokens):
        t = tokens[i]
        i += 1
        if not t:
            continue
        if t.startswith("# "):
            name, _, value = t[2:].partition(" ")
            if name == "branch.oid":
                st["sha"] = None if value == "(initial)" else value
            elif name == "branch.head":
                if value == "(detached)":
                    st["detached"] = True
                else:
                    st["branch"] = value
            elif name == "branch.upstream":
                st["upstream"] = value
            elif name == "branch.ab":
                m = re.match(r"\+(\d+) -(\d+)", value)
                if m:
                    st["ahead"], st["behind"] = int(m.group(1)), int(m.group(2))
            continue
        kind = t[:2]
        d = st["dirty"]
        if kind in ("1 ", "2 "):
            fields = t.split(" ", 8 if kind == "1 " else 9)
            xy = fields[1]
            d["staged"] += xy[0] != "."
            d["unstaged"] += xy[1] != "."
            add_path(fields[-1])
            if kind == "2 ":
                i += 1  # the rename's original path
        elif kind == "u ":
            path = t.split(" ", 10)[-1]
            d["unmerged"] += 1
            st["unmerged_paths"].append(path)
            add_path(path)
        elif kind == "? ":
            d["untracked"] += 1
            add_path(t[2:])
    return st


def _ts(s: str | None) -> float | None:
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    except (ValueError, AttributeError):
        return None


def sha7(s: str | None) -> str:
    return s[:7] if s else "-"


def _clean(value):
    """Replace control characters in every string, so a crafted snapshot can't forge output lines."""
    if isinstance(value, str):
        return CONTROL.sub("\ufffd", value)
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    return value


_REPO_TYPES = {"folder": (str,), "path": (str, type(None)), "key": (str, type(None)), "remote": (dict, type(None)),
               "head": (dict, type(None)), "dirty": (dict, type(None)), "branches": (list,), "worktrees": (list,),
               "unmerged_paths": (list,), "in_progress": (list, type(None)), "error": (str, type(None)),
               "real_path": (str, type(None)), "aliases": (list,)}


def validate_snapshot(data, source: str) -> dict:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise UsageError(f"{source}: not a {SCHEMA} snapshot")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise UsageError(f"{source}: schema_version {data.get('schema_version')!r}, expected {SCHEMA_VERSION}")
    host = data.get("host")
    if not isinstance(host, str) or not HOST_ID.fullmatch(host):
        raise UsageError(f"{source}: invalid host id {host!r}")
    repos = data.get("repos")
    if not isinstance(repos, list) or not all(isinstance(r, dict) for r in repos):
        raise UsageError(f"{source}: repos must be a list of objects")
    for r in repos:
        for k, types in _REPO_TYPES.items():
            if k == "folder" and k not in r or k in r and not isinstance(r[k], types):
                raise UsageError(f"{source}: repo {r.get('folder')!r} has a malformed {k!r}")
        for k in ("branches", "worktrees"):
            if not all(isinstance(x, dict) for x in r.get(k) or []):
                raise UsageError(f"{source}: repo {r.get('folder')!r} has a malformed {k!r}")
    return _clean(data)


# ---------------------------------------------------------------- compare and plan

def _dirty_text(d: dict, untracked=True) -> str:
    parts = [f"{d.get(k, 0)} {k}" for k in ("staged", "unstaged") if d.get(k)]
    if untracked and d.get("untracked"):
        parts.append(f"{d['untracked']} untracked")
    return ", ".join(parts)


def _entry_issues(h: str, e: dict, add) -> None:
    if e.get("error"):
        add(h, "error", e["error"])
        return
    head = e.get("head") or {}
    default = e.get("default_branch")
    on_default = bool(head.get("branch")) and head.get("branch") == default
    if head.get("detached"):
        add(h, "detached", f"HEAD detached at {sha7(head.get('sha'))}")
    elif head.get("branch") and default and not on_default:
        add(h, "off-default", f"on {head['branch']}, not {default}")
    vd = head.get("vs_default") or {}
    a, b = vd.get("ahead") or 0, vd.get("behind") or 0
    if on_default:
        ref = e.get("origin_ref") or f"origin/{default}"
        if a and b:
            add(h, "diverged", f"{default} is {a} ahead and {b} behind {ref}")
        elif a:
            add(h, "ahead", f"{default} is {a} ahead of {ref}")
        elif b:
            add(h, "behind", f"{default} is {b} behind {ref}")
    d = e.get("dirty") or {}
    if _dirty_text(d):
        add(h, "dirty", _dirty_text(d))
    unmerged = e.get("unmerged_paths") or []
    if d.get("unmerged") or unmerged:
        add(h, "conflict", f"{max(len(unmerged), d.get('unmerged', 0))} unmerged: {', '.join(unmerged[:5])}")
    if e.get("in_progress"):
        add(h, "in-progress", ", ".join(e["in_progress"]))
    unpushed = [f"{b['name']} ({b['unpushed']})" for b in e.get("branches") or [] if b.get("unpushed")]
    if unpushed:
        add(h, "unpushed", ", ".join(unpushed))
    for wt in e.get("worktrees") or []:
        if wt.get("dirty") and _dirty_text(wt["dirty"]):
            add(h, "worktree-dirty", f"{wt.get('path')}: {_dirty_text(wt['dirty'])}")
        if wt.get("prunable"):
            add(h, "worktree-prunable", str(wt.get("path")))


def _group_issues(group: dict, hosts: list[str]) -> list[dict]:
    issues = []

    def add(host, kind, detail):
        issues.append({"host": host, "kind": kind, "detail": detail})

    key, entries = group["key"], group["entries"]
    present = [h for h in hosts if h in entries]
    flat = [(h, e) for h in present for e in entries[h]]
    if key:
        for h in hosts:
            if h not in entries:
                add(h, "missing", f"no checkout of {key}")
    if len(present) > 1:
        folders = {e["folder"].rsplit("/", 1)[-1] for _, e in flat}
        if len(folders) > 1:
            add(None, "folder-differs", ", ".join(f"{h}: {e['folder']}" for h, e in flat))
        defaults = {e.get("default_branch") for _, e in flat if e.get("default_branch")}
        if len(defaults) > 1:
            add(None, "default-differs", ", ".join(f"{h}: {e.get('default_branch')}" for h, e in flat))
        origins = [(h, e) for h, e in flat if e.get("origin_sha")]
        if len({e["origin_sha"] for _, e in origins}) > 1:
            dated = [(t, h, e) for h, e in origins if (t := _ts(e.get("origin_date"))) is not None]
            if len(dated) == len(origins):
                _, nh, ne = max(dated, key=lambda x: x[0])
                for t, h, e in dated:
                    if e["origin_sha"] != ne["origin_sha"]:
                        add(h, "stale-origin", f"{e.get('origin_ref')} {sha7(e['origin_sha'])} ({e.get('origin_date')}) "
                                               f"is older than {nh}'s {sha7(ne['origin_sha'])}: git fetch")
            else:
                add(None, "origin-differs", ", ".join(f"{h}: {sha7(e['origin_sha'])}" for h, e in origins))
        names = {h: {b["name"] for e in entries[h] for b in e.get("branches") or []} for h in present}
        for h in present:
            only = sorted(b for b in names[h] if any(b not in names[o] for o in present if o != h))
            if only:
                add(h, "branch-only", ", ".join(only[:20]) + (f" (+{len(only) - 20})" if len(only) > 20 else ""))
    for h in present:
        if len(entries[h]) > 1:
            add(h, "duplicate-clones", f"duplicate clones on {h}: " + ", ".join(e["folder"] for e in entries[h]))
    for h, e in flat:
        _entry_issues(h, e, add)
    return issues


def compare(snapshots: list[dict], aliases: dict[str, str] | None = None) -> dict:
    aliases = aliases or {}
    by_host: dict[str, dict] = {}
    for s in snapshots:
        by_host[s["host"]] = s
    hosts = list(by_host)
    groups: dict[str, dict] = {}
    for h, s in by_host.items():
        for e in s["repos"]:
            key = apply_alias(e.get("key"), aliases)
            label = key or f"(no remote) {h}:{e['folder']}"
            listed = groups.setdefault(label, {"key": key, "label": label, "entries": {}})["entries"].setdefault(h, [])
            where = os.path.normcase(e.get("real_path") or e.get("path") or "")
            same = next((x for x in listed if where and os.path.normcase(x.get("real_path") or x.get("path") or "")
                         == where), None)
            if same is not None:  # one checkout listed under two folder names: an alias, not a second clone
                same["aliases"] = list(same.get("aliases") or []) + [e["folder"]] + list(e.get("aliases") or [])
            else:
                listed.append(dict(e))
    repos = []
    for label in sorted(groups):
        g = groups[label]
        g["issues"] = _group_issues(g, hosts)
        repos.append(g)
    return {
        "schema": "repo-inventory-compare", "schema_version": SCHEMA_VERSION,
        "hosts": [{"host": h, "generated_at": s.get("generated_at"), "root": s.get("root"),
                   "fetched": s.get("fetched"), "repos": len(s["repos"])} for h, s in by_host.items()],
        "repos": repos,
        "drift": any(r["issues"] for r in repos),
        "drift_repos": sum(1 for r in repos if r["issues"]),
    }


def _safe(cmd: str) -> dict:
    return {"kind": "safe", "command": cmd}


def _human(reason: str, suggest: str | None = None) -> dict:
    step = {"kind": "human", "reason": reason}
    if suggest:
        step["suggest"] = suggest
    return step


def _note(text: str) -> dict:
    return {"kind": "note", "reason": text}


def _quoted(*values) -> bool:
    """True when every value can go inside double quotes on a printed command line: no shell expansion, no
    control characters, and no leading '-' that git would read as an option."""
    return all(isinstance(v, str) and v and not v.startswith("-") and not UNSAFE_ARG.search(v) for v in values)


def _host_steps(e: dict, stale: bool) -> list[dict]:
    if e.get("error"):
        return [_human(f"error: {e['error']}")]
    path = e.get("path")
    if not _quoted(path):
        return [_human(f"unsafe characters in path {path!r}: run the steps by hand")]
    git = f'git -C "{path}"'
    steps = []
    remote = e.get("remote")
    if remote:
        steps.append(_safe(f"{git} fetch --prune"))
    else:
        steps.append(_note("no remote: nothing to sync"))
    head = e.get("head") or {}
    d = e.get("dirty") or {}
    default = e.get("default_branch")
    unmerged = e.get("unmerged_paths") or []
    blocking = _dirty_text(d, untracked=False)
    if blocking:  # whatever the branch state: dirty work needs a decision before any sync
        steps.append(_human(f"dirty: {blocking}: commit or stash by hand"))
    if e.get("in_progress") or d.get("unmerged") or unmerged:
        ops = ", ".join(e.get("in_progress") or [])
        if unmerged or d.get("unmerged"):
            reason = f"conflict: {len(unmerged) or d.get('unmerged')} unmerged path(s) ({', '.join(unmerged[:5])})" \
                + (f"; {ops} in progress" if ops else "")
        else:
            reason = f"{ops} in progress"
        steps.append(_human(reason + ": finish or abort by hand"))
    elif head.get("detached"):
        steps.append(_human(f"detached HEAD at {sha7(head.get('sha'))}: check out a branch by hand"))
    elif head.get("branch") != default:
        steps.append(_human(f"on branch {head.get('branch')}, not {default}: no pull planned"))
    else:
        vd = head.get("vs_default") or {}
        a, b = vd.get("ahead") or 0, vd.get("behind") or 0
        ref = e.get("origin_ref") or f"origin/{default}"
        if blocking:
            if b or stale:
                steps.append(_note(f"{b} behind {ref}: pull --ff-only once the tree is clean" if b
                                   else f"{ref} may be stale: pull --ff-only once the tree is clean"))
        elif a and b:
            steps.append(_human(f"diverged: {a} ahead and {b} behind {ref}: rebase or merge by hand"))
        elif a:
            steps.append(_human(f"unpushed: {a} commit(s) on {default} not on {ref}: push or reset by hand"))
        elif (b or stale) and remote:
            steps.append(_safe(f"{git} pull --ff-only"))
    current = head.get("branch")
    other = [f"{x['name']} ({x['unpushed']})" for x in e.get("branches") or []
             if x.get("unpushed") and not (x["name"] == current == default)]
    if other:
        steps.append(_human(f"unpushed branches: {', '.join(other[:10])}: push or delete by hand"))
    gone = [x["name"] + (" (merged)" if x.get("merged") else "") for x in e.get("branches") or [] if x.get("gone")]
    if gone:
        steps.append(_human(f"upstream gone: {', '.join(gone[:10])}: delete or re-push by hand"))
    for wt in e.get("worktrees") or []:
        if wt.get("prunable"):
            steps.append(_human(f"worktree prunable: {wt.get('path')}: git worktree prune by hand"))
        elif wt.get("dirty") and _dirty_text(wt["dirty"]):
            steps.append(_human(f"worktree dirty: {wt.get('path')} ({_dirty_text(wt['dirty'])})"))
    if e.get("stash_count"):
        steps.append(_note(f"{e['stash_count']} stash(es)"))
    if d.get("untracked") and not blocking:
        steps.append(_note(f"{d['untracked']} untracked file(s); git refuses a pull that would overwrite them"))
    return steps


def plan(result: dict) -> dict:
    hosts = [h["host"] for h in result["hosts"]]
    roots = {h["host"]: h.get("root") for h in result["hosts"]}
    out = {"schema": "repo-inventory-plan", "schema_version": SCHEMA_VERSION, "hosts": {h: [] for h in hosts},
           "snapshots": result["hosts"]}
    for repo in result["repos"]:
        stale = {i["host"] for i in repo["issues"] if i["kind"] == "stale-origin"}
        entries = repo["entries"]
        for h in hosts:
            if h in entries:
                for e in entries[h]:
                    out["hosts"][h].append({"key": repo["label"], "folder": e["folder"], "path": e.get("path"),
                                            "steps": _host_steps(e, h in stale)})
            elif repo["key"]:
                others = [e for es in entries.values() for e in es if (e.get("remote") or {}).get("url")]
                others.sort(key=lambda e: (e["remote"].get("host") == "local",))
                url = strip_credentials(others[0]["remote"]["url"]) if others else None
                if url and parse_remote(url)[0] == "local":
                    url = url.replace("\\", "/")  # git accepts C:/... and //server/... on Windows
                names = collections.Counter(e["folder"].rsplit("/", 1)[-1] for es in entries.values() for e in es)
                folder = names.most_common(1)[0][0] if names else None
                root = roots.get(h)
                cmd = f'git -C "{root}" clone -- "{url}" "{folder}"' if _quoted(root, url, folder) else None
                out["hosts"][h].append({"key": repo["label"], "folder": None, "path": None,
                                        "steps": [_human(f"missing clone of {repo['key']}", suggest=cmd)]})
    return out


# ---------------------------------------------------------------- rendering

def _head_text(e: dict) -> str:
    if e.get("error"):
        return "ERROR"
    head = e.get("head") or {}
    if head.get("detached"):
        return f"detached@{sha7(head.get('sha'))}"
    vd = head.get("vs_default") or {}
    a, b = vd.get("ahead") or 0, vd.get("behind") or 0
    rel = "=" if not (a or b) else f"+{a}/-{b}"
    if head.get("branch") == e.get("default_branch"):
        return f"{head.get('branch')} {rel}"
    return f"{head.get('branch')} ({rel} vs {e.get('default_branch')})"


def _table(rows: list[list[str]], indent="  ") -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return [indent + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]


def render_compare(result: dict, show_all: bool = False) -> str:
    lines = ["hosts: " + "; ".join(f"{h['host']} ({h.get('generated_at')}, root {h.get('root')}, "
                                   f"{'fetched' if h.get('fetched') else 'not fetched'}, {h['repos']} repos)"
                                   for h in result["hosts"])]
    hidden = 0
    for repo in result["repos"]:
        if not repo["issues"] and not show_all:
            hidden += 1
            continue
        lines.append("")
        lines.append(f"{repo['label']}{'  [drift]' if repo['issues'] else ''}")
        rows = [["host", "folder", "head", "origin", "dirty s/u/?/!", "stash", "wt", "branches"]]
        for h, es in repo["entries"].items():
            for e in es:
                d = e.get("dirty") or {}
                folder = e["folder"] + (f" (= {', '.join(e['aliases'])})" if e.get("aliases") else "")
                rows.append([h, folder, _head_text(e),
                             f"{sha7(e.get('origin_sha'))} {(e.get('origin_date') or '')[:10]}".strip(),
                             "/".join(str(d.get(k, 0)) for k in ("staged", "unstaged", "untracked", "unmerged")),
                             str(e.get("stash_count") or 0), str(len(e.get("worktrees") or [])),
                             str(len(e.get("branches") or []))])
        lines += _table(rows)
        for i in repo["issues"]:
            lines.append(f"  ! {i['host'] + ' ' if i['host'] else ''}{i['kind']}: {i['detail']}")
    lines.append("")
    lines.append(f"{len(result['repos'])} repos, {result['drift_repos']} with drift"
                 + (f", {hidden} in sync not shown (--all shows them)" if hidden else ""))
    return "\n".join(lines) + "\n"


def render_plan(p: dict) -> str:
    lines = ["Plan only: nothing was run. Review each step, then run it on that host.",
             "Untracked files do not block a pull: git refuses one that would overwrite them."]
    meta = {s["host"]: s for s in p.get("snapshots", [])}
    labels = {"safe": "SAFE", "human": "NEEDS HUMAN", "note": "note"}
    for h, repos in p["hosts"].items():
        m = meta.get(h, {})
        lines += ["", f"== {h} (snapshot {m.get('generated_at')}, root {m.get('root')}) =="]
        for r in repos:
            lines.append(f"{r['key']}  {r['folder'] or '(missing)'}")
            for s in r["steps"]:
                lines.append(f"  {labels[s['kind']]:<11}  {s.get('command') or s.get('reason')}")
                if s.get("suggest"):
                    lines.append(f"  {'':<11}  suggested: {s['suggest']}")
    return "\n".join(lines) + "\n"


def _summary_facts(e: dict) -> list[str]:
    if e.get("error"):
        return [f"ERROR {e['error'][:120]}"]
    facts = []
    head = e.get("head") or {}
    default = e.get("default_branch")
    vd = head.get("vs_default") or {}
    if head.get("detached"):
        facts.append("detached")
    elif head.get("branch") != default:
        facts.append("off-default")
    if vd.get("ahead") or vd.get("behind"):
        facts.append(f"+{vd.get('ahead') or 0}/-{vd.get('behind') or 0}")
    d = e.get("dirty") or {}
    if any(d.get(k) for k in ("staged", "unstaged", "untracked", "unmerged")):
        facts.append("dirty=" + "/".join(str(d.get(k, 0)) for k in ("staged", "unstaged", "untracked", "unmerged")))
    if e.get("in_progress"):
        facts.append("op=" + ",".join(e["in_progress"]))
    if e.get("stash_count"):
        facts.append(f"stash={e['stash_count']}")
    wts = e.get("worktrees") or []
    if wts:
        facts.append(f"wt={len(wts)}")
    unp = [f"{b['name']}:{b['unpushed']}" for b in e.get("branches") or [] if b.get("unpushed")]
    if unp:
        facts.append("unpushed=" + ",".join(unp[:8]) + ("..." if len(unp) > 8 else ""))
    gone = [b["name"] for b in e.get("branches") or [] if b.get("gone")]
    if gone:
        facts.append("gone=" + ",".join(gone[:8]) + ("..." if len(gone) > 8 else ""))
    return facts


def render_summary(s: dict, limit: int = SUMMARY_LIMIT) -> str:
    """A compact digest for an agent-mail body: header, one line per repo with something to report, then the
    keys of the quiet repos. Always under limit bytes (UTF-8)."""
    budget = limit - 512
    head = (f"repo-inventory v{VERSION} host={s['host']} at={s.get('generated_at')} root={s.get('root')} "
            f"fetched={'yes' if s.get('fetched') else 'no'} repos={len(s['repos'])}")
    lines, quiet, used, omitted = [head], [], len(head.encode("utf-8")) + 1, 0
    for e in s["repos"]:
        facts = _summary_facts(e)
        key = e.get("key") or "(no remote)"
        if not facts:
            quiet.append(key)
            continue
        line = (f"{key} [{e['folder']}] {(e.get('head') or {}).get('branch') or '-'}@"
                f"{sha7((e.get('head') or {}).get('sha'))} o={sha7(e.get('origin_sha'))} " + " ".join(facts))
        n = len(line.encode("utf-8")) + 1
        if omitted or used + n > budget:
            omitted += 1
            continue
        lines.append(line)
        used += n
    if omitted:
        lines.append(f"... +{omitted} more repos with drift not shown (full snapshot: --share or --out)")
        used += len(lines[-1].encode("utf-8")) + 1
    if quiet:
        text, shown = "in sync: ", 0
        for k in quiet:
            if used + len((text + k + " ").encode("utf-8")) + 40 > budget:
                break
            text += k + " "
            shown += 1
        lines.append(text.rstrip() + (f" ... +{len(quiet) - shown} more" if shown < len(quiet) else ""))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- git (snapshot)

def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    # No optional index writes, no prompts: a snapshot stays read-only and never waits for input.
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    return env


def git_out(cwd, args: list[str], timeout: float = 30) -> str:
    argv = ["git", "-C", str(cwd), *GIT_CFG, *args]
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, env=_git_env())
    except FileNotFoundError:
        raise UsageError("git is not on PATH")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out after {timeout}s")
    if r.returncode:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitError(scrub(f"git {args[0]} exited {r.returncode}: {err[0] if err else ''}")[:300])
    return r.stdout.decode("utf-8", "replace")


def git_try(cwd, args: list[str], timeout: float = 30) -> str | None:
    try:
        return git_out(cwd, args, timeout)
    except GitError:
        return None


def _same(a, b) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def _worktrees(repo: Path, timeout: float) -> list[dict]:
    out = git_try(repo, ["worktree", "list", "--porcelain"], timeout) or ""
    blocks, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                blocks.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v
    result = []
    for b in blocks[1:]:  # the first block is the main worktree
        path = b.get("worktree")
        wt = {"path": path, "head": b.get("HEAD"), "branch": (b.get("branch") or "").removeprefix("refs/heads/"),
              "detached": "detached" in b, "locked": "locked" in b, "prunable": "prunable" in b,
              "missing": not (path and Path(path).is_dir()), "dirty": None, "error": None}
        wt["branch"] = wt["branch"] or None
        if not wt["missing"]:
            try:
                st = parse_status(git_out(path, ["status", "--porcelain=v2", "-z"], timeout), 0)
                wt["dirty"] = st["dirty"]
            except GitError as ex:
                wt["error"] = str(ex)
        result.append(wt)
    return result


def _rel(path: Path, root: Path) -> str:
    return (path.relative_to(root).as_posix() if path != root else "") or path.name


def collect(path: Path, root: Path, max_paths: int = 20, fetch: bool = False, timeout: float = 30,
            fetch_timeout: float = 120) -> dict | None:
    """One repo's facts. None for a linked worktree (it is recorded under its main repository)."""
    e = {"folder": _rel(path, root), "path": path.as_posix(), "real_path": None, "aliases": [], "remote": None, "key": None, "default_branch": None,
         "origin_ref": None, "origin_sha": None, "origin_date": None, "head": None, "branches": [],
         "worktrees": [], "dirty": {"staged": 0, "unstaged": 0, "untracked": 0, "unmerged": 0},
         "dirty_paths": [], "dirty_paths_truncated": False, "stash_count": 0, "in_progress": None,
         "unmerged_paths": [], "last_commit": None, "fetch_error": None, "error": None}
    try:
        rp = git_out(path, ["rev-parse", "--absolute-git-dir", "--git-common-dir"], timeout).splitlines()
        gitdir = Path(rp[0])
        common = Path(rp[1]) if Path(rp[1]).is_absolute() else path / rp[1]
        if not _same(gitdir, common):
            return None
        remotes = {}
        for line in (git_try(path, ["config", "--get-regexp", r"^remote\..*\.url$"], timeout) or "").splitlines():
            name, _, url = line.partition(" ")
            remotes[name[len("remote."):-len(".url")]] = url
        rname = "origin" if "origin" in remotes else (sorted(remotes)[0] if remotes else None)
        if rname:
            url = strip_credentials(remotes[rname])
            host, key = parse_remote(remotes[rname])
            e["remote"] = {"name": rname, "url": url, "host": host, "key": key}
            e["key"] = key
            if fetch:
                try:
                    git_out(path, ["fetch", "--prune", "--quiet", "--no-recurse-submodules", rname], fetch_timeout)
                except GitError as ex:
                    e["fetch_error"] = str(ex)
        fmt = "%(refname)%00%(objectname)%00%(upstream:short)%00%(upstream:track,nobracket)%00%(symref)%00" \
              "%(committerdate:iso-strict)"
        patterns = ["refs/heads", "refs/stash"] + ([f"refs/remotes/{rname}"] if rname else [])
        refs = {}
        for line in git_out(path, ["for-each-ref", f"--format={fmt}", *patterns], timeout).splitlines():
            f = line.split("\0")
            if len(f) >= 6:
                refs[f[0]] = {"sha": f[1], "upstream": f[2] or None, "track": f[3], "symref": f[4], "date": f[5]}
        rprefix = f"refs/remotes/{rname}/"
        heads = {r[len("refs/heads/"):]: v for r, v in refs.items() if r.startswith("refs/heads/")}
        st = parse_status(git_out(path, ["status", "--porcelain=v2", "--branch", "-z"], timeout), max_paths)
        default = None
        sym = (refs.get(rprefix + "HEAD") or {}).get("symref") if rname else None
        if sym and sym.startswith(rprefix):
            default = sym[len(rprefix):]
        for cand in ("main", "master"):
            if default is None and (rname and rprefix + cand in refs or cand in heads):
                default = cand
        default = default or st["branch"]
        e["default_branch"] = default
        origin = refs.get(rprefix + default) if rname and default else None
        if origin:
            e["origin_ref"], e["origin_sha"], e["origin_date"] = f"{rname}/{default}", origin["sha"], origin["date"]
        base = (rprefix + default) if origin else (f"refs/heads/{default}" if default in heads else None)
        merged = set()
        if base:
            merged = set((git_try(path, ["for-each-ref", "--merged", base, "--format=%(refname)", "refs/heads"],
                                  timeout) or "").split())
        for name in sorted(heads):
            v = heads[name]
            m = re.search(r"ahead (\d+)", v["track"])
            b = re.search(r"behind (\d+)", v["track"])
            gone = v["track"] == "gone"
            br = {"name": name, "sha": v["sha"], "upstream": v["upstream"], "ahead": int(m.group(1)) if m else 0,
                  "behind": int(b.group(1)) if b else 0, "gone": gone, "merged": f"refs/heads/{name}" in merged,
                  "unpushed": None}
            if not remotes:
                pass
            elif v["upstream"] and not gone:
                br["unpushed"] = br["ahead"]
            elif br["merged"] and origin:
                br["unpushed"] = 0
            else:
                n = git_try(path, ["rev-list", "--count", v["sha"], "--not", "--remotes"], timeout)
                br["unpushed"] = int(n) if n and n.strip().isdigit() else None
            if not v["upstream"]:
                br["ahead"] = br["behind"] = None
            e["branches"].append(br)
        vs = None
        if origin and st["sha"]:
            lr = git_try(path, ["rev-list", "--left-right", "--count", f"HEAD...{rprefix + default}"], timeout)
            if lr and len(lr.split()) == 2:
                vs = {"ahead": int(lr.split()[0]), "behind": int(lr.split()[1])}
        e["head"] = {"branch": st["branch"], "sha": st["sha"], "detached": st["detached"], "upstream": st["upstream"],
                     "ahead": st["ahead"], "behind": st["behind"], "vs_default": vs}
        e["dirty"], e["dirty_paths"], e["dirty_paths_truncated"] = st["dirty"], st["paths"], st["paths_truncated"]
        e["unmerged_paths"] = st["unmerged_paths"][:max_paths]
        if "refs/stash" in refs:
            n = git_try(path, ["rev-list", "--walk-reflogs", "--count", "refs/stash"], timeout)
            e["stash_count"] = int(n) if n and n.strip().isdigit() else 1
        ops = [label for name, label in OPERATIONS if (gitdir / name).exists()]
        if (gitdir / "rebase-apply").is_dir():
            ops.append("am" if (gitdir / "rebase-apply" / "applying").exists() else "rebase")
        e["in_progress"] = ops or None
        if st["sha"]:
            e["last_commit"] = (git_try(path, ["log", "-1", "--format=%cI", "HEAD"], timeout) or "").strip() or None
        e["worktrees"] = _worktrees(path, timeout)
    except GitError as ex:
        e["error"] = str(ex)
    return e


def _is_reparse(path: Path) -> bool:
    """True for a Windows junction or symlink (Python < 3.12 has no DirEntry.is_junction)."""
    try:
        return bool(os.lstat(path).st_file_attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except (OSError, AttributeError):
        return os.path.islink(path)


def discover(root: Path, depth: int) -> list[Path]:
    found = []

    def walk(d: Path, level: int):
        if (d / ".git").exists():
            found.append(d)
            return
        if level >= depth:
            return
        try:
            entries = sorted(os.scandir(d), key=lambda x: x.name.lower())
        except OSError:
            return
        for en in entries:
            name = en.name
            child = Path(en.path)
            try:
                if not en.is_dir():
                    continue
                # A junction or symlink is never searched (no loops), but one that is a checkout is kept, so
                # snapshot can record it as an alias of the checkout it points at.
                link = en.is_symlink() or _is_reparse(child)
            except OSError:
                continue
            if (child / ".git").exists():  # a checkout is recorded whatever its name (.dotfiles, build, ...)
                found.append(child)
            elif not link and not (name.startswith(".") or name.lower() in SKIP_DIRS
                                   or name.lower().endswith(".worktrees")):
                walk(child, level + 1)

    walk(root, 0)
    return found


def snapshot(root, host: str, depth: int = 2, fetch: bool = False, max_paths: int = 20, jobs: int = 8,
             timeout: float = 30, fetch_timeout: float = 120) -> dict:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise UsageError(f"projects root {root} is not a folder")
    # Folders that resolve to one checkout (a junction or symlink to another folder) are scanned once; the
    # extra folder names are recorded as that checkout's aliases. A plain folder is preferred as the real one.
    real: dict[str, list[Path]] = {}
    for p in discover(root, depth):
        real.setdefault(os.path.normcase(os.path.realpath(p)), []).append(p)
    groups = [sorted(ps, key=lambda p: (_is_reparse(p), p.name.lower())) for ps in real.values()]
    with ThreadPoolExecutor(max(1, jobs)) as ex:
        entries = list(ex.map(lambda ps: collect(ps[0], root, max_paths, fetch, timeout, fetch_timeout), groups))
    for ps, e in zip(groups, entries):
        if e is not None:
            e["real_path"] = Path(os.path.realpath(ps[0])).as_posix()
            e["aliases"] = [_rel(p, root) for p in ps[1:]]
    return {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "tool_version": VERSION, "host": host,
            "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "root": root.as_posix(), "depth": depth, "fetched": fetch, "max_paths": max_paths,
            "repos": sorted((e for e in entries if e is not None), key=lambda e: e["folder"].lower())}


# ---------------------------------------------------------------- share, host, ssh, config

def norm(path) -> Path:
    """Accept Windows, POSIX and Git Bash (/c/...) spellings."""
    s = str(path)
    m = re.match(r"^/([a-zA-Z])/(.*)$", s)
    if m and os.name == "nt":
        s = f"{m.group(1).upper()}:/{m.group(2)}"
    return Path(os.path.expanduser(s))


def load_config() -> dict:
    p = norm(os.environ.get("REPO_INVENTORY_CONFIG") or CONFIG)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        raise UsageError(f"config {p}: {ex}")
    return data if isinstance(data, dict) else {}


def is_share(root: Path) -> bool:
    try:
        return root.is_dir() and any(PAIR.match(p.name) for p in root.iterdir() if p.is_dir())
    except OSError:
        return False


def find_share(arg: str | None, cfg: dict) -> Path:
    if arg:
        p = norm(arg)
        if not is_share(p):
            raise UsageError(f"{p} is not a share (a folder with <from>-to-<to> subfolders)")
        return p.resolve()
    sources = [os.environ.get("CROSS_PC_SHARE")]
    mail = norm(MAIL_CONFIG)
    if mail.is_file():
        try:
            sources.append(json.loads(mail.read_text(encoding="utf-8")).get("root"))
        except (OSError, ValueError, AttributeError):
            pass
    sources += [cfg.get("share"), *CANDIDATES]
    tried = []
    for s in sources:
        if s:
            p = norm(s)
            tried.append(str(p))
            if is_share(p):
                return p.resolve()
    raise UsageError("no share found. Tried: " + "; ".join(tried) + ". Pass --share-root or set CROSS_PC_SHARE.")


def share_hosts(root: Path) -> dict[str, str]:
    found = {}
    agents = root / "AGENTS.md"
    if agents.is_file():
        for line in agents.read_text(encoding="utf-8", errors="replace").splitlines():
            m = HOST_ROW.match(line)
            if m:
                found[m.group(1)] = m.group(2)
    for p in root.iterdir():
        m = PAIR.match(p.name) if p.is_dir() else None
        if m:
            for h in m.groups():
                found.setdefault(h, "")
    return found


def resolve_host(arg: str | None, share: Path | None = None) -> str:
    raw = arg or os.environ.get("CROSS_PC_HOST") or os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") \
        or socket.gethostname()
    want = raw.strip().lower()
    if share is not None and not arg:
        known = share_hosts(share)
        if want not in known:
            for hid, machine in known.items():  # the Machine column, e.g. "DESKTOP1 (desktop, ...)"
                if machine.lower().split(" (")[0].strip() == want:
                    want = hid
                    break
    if not HOST_ID.fullmatch(want):
        raise UsageError(f"host id {raw!r} must be lowercase letters, digits and hyphens: pass --host")
    return want


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def ssh_run(argv: list[str], timeout: float):
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise UsageError("ssh client not found on PATH")
    except subprocess.TimeoutExpired:
        raise UsageError(f"ssh timed out after {timeout}s")


def fetch_ssh(peer: str, command: str, timeout: float = 600) -> dict:
    """Run the snapshot command on the peer and read its JSON from stdout."""
    if not isinstance(peer, str) or not PEER.fullmatch(peer):
        raise UsageError(f"peer must be an ssh alias or user@host, got {peer!r}")
    r = ssh_run(["ssh", *SSH_OPTS, peer, command], timeout)
    if r.returncode:
        err = [l for l in r.stderr.decode("utf-8", "replace").splitlines() if l.strip()]
        raise UsageError(f"ssh {peer} exited {r.returncode}: {scrub(err[-1]) if err else 'no output'}")
    text = r.stdout.decode("utf-8", "replace")
    start = text.find("{")
    try:
        data = json.loads(text[start:]) if start >= 0 else None
    except ValueError as ex:
        raise UsageError(f"ssh {peer}: output is not a snapshot ({ex})")
    return validate_snapshot(data, f"ssh {peer}")


def load_inputs(inputs: list[str]) -> list[dict]:
    files = []
    for raw in inputs:
        p = norm(raw)
        if p.is_dir():
            files += sorted(p.glob("repos-*.json"))
        elif p.is_file():
            files.append(p)
        else:
            raise UsageError(f"{p}: no such file or folder")
    out = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as ex:
            raise UsageError(f"{f}: {ex}")
        out.append(validate_snapshot(data, str(f)))
    return out


# ---------------------------------------------------------------- CLI

def _scan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--root", help="projects folder to scan (default: REPO_INVENTORY_ROOT, then config root)")
    p.add_argument("--depth", type=int, help="folder levels below the root to search (default 2)")
    p.add_argument("--host", help="this host's id (default: CROSS_PC_HOST, then COMPUTERNAME lowercased)")
    p.add_argument("--max-paths", type=int, default=20, help="dirty file paths kept per repo (default 20)")
    p.add_argument("--jobs", type=int, default=8, help="repos scanned in parallel (default 8)")
    p.add_argument("--timeout", type=float, default=30, help="seconds per git call (default 30)")
    p.add_argument("--share-root", help="the cross-PC share (default: CROSS_PC_SHARE, cross-pc-mail config, ...)")


def _local_snapshot(args, cfg: dict, fetch: bool = False, share: Path | None = None) -> dict:
    root = args.root or os.environ.get("REPO_INVENTORY_ROOT") or cfg.get("root")
    if not root:
        raise UsageError("no projects root: pass --root, set REPO_INVENTORY_ROOT, or set root in the config")
    depth = args.depth if args.depth is not None else int(cfg.get("depth", 2))
    host = resolve_host(args.host, share)
    return snapshot(norm(root), host, depth=depth, fetch=fetch, max_paths=max(0, args.max_paths),
                    jobs=args.jobs, timeout=args.timeout)


def _gather(args, cfg: dict) -> list[dict]:
    snaps = []
    if not args.inputs and not args.ssh and not args.local or args.share:
        try:
            share = find_share(args.share_root, cfg)
        except UsageError:
            if args.ssh or args.local:
                share = None
            else:
                raise
        if share is not None:
            snaps += load_inputs([str(share / PROFILE_DIR)]) if (share / PROFILE_DIR).is_dir() else []
    snaps += load_inputs(args.inputs)
    for peer in args.ssh or []:
        cmd = args.ssh_command or (cfg.get("ssh_commands") or {}).get(peer) or cfg.get("ssh_command") \
            or DEFAULT_SSH_COMMAND
        snaps.append(fetch_ssh(peer, cmd))
    if args.local:
        snaps.append(validate_snapshot(_local_snapshot(args, cfg), "local snapshot"))
    seen = {}
    for s in snaps:
        if s["host"] in seen:
            print(f"repo_inventory: host {s['host']} given twice; using the later snapshot", file=sys.stderr)
        seen[s["host"]] = s
    if not seen:
        raise UsageError("no snapshots to read")
    return list(seen.values())


def _aliases(args, cfg: dict) -> dict[str, str]:
    raw = dict(cfg.get("aliases") or {})
    if args.aliases:
        try:
            extra = json.loads(norm(args.aliases).read_text(encoding="utf-8"))
        except (OSError, ValueError) as ex:
            raise UsageError(f"aliases {args.aliases}: {ex}")
        if not isinstance(extra, dict):
            raise UsageError(f"aliases {args.aliases}: expected a JSON object {{old_name: new_name}}")
        raw.update(extra)
    return normalize_aliases(raw)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="repo_inventory", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("snapshot", help="inventory this PC's checkouts")
    _scan_args(sp)
    sp.add_argument("--fetch", action="store_true", help="git fetch --prune each repo first (updates remote-tracking refs)")
    sp.add_argument("--out", help="write the JSON here instead of stdout")
    sp.add_argument("--share", action="store_true", help="write <share>/private/host-profile/repos-<host>.json")
    sp.add_argument("--summary", action="store_true", help="print a digest under 16 KB instead of the JSON")
    for name in ("compare", "plan"):
        p = sub.add_parser(name, help="compare snapshots" if name == "compare" else "print sync steps, run none")
        p.add_argument("inputs", nargs="*", help="snapshot files or folders of repos-<host>.json")
        _scan_args(p)
        p.add_argument("--share", action="store_true", help="also read the share's host-profile folder")
        p.add_argument("--ssh", action="append", metavar="PEER", help="read PEER's live snapshot over ssh")
        p.add_argument("--ssh-command", help=f"remote command (default: config, then {DEFAULT_SSH_COMMAND!r})")
        p.add_argument("--local", action="store_true", help="add a fresh snapshot of this PC (uses --root)")
        p.add_argument("--aliases", help="JSON file {old_name: new_name} for renamed repos")
        p.add_argument("--json", action="store_true")
        if name == "compare":
            p.add_argument("--all", action="store_true", help="also show repos without drift")
    args = ap.parse_args(argv)
    try:
        cfg = load_config()
        if args.cmd == "snapshot":
            share = find_share(args.share_root, cfg) if args.share else None
            snap = _local_snapshot(args, cfg, fetch=args.fetch, share=share)
            text = json.dumps(snap, indent=2) + "\n"
            if args.out:
                write_atomic(norm(args.out), text)
            if share is not None:
                target = share / PROFILE_DIR / f"repos-{snap['host']}.json"
                write_atomic(target, text)
                print(f"wrote {target}", file=sys.stderr)
            if args.summary:
                sys.stdout.write(render_summary(snap))
            elif not args.out and share is None:
                sys.stdout.write(text)
            return 0
        result = compare(_gather(args, cfg), _aliases(args, cfg))
        if args.cmd == "compare":
            if args.json:
                sys.stdout.write(json.dumps(result, indent=2) + "\n")
            else:
                sys.stdout.write(render_compare(result, args.all))
            return 1 if result["drift"] else 0
        p = plan(result)
        sys.stdout.write(json.dumps(p, indent=2) + "\n" if args.json else render_plan(p))
        return 0
    except UsageError as ex:
        print(f"repo_inventory: {ex}", file=sys.stderr)
        return 2
    except (KeyError, TypeError, AttributeError, ValueError) as ex:
        print(f"repo_inventory: malformed snapshot data ({type(ex).__name__}: {ex})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
