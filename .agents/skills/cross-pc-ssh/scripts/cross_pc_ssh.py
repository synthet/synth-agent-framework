#!/usr/bin/env python3
"""Work on the same project checkout on a paired PC over SSH.

The neighbour PC keeps its checkouts somewhere else (another drive, another root, another folder name), so
this helper finds the checkout by git identity instead of by path:

  1. root commit(s) of the current repo, identical in every clone whatever the folder or remote is called;
  2. the origin URL, normalised (and resolved through GitHub's rename redirect when gh is available), only to
     break a tie between candidates that share history (forks).

The match must be unique. Linked worktrees, junction/symlink duplicates and shallow clones never match.
Every verb then runs a fixed remote script inside that checkout; there is no free-form remote command verb.

Commands:
  hosts [--probe]                       configured peers (ssh alias + project roots); --probe runs hostname
  locate HOST [--refresh]               remote path of this repo's checkout
  status HOST                           branch, HEAD, ahead/behind, short status, worktrees, stashes
  log HOST [-n N]                       recent commits
  diff HOST [PATH]                      --stat of unstaged + staged changes, or the diff of one relative PATH
  ls HOST [PATH]                        entries of a directory inside the checkout
  cat HOST PATH [--max-bytes N]         a text file inside the checkout (secret-looking files refused)
  agent HOST (--prompt TEXT | --prompt-file F) [--harness claude|codex] [--write] [--model M]
        [--timeout SECONDS]             run a headless agent in the remote checkout (read-only by default)

Config (never in a repo): --config, env CROSS_PC_SSH_CONFIG, else ~/.config/cross-pc-ssh.json:
  {"hosts": {"beta": {"ssh": "beta", "roots": ["C:\\Repos"]}}, "self": "alpha"}
"ssh" is an alias from ~/.ssh/config (or user@host). Remote hosts must be Windows with OpenSSH + PowerShell.

This is a guard rail for agents, not a security boundary: the SSH key itself grants a full remote shell.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

VERSION = "1"
MARK = "@@XPC@@"
CONFIG = Path("~/.config/cross-pc-ssh.json")
CACHE = Path("~/.cache/cross-pc-ssh/locations.json")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
MAX_PROMPT = 20000
CLAUDE_READ_TOOLS = ["Read", "Grep", "Glob", "Bash(git status:*)", "Bash(git log:*)", "Bash(git diff:*)",
                     "Bash(git show:*)", "Bash(git branch:*)", "Bash(git rev-parse:*)", "Bash(git ls-files:*)"]
# Names cat never returns, even inside the checkout. Matched case-insensitively against the file name.
SECRET_NAMES = [re.compile(p, re.I) for p in (
    r"^\.env(\..+)?$", r"^secrets?\.(json|ya?ml|toml)$", r"^credentials?(\..+)?$", r".*\.(pem|key|pfx|p12|kdbx)$",
    r"^id_(rsa|dsa|ecdsa|ed25519)(_sk)?(\.pub)?$", r"^\.(npmrc|pypirc|netrc|git-credentials)$",
    r"^settings\.local\.json$", r"^mcp\.json$")]
SAFE_NAMES = [re.compile(p, re.I) for p in (r"^\.env\.(example|sample|template)$",)]
SECRET_DIRS = {".git", ".ssh", ".gnupg"}


class XpcError(Exception):
    """A refusal or failure the caller should see as one line."""


# ---------------------------------------------------------------- config and local identity

def load_config(arg: str | None) -> dict:
    path = Path(arg or os.environ.get("CROSS_PC_SSH_CONFIG") or CONFIG).expanduser()
    if not path.is_file():
        raise XpcError(f"no config at {path}; create it (see --help) with each peer's ssh alias and project roots")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    hosts = cfg.get("hosts") or {}
    for hid, h in hosts.items():
        if not h.get("ssh") or not h.get("roots"):
            raise XpcError(f"host {hid!r} needs 'ssh' and 'roots' in {path}")
    cfg["hosts"] = {k.lower(): v for k, v in hosts.items()}
    cfg["_path"] = str(path)
    return cfg


def this_host(cfg: dict) -> str:
    return (os.environ.get("CROSS_PC_HOST") or cfg.get("self") or os.environ.get("COMPUTERNAME")
            or os.environ.get("HOSTNAME") or "").lower()


def peer(cfg: dict, host: str) -> dict:
    h = cfg["hosts"].get(host.lower())
    if not h:
        raise XpcError(f"unknown host {host!r}; configured: {', '.join(sorted(cfg['hosts'])) or 'none'}")
    if host.lower() == this_host(cfg):
        raise XpcError(f"{host} is this PC; work on the local checkout instead")
    return h


def git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise XpcError(f"git {' '.join(args)} failed in {repo}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def norm_origin(url: str | None) -> str | None:
    """git@github.com:Owner/Name.git, https://github.com/owner/name and ssh://git@github.com/owner/name.git
    all become 'github.com/owner/name'. Unknown shapes return None."""
    if not url:
        return None
    u = url.strip()
    m = (re.match(r"^(?:ssh|https?|git)://(?:[^@/]+@)?([\w.-]+)(?::\d+)?/(.+)$", u)
         or re.match(r"^[\w.-]+@([\w.-]+):(.+)$", u))
    if not m:
        return None
    path = m.group(2).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if path.count("/") != 1:
        return None
    return f"{m.group(1).lower()}/{path.lower()}"


def local_identity(repo: str) -> dict:
    top = git(repo, "rev-parse", "--show-toplevel")
    if git(top, "rev-parse", "--is-shallow-repository") == "true":
        raise XpcError("this checkout is shallow, so its root commit is not the real one; unshallow it first")
    roots = sorted(set(git(top, "rev-list", "--max-parents=0", "HEAD").split()))
    if not roots:
        raise XpcError("this repo has no commits")
    try:
        origin = git(top, "remote", "get-url", "origin")
    except XpcError:
        origin = None
    return {"top": top, "roots": roots, "origin": norm_origin(origin)}


def canonical(origin: str | None) -> str | None:
    """Resolve a GitHub rename redirect with gh, when gh is installed. Best effort."""
    if not origin or not origin.startswith("github.com/") or not shutil.which("gh"):
        return origin
    try:
        r = subprocess.run(["gh", "api", f"repos/{origin.split('/', 1)[1]}", "-q", ".full_name"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return origin
    return f"github.com/{r.stdout.strip().lower()}" if r.returncode == 0 and r.stdout.strip() else origin


# ---------------------------------------------------------------- matching and path rules

def choose(ident: dict, candidates: list[dict], resolve=canonical) -> dict:
    """Pick the one remote checkout that is this repo. Raises XpcError when none or several fit."""
    want = set(ident["roots"])
    seen, matches, near = set(), [], []
    for c in candidates:
        if not want & set(c.get("roots") or []):
            continue
        if c.get("kind") != "repo" or c.get("shallow"):
            near.append(c)
            continue
        key = str(c["path"]).rstrip("\\/").lower()
        if key not in seen:
            seen.add(key)
            matches.append(c)
    if len(matches) > 1 and ident.get("origin"):
        mine = resolve(ident["origin"])
        same = [c for c in matches if norm_origin(c.get("origin")) in (ident["origin"], mine)
                or resolve(norm_origin(c.get("origin"))) == mine]
        if same:
            matches = same
    if len(matches) == 1:
        return matches[0]
    if not matches:
        hint = "".join(f"\n  skipped {c['kind']}{' (shallow)' if c.get('shallow') else ''}: {c['path']}" for c in near)
        raise XpcError("no checkout of this repo under the host's roots" + hint)
    raise XpcError("several checkouts share this repo's history and origin did not decide:\n  "
                   + "\n  ".join(f"{c['path']} ({c.get('origin') or 'no origin'})" for c in matches))


def check_rel(rel: str | None) -> str:
    """A relative path that stays inside the checkout. Returns it with backslashes, '.' for the root."""
    if rel in (None, "", "."):
        return "."
    if "\0" in rel or ":" in rel or rel.startswith(("~", "\\\\", "//")):
        raise XpcError(f"refused path {rel!r}: drives, streams, UNC and ~ are not allowed")
    p = PureWindowsPath(rel)
    if p.is_absolute() or p.drive or p.root:
        raise XpcError(f"refused path {rel!r}: give a path relative to the checkout")
    parts: list[str] = []
    for part in p.parts:
        if part == "..":
            if not parts:
                raise XpcError(f"refused path {rel!r}: it leaves the checkout")
            parts.pop()
        elif part != ".":
            parts.append(part)
    return "\\".join(parts) or "."


def check_readable(rel: str) -> None:
    for part in PureWindowsPath(rel).parts:
        if part.lower() in SECRET_DIRS:
            raise XpcError(f"refused {rel!r}: {part} is not readable through this helper")
    name = PureWindowsPath(rel).name
    if any(p.match(name) for p in SAFE_NAMES):
        return
    if any(p.match(name) for p in SECRET_NAMES):
        raise XpcError(f"refused {rel!r}: it looks like a secret or machine-local settings file")


# ---------------------------------------------------------------- remote execution

def ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


PRELUDE = r"""
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$NL = [string][char]10
function Emit($o) { '@@XPC@@' + ($o | ConvertTo-Json -Compress -Depth 6) }
function Roots($p) { @(& git -C $p rev-list --max-parents=0 HEAD 2>$null) }
function Confine($root, $rel) {
  $r = $root.TrimEnd('\')
  $full = [IO.Path]::GetFullPath([IO.Path]::Combine($r, $rel)).TrimEnd('\')
  if (-not ($full -eq $r -or $full.StartsWith($r + '\', [StringComparison]::OrdinalIgnoreCase))) { throw "outside the checkout: $rel" }
  $cur = $r
  foreach ($seg in $full.Substring($r.Length).Split([char[]]'\', [StringSplitOptions]::RemoveEmptyEntries)) {
    $cur = Join-Path $cur $seg
    if ((Test-Path -LiteralPath $cur) -and ((Get-Item -LiteralPath $cur -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "goes through a link or junction: $seg" }
  }
  $full
}
"""

BOOT = ("$s=[Console]::In.ReadToEnd();"
        "$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($s.Trim()));"
        "& ([scriptblock]::Create($t))")


def wrap(body: str) -> str:
    return PRELUDE + "\ntry {\n" + body + "\n} catch { Emit @{ error = $_.Exception.Message } }\n"


def run_remote(h: dict, body: str, timeout: int = 120) -> dict:
    """Run PRELUDE + body on the host. The script travels base64-encoded on stdin, so no remote shell
    quoting or command-line length limit applies; the command line is a fixed bootstrap."""
    boot = base64.b64encode(BOOT.encode("utf-16-le")).decode()
    cmd = ["ssh", *SSH_OPTS, h["ssh"], "powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", boot]
    try:
        r = subprocess.run(cmd, input=base64.b64encode(wrap(body).encode("utf-8")).decode(), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise XpcError(f"no answer from {h['ssh']} within {timeout}s (a remote process may still be running)")
    out = parse_marker(r.stdout)
    if out is None:
        err = re.sub(r"#< CLIXML|<Objs[\s\S]*?</Objs>", "", r.stderr).strip()
        raise XpcError(f"ssh {h['ssh']} gave no result (exit {r.returncode}): {err[:300] or r.stdout[:300]}")
    if "error" in out:
        raise XpcError(f"{h['ssh']}: {out['error']}")
    return out


def parse_marker(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith(MARK):
            return json.loads(line[len(MARK):])
    return None


def ps_list(items) -> str:
    return "@(" + ", ".join(ps_quote(x) for x in items) + ")"


def scan_script(roots: list[str], want: list[str]) -> str:
    return ("$want = " + ps_list(want) + "\n$out = @()\nforeach ($r in " + ps_list(roots) + ") {\n"
            r"""  if (-not (Test-Path -LiteralPath $r)) { continue }
  foreach ($d in Get-ChildItem -LiteralPath $r -Directory -Force -ErrorAction SilentlyContinue) {
    $g = Join-Path $d.FullName '.git'
    if ($d.Attributes -band [IO.FileAttributes]::ReparsePoint) { $kind = 'link' }
    elseif (Test-Path -LiteralPath $g -PathType Container) { $kind = 'repo' }
    elseif (Test-Path -LiteralPath $g -PathType Leaf) { $kind = 'worktree' } else { continue }
    $roots = Roots $d.FullName
    if (-not ($roots | Where-Object { $want -contains $_ })) { continue }
    $out += @{ path = $d.FullName; kind = $kind; roots = $roots
      origin = "$(& git -C $d.FullName remote get-url origin 2>$null)"
      shallow = ("$(& git -C $d.FullName rev-parse --is-shallow-repository 2>$null)" -eq 'true') }
  }
}
Emit @{ candidates = $out }""")


def guard(path: str, want: list[str]) -> str:
    """Remote preamble: stop with stale=true if the cached path is no longer this repo's checkout."""
    return ("$p = " + ps_quote(path) + "\n$want = " + ps_list(want) + "\n"
            "if (-not (Test-Path -LiteralPath (Join-Path $p '.git') -PathType Container) -or "
            "-not ((Roots $p) | Where-Object { $want -contains $_ })) { Emit @{ stale = $true }; return }\n")


def cache_load() -> dict:
    try:
        return json.loads(CACHE.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cache_save(data: dict) -> None:
    f = CACHE.expanduser()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=1), encoding="utf-8")


def as_list(v) -> list:
    """ConvertTo-Json turns a one-item array into a bare value and an empty one into null."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def locate(cfg: dict, host: str, ident: dict, refresh: bool = False) -> str:
    h = peer(cfg, host)
    key = f"{host.lower()}|{','.join(ident['roots'])}|{ident.get('origin') or ''}"
    cache = cache_load()
    if not refresh and key in cache:
        return cache[key]
    found = as_list(run_remote(h, scan_script(h["roots"], ident["roots"]), timeout=180).get("candidates"))
    for c in found:
        c["roots"] = as_list(c.get("roots"))
    path = choose(ident, found)["path"]
    cache[key] = path
    cache_save(cache)
    return path


def in_checkout(cfg: dict, host: str, ident: dict, body: str, timeout: int = 120) -> dict:
    """Run body inside the matched checkout ($p), rescanning once if the cached location went stale."""
    h = peer(cfg, host)
    for refresh in (False, True):
        path = locate(cfg, host, ident, refresh=refresh)
        out = run_remote(h, guard(path, ident["roots"]) + body, timeout=timeout)
        if not out.get("stale"):
            out["checkout"] = path
            return out
    raise XpcError(f"the checkout on {host} moved during the call; run locate {host} --refresh")


# ---------------------------------------------------------------- verb bodies

STATUS = r"""
$up = "$(& git -C $p rev-list --left-right --count '@{u}...HEAD' 2>$null)".Split()
Emit @{ branch = "$(& git -C $p rev-parse --abbrev-ref HEAD)"; head = "$(& git -C $p log -1 --format='%h %cI %s')"
  behind = $up[0]; ahead = $up[1]; status = @(& git -C $p status --short | Select-Object -First 200)
  worktrees = @(& git -C $p worktree list); stashes = @(& git -C $p stash list).Count }
"""


def body_log(n: int) -> str:
    return "Emit @{ log = @(& git -C $p log -n " + str(int(n)) + " --format='%h%x09%cI%x09%an%x09%s') }\n"


def body_diff(rel: str | None) -> str:
    if rel is None:
        return "Emit @{ unstaged = @(& git -C $p diff --stat); staged = @(& git -C $p diff --cached --stat) }\n"
    return ("$f = Confine $p " + ps_quote(rel) + "\n"
            "$d = (& git -C $p diff HEAD -- $f) -join $NL\n"
            "if ($d.Length -gt 200000) { $d = $d.Substring(0, 200000) + $NL + '[cut at 200000 chars]' }\n"
            "Emit @{ diff = $d }\n")


def body_ls(rel: str) -> str:
    return ("$f = Confine $p " + ps_quote(rel) + "\n"
            "Emit @{ entries = @(Get-ChildItem -LiteralPath $f -Force | Select-Object -First 500 | ForEach-Object {"
            " @{ name = $_.Name; dir = $_.PSIsContainer; size = $(if ($_.PSIsContainer) { $null } else { $_.Length });"
            " modified = $_.LastWriteTimeUtc.ToString('s') + 'Z' } }) }\n")


def body_cat(rel: str, max_bytes: int) -> str:
    return ("$f = Confine $p " + ps_quote(rel) + "\n$max = " + str(int(max_bytes)) + "\n"
            r"""$i = Get-Item -LiteralPath $f -Force
if ($i.PSIsContainer) { throw "a directory; use ls" }
if ($i.Length -gt $max) { throw "$($i.Length) bytes is over --max-bytes $max" }
$b = [IO.File]::ReadAllBytes($f)
if ($b -contains 0) { throw "binary file" }
Emit @{ text = [Text.Encoding]::UTF8.GetString($b); bytes = $b.Length }
""")


def agent_args(harness: str, write: bool, model: str | None, path: str) -> tuple[str, list[str]]:
    if harness == "claude":
        args = ["-p", "--output-format", "text", "--permission-mode", "acceptEdits" if write else "dontAsk"]
        if model:
            args += ["--model", model]
        if not write:
            args += ["--allowedTools", *CLAUDE_READ_TOOLS]  # last: the flag takes a variadic list
        return "claude", args
    if harness == "codex":
        args = ["exec", "-C", path, "-s", "workspace-write" if write else "read-only"]
        if model:
            args += ["-m", model]
        return "codex", args
    raise XpcError(f"unknown harness {harness!r}")


FALLBACKS = {"claude": [r"$env:USERPROFILE\.local\bin\claude.exe", r"$env:APPDATA\npm\claude.cmd"],
             "codex": [r"$env:APPDATA\npm\codex.cmd", r"$env:USERPROFILE\.local\bin\codex.exe"]}


def body_agent(exe: str, args: list[str], prompt: str, timeout: int) -> str:
    """The prompt goes to the agent's stdin from a temp file, never on a command line."""
    fallbacks = "@(" + ", ".join('"' + x + '"' for x in FALLBACKS[exe]) + ")"
    b64 = base64.b64encode(prompt.encode("utf-8")).decode()
    return ("$exe = (Get-Command " + exe + " -ErrorAction SilentlyContinue | Select-Object -First 1).Source\n"
            "foreach ($c in " + fallbacks + ") { if (-not $exe -and (Test-Path -LiteralPath $c)) { $exe = $c } }\n"
            "if (-not $exe) { throw '" + exe + " is not installed on this PC' }\n"
            "$in = Join-Path $env:TEMP ('xpc-' + [guid]::NewGuid() + '.in'); $o = \"$in.out\"; $e = \"$in.err\"\n"
            "$enc = New-Object System.Text.UTF8Encoding $false\n"
            "[IO.File]::WriteAllText($in, [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + b64 + "')), $enc)\n"
            "$a = " + ps_quote(subprocess.list2cmdline(args)) + "\n"
            "$proc = Start-Process -FilePath $exe -ArgumentList $a -WorkingDirectory $p -NoNewWindow -PassThru "
            "-RedirectStandardInput $in -RedirectStandardOutput $o -RedirectStandardError $e\n"
            "$null = $proc.Handle\n"
            "$done = $proc.WaitForExit(" + str(int(timeout) * 1000) + ")\n"
            "if (-not $done) { try { $proc.Kill() } catch {} }\n"
            "$res = @{ exit = $(if ($done) { $proc.ExitCode } else { $null }); timeout = (-not $done); harness = $exe\n"
            "  stdout = [IO.File]::ReadAllText($o, $enc)\n"
            "  stderr = ((Get-Content -LiteralPath $e -Encoding UTF8 -ErrorAction SilentlyContinue | Select-Object -Last 20) -join $NL) }\n"
            "Remove-Item -LiteralPath $in, $o, $e -ErrorAction SilentlyContinue\nEmit $res\n")


# ---------------------------------------------------------------- CLI

def show(out: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return
    for k, v in out.items():
        if isinstance(v, list):
            print(f"{k}:")
            for item in v:
                print(f"  {item}")
        elif isinstance(v, str) and "\n" in v:
            print(f"{k}:\n{v}")
        else:
            print(f"{k}: {v}")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--repo", default=".", help="local checkout whose twin to use (default: current directory)")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hosts").add_argument("--probe", action="store_true")
    p = sub.add_parser("locate"); p.add_argument("host"); p.add_argument("--refresh", action="store_true")
    sub.add_parser("status").add_argument("host")
    p = sub.add_parser("log"); p.add_argument("host"); p.add_argument("-n", type=int, default=10)
    p = sub.add_parser("diff"); p.add_argument("host"); p.add_argument("path", nargs="?")
    p = sub.add_parser("ls"); p.add_argument("host"); p.add_argument("path", nargs="?", default=".")
    p = sub.add_parser("cat"); p.add_argument("host"); p.add_argument("path")
    p.add_argument("--max-bytes", type=int, default=262144)
    p = sub.add_parser("agent"); p.add_argument("host")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--prompt"); g.add_argument("--prompt-file")
    p.add_argument("--harness", choices=("claude", "codex"), default="claude")
    p.add_argument("--write", action="store_true", help="let the agent edit files (only with the user's approval)")
    p.add_argument("--model"); p.add_argument("--timeout", type=int, default=600)
    return ap


def cmd_hosts(cfg: dict, probe: bool) -> dict:
    rows = []
    for hid, h in sorted(cfg["hosts"].items()):
        row = {"host": hid, "ssh": h["ssh"], "roots": h["roots"], "self": hid == this_host(cfg)}
        if probe and not row["self"]:
            try:
                r = subprocess.run(["ssh", "-n", *SSH_OPTS, h["ssh"], "hostname"], capture_output=True,
                                   text=True, timeout=40)
                row["reachable"] = r.returncode == 0
                row["hostname"] = r.stdout.strip() if r.returncode == 0 else r.stderr.strip()[:200]
            except subprocess.TimeoutExpired:
                row["reachable"] = False
        rows.append(row)
    return {"config": cfg["_path"], "this_host": this_host(cfg), "hosts": rows}


def main(argv=None) -> int:
    a = parser().parse_args(argv)
    try:
        cfg = load_config(a.config)
        if a.cmd == "hosts":
            show(cmd_hosts(cfg, a.probe), a.json)
            return 0
        ident = local_identity(a.repo)
        if a.cmd == "locate":
            show({"host": a.host, "checkout": locate(cfg, a.host, ident, refresh=a.refresh)}, a.json)
            return 0
        if a.cmd == "status":
            out = in_checkout(cfg, a.host, ident, STATUS)
        elif a.cmd == "log":
            out = in_checkout(cfg, a.host, ident, body_log(a.n))
        elif a.cmd == "diff":
            out = in_checkout(cfg, a.host, ident, body_diff(None if a.path is None else check_rel(a.path)))
        elif a.cmd == "ls":
            out = in_checkout(cfg, a.host, ident, body_ls(check_rel(a.path)))
        elif a.cmd == "cat":
            rel = check_rel(a.path)
            check_readable(rel)
            out = in_checkout(cfg, a.host, ident, body_cat(rel, a.max_bytes))
            if not a.json:
                sys.stdout.write(out["text"])
                return 0
        else:
            return cmd_agent(cfg, ident, a)
        show(out, a.json)
        return 0
    except XpcError as e:
        print(f"cross-pc-ssh: {e}", file=sys.stderr)
        return 2


def cmd_agent(cfg: dict, ident: dict, a) -> int:
    prompt = a.prompt if a.prompt is not None else Path(a.prompt_file).read_text(encoding="utf-8")
    if len(prompt) > MAX_PROMPT:
        raise XpcError(f"prompt is {len(prompt)} chars; keep it under {MAX_PROMPT} or point at a file in the repo")
    path = locate(cfg, a.host, ident)
    exe, args = agent_args(a.harness, a.write, a.model, path)
    out = in_checkout(cfg, a.host, ident, body_agent(exe, args, prompt, a.timeout), timeout=a.timeout + 90)
    if a.json:
        show(out, True)
    else:
        sys.stdout.write((out.get("stdout") or "").rstrip() + "\n")
        print(f"[{a.harness} on {a.host} in {out['checkout']}: exit {out.get('exit')}"
              f"{', timed out' if out.get('timeout') else ''}{'' if a.write else ', read-only'}]", file=sys.stderr)
        if out.get("stderr"):
            print(out["stderr"], file=sys.stderr)
    return 0 if out.get("exit") == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
