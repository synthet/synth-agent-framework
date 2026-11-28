#!/usr/bin/env python3
"""Cross-PC messages through a two-way synced folder (Resilio Sync, Syncthing, OneDrive, an SMB share).

The share holds one-way folders named ``<from>-to-<to>``. Only the sender writes in its own outgoing
folders. The share's own ``AGENTS.md`` is the protocol authority, and this script reads its host table.

Every command except `archive` only ADDS files: it never deletes, moves or renames anything in the share,
because every change syncs to every PC. `archive` is the owner-approved exception: it is a dry run unless
--apply, and it moves only this host's own finished threads out of its own <me>-to-<peer> folders.

Thread names: a thread starts as <topic>-<date> (package folder or note); every follow-up in either
direction is <topic>-<date>-<word>.md (reply, result, status, ack, done, fix, addendum, ...).

Commands:
  whoami                      share root, this host, peers, incoming/outgoing folders
  status [--since DAYS]       threads per peer (topic + its -reply/-result/... follow-ups); whose turn it is;
                              done threads only with --all
  new   --to PEER --topic T   package <T>-<date>/README.md (--note: one <T>-<date>.md). --print shows the
                              template only; --from-file DRAFT writes a checked local draft (atomic send)
  reply --to PEER --re NAME   <topic>-<date>-<kind>.md (--kind reply|result|status|ack, or --suffix WORD);
                              --body-file DRAFT = atomic reply. NAME may also be your own outgoing item
  close --to PEER --re NAME   <topic>-<date>-done.md: the thread is finished (either side may close)
  lint                        read-only: nonconforming names, root clutter, big files, unknown hosts; exit 1
  archive [--apply]           dry run: this host's done (or --older-than N days idle) threads; --apply moves
                              them to private/archive/cross-pc/ with a manifest
  watch [--once]              print NEW:/CHANGED: lines for incoming files; --once exits on the first event
  check PATH...               flag secrets and unfilled placeholders before you send
  manifest                    print SHA256SUMS for this skill copy (writes nothing)
  verify                      running + installed copies vs the share's newest v<N>/SHA256SUMS; exit 1 on drift

Share root: --root, env CROSS_PC_SHARE, ~/.config/cross-pc-mail.json {"root": "..."}, then common
Resilio/Syncthing locations. Host id: --host, env CROSS_PC_HOST, then COMPUTERNAME/HOSTNAME lowercased.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

VERSION = "4"  # equals the share's private/guides/cross-pc-messages/v<N>/; bump on any change
SKILL_DIR = Path(__file__).resolve().parents[1]
RELEASES = Path("private/guides/cross-pc-messages")  # relative to the share root
# Global skill trees, as installed by the framework's agent_setup.py.
INSTALL_TREES = ("~/.claude/skills", "~/.agents/skills", "~/.gemini/config/skills", "~/.gemini/antigravity-cli/skills")
PAIR = re.compile(r"^([a-z0-9][a-z0-9-]*?)-to-([a-z0-9][a-z0-9-]*)$")
HOST_ROW = re.compile(r"^\|\s*`([a-z0-9][a-z0-9-]*)`\s*\|\s*([^|]+?)\s*\|")
TOPIC = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REPLY_KINDS = ("reply", "result", "status", "ack")
DATE = r"\d{4}-\d{2}-\d{2}"
WORD = r"[a-z0-9]+(?:-[a-z0-9]+)*"
# <topic>-<date>[-<suffix>]: the topic is everything before the first date.
NAME = re.compile(rf"^({WORD}?)-({DATE})(?:-({WORD}))?$")
LEGACY_BEFORE_DATE = re.compile(rf"^({WORD})-([a-z0-9]+)-({DATE})$")  # <base>-<word>-<date>, one word
DATE_FIRST = re.compile(rf"^({DATE})-({WORD})$")
# Words that mark a legacy <base>-<word>-<date> note as a follow-up of <base>-<date> rather than a new topic.
FOLLOWUP_WORDS = {*REPLY_KINDS, "done", "fix", "addendum", "correction", "warning", "update", "notice", "followup"}
ROOT_ALLOWED = {"AGENTS.md", "CLAUDE.md", "desktop.ini", ".sync", "private"}
ARCHIVE = Path("private/archive/cross-pc")  # relative to the share root
SETTLE_SECONDS = 600  # archive refuses threads with anything modified this recently
# "Replies to:" links: <from>-to-<to>/<item>, where <item> ends at /, ), `, ], whitespace or end of line.
REPLY_LINK = re.compile(r"([a-z0-9][a-z0-9-]*-to-[a-z0-9][a-z0-9-]*)/([^/)\]`\s]+)(?=[/)\]`\s]|$)")
FYI_LINE = re.compile(r"(?im)^\s*-\s*\*\*Reply needed:\*\*\s*no\b")  # a header line, not a quote in prose
PLACEHOLDERS = [
    # The exact placeholders HEADER writes, so quoting a fragment in prose is not a finding.
    ("placeholder", re.compile(r"<one sentence: why this message exists>|<what the receiver must not do")),
    ("empty-list-item", re.compile(r"^\s*(?:\d+\.|-)\s*$")),
]
CANDIDATES = ("~/Resilio Sync", "C:/Resilio Sync", "D:/Resilio Sync", "~/Sync", "~/Syncthing")
CONFIG = Path("~/.config/cross-pc-mail.json")

SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("api-key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}")),
    ("hf-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("assignment", re.compile(
        r"(?i)\b[\w.-]*(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[\"']?(?!<)[A-Za-z0-9+/_=.-]{12,}")),
]


def norm(path: str | os.PathLike) -> Path:
    """Accept Windows, POSIX and Git Bash (/c/...) spellings."""
    s = str(path)
    m = re.match(r"^/([a-zA-Z])/(.*)$", s)
    if m and os.name == "nt":
        s = f"{m.group(1).upper()}:/{m.group(2)}"
    return Path(os.path.expanduser(s))


def is_share(root: Path) -> bool:
    return root.is_dir() and any(PAIR.match(p.name) for p in root.iterdir() if p.is_dir())


def find_root(arg: str | None) -> Path:
    tried = []
    sources = [arg, os.environ.get("CROSS_PC_SHARE")]
    cfg = norm(CONFIG)
    if cfg.is_file():
        try:
            sources.append(json.loads(cfg.read_text(encoding="utf-8")).get("root"))
        except (OSError, ValueError):
            pass
    sources += list(CANDIDATES)
    for s in sources:
        if not s:
            continue
        p = norm(s)
        tried.append(str(p))
        if is_share(p):
            return p.resolve()
    sys.exit("No share found (a folder with <from>-to-<to> subfolders). Tried: " + "; ".join(tried)
             + ". Set CROSS_PC_SHARE or pass --root.")


def host_table(root: Path) -> dict[str, str]:
    """Host id -> machine, from the share's AGENTS.md table only."""
    found: dict[str, str] = {}
    agents = root / "AGENTS.md"
    if agents.is_file():
        for line in agents.read_text(encoding="utf-8", errors="replace").splitlines():
            m = HOST_ROW.match(line)
            if m:
                found[m.group(1)] = m.group(2)
    return found


def hosts(root: Path) -> dict[str, str]:
    """Host table from the share's AGENTS.md, plus any host named only by a folder pair."""
    found = host_table(root)
    for p in root.iterdir():
        m = PAIR.match(p.name) if p.is_dir() else None
        if m:
            for h in m.groups():
                found.setdefault(h, "")
    return found


def find_host(arg: str | None, known: dict[str, str]) -> str:
    raw = arg or os.environ.get("CROSS_PC_HOST") or os.environ.get("COMPUTERNAME") or socket.gethostname()
    want = raw.strip().lower()
    if want in known:
        return want
    for hid, machine in known.items():  # match the Machine column, e.g. "STRIX (desktop, ...)"
        if machine.lower().split(" (")[0].strip() == want:
            return hid
    sys.exit(f"Host {raw!r} is not in the share's host table {sorted(known)}. Pass --host or set CROSS_PC_HOST.")


def context(args) -> dict:
    root = find_root(args.root)
    known = hosts(root)
    me = find_host(args.host, known)
    pairs = [PAIR.match(p.name) for p in sorted(root.iterdir()) if p.is_dir()]
    pairs = [m for m in pairs if m]
    return {
        "root": root, "host": me, "hosts": known,
        "outgoing": {m.group(2): root / m.group(0) for m in pairs if m.group(1) == me},
        "incoming": {m.group(1): root / m.group(0) for m in pairs if m.group(2) == me},
    }


def ignored(p: Path) -> bool:
    return p.name.endswith(".!sync") or p.name == "desktop.ini" or any(part == ".sync" for part in p.parts)


def items(folder: Path) -> list[Path]:
    """Top-level messages: package folders (they have a README.md) and single notes."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if not ignored(p) and ((p.is_dir() and (p / "README.md").is_file()) or p.suffix == ".md"))


def written(p: Path) -> float:
    """When a message was written: a package's newest file (folder mtimes move when the sync tool touches them)."""
    if p.is_dir():
        times = [f.stat().st_mtime for f in p.rglob("*") if f.is_file() and not ignored(f)]
        if times:
            return max(times)
    return p.stat().st_mtime


def stem(p: Path) -> str:
    return p.name[:-3] if p.name.endswith(".md") else p.name


def parse_name(s: str) -> dict:
    """Split a message stem into topic, date and follow-up suffix, and name its shape.

    Shapes: base (<topic>-<date>), followup (<topic>-<date>-<word>), second-date (a date inside the suffix),
    date-first (<date>-<topic>), undated (<topic>), uppercase, bad-name.
    """
    out = {"topic": "", "date": "", "suffix": "", "shape": "bad-name"}
    if not re.match(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$", s):
        return out
    if s != s.lower():
        return {**out, "shape": "uppercase"}
    m = NAME.match(s)
    if m:
        suffix = m.group(3) or ""
        shape = "second-date" if re.search(DATE, suffix) else ("followup" if suffix else "base")
        return {"topic": m.group(1), "date": m.group(2), "suffix": suffix, "shape": shape}
    m = DATE_FIRST.match(s)
    if m:
        return {"topic": m.group(2), "date": m.group(1), "suffix": "", "shape": "date-first"}
    return {**out, "topic": s, "shape": "undated"}


def thread_keyer(paths: list[Path]):
    """Return a function mapping a message stem to its thread name, given every message of one peer pair.

    1. A name that starts with an existing message's name plus `-` joins it (`x-2026-11-18-fix` -> `x-2026-11-18`).
    2. Otherwise a `<topic>-<date>-<suffix>` name anchors at `<topic>-<date>`, even when that base is gone.
    3. Legacy suffix-before-date: a note `<base>-<word>-<date>` joins an existing `<base>-<date>` thread
       only as legacy_base() allows. A package folder is always a thread of its own.
    """
    stems = sorted({stem(p) for p in paths}, key=len)
    known = set(stems)
    legacy = {}
    for p in paths:
        base = legacy_base(p, known)
        if base:
            legacy[stem(p)] = base

    def key(s: str) -> str:
        r = next((x for x in stems if s == x or s.startswith(x + "-")), s)
        if r == s:
            n = parse_name(s)
            if n["shape"] in ("followup", "second-date"):
                r = f"{n['topic']}-{n['date']}"
        return legacy.get(r, r)
    return key


def legacy_base(p: Path, known: set[str]) -> str | None:
    """The `<base>-<date>` thread a legacy note `<base>-<word>-<date>.md` belongs to, or None.

    Only when `<word>` is a follow-up word (FOLLOWUP_WORDS) or the note's "Replies to:" links the base:
    a same-day topic that merely extends another one's name (`x-<other>-<date>`) stays its own thread.
    """
    m = LEGACY_BEFORE_DATE.match(stem(p)) if not p.is_dir() else None
    if not m or f"{m.group(1)}-{m.group(3)}" not in known:
        return None
    base = f"{m.group(1)}-{m.group(3)}"
    if m.group(2) in FOLLOWUP_WORDS:
        return base
    return base if base in {stem(Path(n)) for _, n in REPLY_LINK.findall(replies_field(header(p)))} else None


# -- commands -----------------------------------------------------------------------------------

def cmd_whoami(args) -> int:
    c = context(args)
    out = {"version": VERSION, "sha256": sha256(Path(__file__)),
           "root": str(c["root"]), "host": c["host"], "machine": c["hosts"].get(c["host"], ""),
           "peers": sorted(set(c["outgoing"]) | set(c["incoming"])),
           "outgoing": {k: str(v) for k, v in c["outgoing"].items()},
           "incoming": {k: str(v) for k, v in c["incoming"].items()}}
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        print(f"cross_pc_mail v{VERSION} sha256 {out['sha256'][:12]}")
        print(f"share: {out['root']}\nhost:  {out['host']} {out['machine']}")
        for k in out["peers"]:
            print(f"  {k}: out {c['outgoing'].get(k, '-')}\n  {'':{len(k)}}  in  {c['incoming'].get(k, '-')}")
    return 0


def peer_messages(c: dict, peer: str) -> list[tuple[str, Path]]:
    """("in" | "out", path) for every message between this host and one peer."""
    inbox, outbox = c["incoming"].get(peer), c["outgoing"].get(peer)
    msgs = [("in", p) for p in items(inbox)] if inbox else []
    msgs += [("out", p) for p in items(outbox)] if outbox else []
    return msgs


def thread_groups(c: dict, peer: str):
    """thread name -> [(written, direction, name, fyi)] sorted oldest first, plus the stem -> thread keyer."""
    inbox, outbox = c["incoming"].get(peer), c["outgoing"].get(peer)
    msgs = peer_messages(c, peer)
    key = thread_keyer([p for _, p in msgs])
    own = {key(stem(p)) for _, p in msgs}
    groups: dict[str, list] = {}
    for d, p in msgs:
        head = header(p)
        entry = (written(p), d, p.name, bool(FYI_LINE.search(head)))
        roots = {key(stem(p))}
        other = (inbox if d == "out" else outbox)
        if other is not None:
            roots |= {key(stem(Path(name))) for pair, name in REPLY_LINK.findall(replies_field(head))
                      if pair == other.name} & own
        for r in roots:
            groups.setdefault(r, []).append(entry)
    for ms in groups.values():
        ms.sort()
    return groups, key


def is_done(thread: str, ms: list) -> bool:
    """A thread is done when its newest message is `<thread>-done.md`; any later message reopens it."""
    return stem(Path(ms[-1][2])) == f"{thread}-done"


def archive_dir(root: Path, pair: str, thread: str) -> Path:
    """private/archive/cross-pc/<pair>/<topic>/ (an undated thread uses its own name as the topic).

    Entries keep their names, which carry the date, so threads of a reused topic sit side by side.
    """
    n = parse_name(thread)
    return root / ARCHIVE / pair / (n["topic"] if n["date"] else thread)


def archive_marks(root: Path) -> dict[tuple[str, str], float]:
    """(pair, thread) -> when the newest manifest that moved it was written."""
    marks: dict[tuple[str, str], float] = {}
    folder = root / ARCHIVE / "manifests"
    for f in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            when = dt.datetime.fromisoformat(data["created"]).timestamp()
            for m in data["moves"]:
                pair = m["entries"][0]["new"].split("/")[3]  # private/archive/cross-pc/<pair>/...
                marks[(pair, m["thread"])] = max(when, marks.get((pair, m["thread"]), 0.0))
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue
    return marks


def archived_at(c: dict, peer: str, thread: str, marks: dict) -> float | None:
    """When a thread was archived (by either side), or None.

    A thread is archived when its base sits in its archive folder. The time is the newest of its own archived
    entries (renames keep file times) and the manifest that moved it, so a reply the peer left behind, older
    than the archive run, doesn't revive it.
    """
    for pair in (f"{c['host']}-to-{peer}", f"{peer}-to-{c['host']}"):
        d = archive_dir(c["root"], pair, thread)
        own = [p for p in d.iterdir() if not ignored(p) and (stem(p) == thread or stem(p).startswith(thread + "-"))] \
            if d.is_dir() else []
        if any(stem(p) == thread for p in own):
            return max([written(p) for p in own] + ([marks[(pair, thread)]] if (pair, thread) in marks else []))
    return None


def turn_state(ms: list, peer: str) -> str:
    """Whose turn, from the newest message that needs a reply; fyi messages count only for activity."""
    need = [m for m in ms if not m[3]]
    if not need:
        return "fyi"
    return "your turn" if need[-1][1] == "in" else f"waiting on {peer}"


def threads(c: dict, since_days: float | None, show_done: bool = False) -> list[dict]:
    """Group each peer's messages (both directions) into threads (see thread_keyer).

    ``<topic>-<date>-reply.md``, ``-result.md`` and any other ``<topic>-<date>-<word>`` belong to ``<topic>-<date>``.
    Whose turn it is comes from the newest message that needs a reply: from the peer means it's your turn.
    A message whose "Replies to:" line links ``<other-folder>/<item>`` also counts in ``<item>``'s thread,
    so one reply can answer several topics. ``- **Reply needed:** no`` marks a message fyi: it moves the
    thread's last activity but not the turn, and a thread of only fyi messages is ``fyi``;
    a newest ``-done`` message makes it ``done``, and an archive folder with no newer message left in the share
    makes it ``archived`` (both hidden unless show_done).
    """
    cutoff = time.time() - since_days * 86400 if since_days else None
    out = []
    for peer in sorted(set(c["incoming"]) | set(c["outgoing"])):
        groups, _ = thread_groups(c, peer)
        marks = archive_marks(c["root"]) if groups else {}
        for root, ms in groups.items():
            last_t, last_d, last_name, _ = ms[-1]
            if cutoff and last_t < cutoff:
                continue
            archived = archived_at(c, peer, root, marks)
            archived = archived is not None and last_t <= archived
            done = archived or is_done(root, ms)
            if done and not show_done:
                continue
            state = "archived" if archived else "done" if done else turn_state(ms, peer)
            out.append({"peer": peer, "thread": root, "messages": len(ms), "last": last_name,
                        "last_from": peer if last_d == "in" else c["host"],
                        "modified": dt.datetime.fromtimestamp(last_t).strftime("%Y-%m-%d %H:%M"),
                        "state": state})
    return sorted(out, key=lambda t: t["modified"], reverse=True)


def replies_field(head: str) -> str:
    """The "Replies to:" line plus its indented continuation lines (a second link often wraps)."""
    out, inside = [], False
    for line in head.splitlines():
        if "replies to:" in line.lower():
            out.append(line)
            inside = True
        elif inside and line[:1].isspace() and not line.lstrip().startswith("- **"):
            out.append(line)
        else:
            inside = False
    return "\n".join(out)


def header(p: Path, lines: int = 40) -> str:
    """The first lines of a message (a package's README.md), where the header lives."""
    f = p / "README.md" if p.is_dir() else p
    try:
        with f.open(encoding="utf-8", errors="replace") as fh:
            return "".join(next(fh, "") for _ in range(lines))
    except OSError:
        return ""


def cmd_status(args) -> int:
    c = context(args)
    rows = threads(c, args.since, args.all)
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    if not rows:
        print(f"No threads changed in the last {args.since:g} days (use --since 0 for all).")
    for r in rows:
        print(f"{r['modified']}  {r['peer']:<10} {r['state']:<18} {r['thread']}  "
              f"({r['messages']} msg, last {r['last']} from {r['last_from']})")
    return 0


def outbox(c: dict, peer: str) -> Path:
    if peer not in c["hosts"] or peer == c["host"]:
        sys.exit(f"Unknown peer {peer!r}; known: {sorted(h for h in c['hosts'] if h != c['host'])}")
    box = c["outgoing"].get(peer)
    if box is None:
        sys.exit(f"No folder {c['host']}-to-{peer} in the share. Ask the user, then add it and update AGENTS.md.")
    return box


HEADER = """# {title}

- **From:** {sender}
- **For:** {receiver}
- **Purpose:** <one sentence: why this message exists>
- **Reply in:** `{reply}`
- **Don't:** <what the receiver must not do, e.g. run scripts unreviewed, put secrets in a message folder>
"""


def drafted(path, title: bool) -> str:
    """A draft's text, refusing a blank one, or one without a `# ` title line when `title` is set.

    An unquoted shell heredoc runs the backticks in Markdown and can leave the draft empty; a blank note
    would still sync to every PC."""
    text = norm(path).read_text(encoding="utf-8")
    if not text.strip():
        sys.exit(f"{path} is empty. Write the draft with an editor or a quoted heredoc (<<'EOF'). Nothing was written to the share.")
    if title and not any(line.startswith("# ") for line in text.splitlines()):
        sys.exit(f"{path} has no '# ' title line; start from `new --print`. Nothing was written to the share.")
    return text


def cmd_new(args) -> int:
    c = context(args)
    if not TOPIC.match(args.topic):
        sys.exit("Topic must be lowercase words joined by hyphens, e.g. gpu-runner-setup.")
    box = outbox(c, args.to)
    name = f"{args.topic}-{args.date or dt.date.today().isoformat()}"
    target = box / (f"{name}.md" if args.note else name)
    if target.exists() and not args.print:
        sys.exit(f"Refusing to overwrite {target}. Pick another topic or edit your own file.")
    template = HEADER.format(title=f"{c['host'].upper()} → {args.to.upper()}: {args.title or args.topic}",
                             sender=f"agent on {c['host']}", receiver=f"agent on {args.to}",
                             reply=f"{args.to}-to-{c['host']}/{name}-reply.md")
    template += "\n## What to do\n\n1. \n\n## What not to do\n\n- \n"
    if args.print:
        print(template, end="")
        print(f"(would be written to {target})", file=sys.stderr)
        return 0
    text = template
    if args.from_file:  # atomic send: the drafted text lands in the share only if it's clean
        text = drafted(args.from_file, title=True)
        findings = scan(text.splitlines())
        if findings:
            for n, kind in findings:
                print(f"{args.from_file}:{n}: {kind}")
            sys.exit(FIX_HINT.format(n=len(findings)) + ". Nothing was written to the share.")
    if args.note:
        target.write_text(text, encoding="utf-8")
    else:
        target.mkdir()
        (target / "README.md").write_text(text, encoding="utf-8")
        target = target / "README.md"
    print(target)
    if not args.from_file:
        print("Fill in every <...> placeholder and list item, then run `check` until it prints clean.", file=sys.stderr)
    return 0


def find_item(c: dict, peer: str, name: str) -> tuple[Path, Path]:
    """(folder, item): NAME in the peer's folder to this host, else in this host's own folder to the peer."""
    for box in (c["incoming"].get(peer), c["outgoing"].get(peer)):
        if box is not None and (box / name).exists():
            return box, box / name
    sys.exit(f"{peer}-to-{c['host']}/{name} not found (nor in {c['host']}-to-{peer}/); "
             "pass the item's name as listed by status.")


def thread_of(c: dict, peer: str, item: Path) -> str:
    """The thread name an item belongs to, so follow-ups are <topic>-<date>-<word> and names don't grow."""
    base = thread_keyer([p for _, p in peer_messages(c, peer)] + [item])(stem(item))
    if base == stem(item):  # an undated name: strip a trailing kind as before
        for kind in REPLY_KINDS:
            if base.endswith(f"-{kind}"):
                return base[: -len(kind) - 1]
    return base


def replies_to(folder: Path, item: Path) -> str:
    link = f"../{folder.name}/{item.name}" + ("/README.md" if item.is_dir() else "")
    return f"- **Replies to:** [`{folder.name}/{item.name}`]({link})"


def cmd_reply(args) -> int:
    c = context(args)
    box = outbox(c, args.to)
    if args.suffix is not None:
        if args.kind:
            sys.exit("Pass --kind or --suffix, not both.")
        if not TOPIC.match(args.suffix) or re.search(DATE, args.suffix) or args.suffix == "done":
            sys.exit("--suffix must be lowercase words joined by hyphens, without a date and not 'done' "
                     "(use close), e.g. correction or addendum.")
    kind = args.suffix or args.kind or "reply"
    folder, original = find_item(c, args.to, args.re)
    base = thread_of(c, args.to, original)
    target = box / f"{base}-{kind}.md"
    if target.exists():
        sys.exit(f"Refusing to overwrite {target}. Edit your own file, or pick another --kind (or --suffix WORD) "
                 "for a second message.")
    text = HEADER.format(title=f"{c['host'].upper()} → {args.to.upper()}: {kind} to {base}",
                         sender=f"agent on {c['host']}", receiver=f"agent on {args.to}",
                         reply=f"{args.to}-to-{c['host']}/{base}-<kind>.md")
    text = text.replace("- **Purpose:**", replies_to(folder, original) + "\n- **Purpose:**")
    if args.body_file:  # atomic reply: generated header + drafted body, written only if clean
        body = drafted(args.body_file, title=False)
        head = [line for line in text.splitlines() if "**Purpose:**" not in line and "**Don't:**" not in line]
        text = "\n".join(head) + "\n\n" + body
        findings = scan(text.splitlines())
        if findings:
            for n, kind in findings:
                print(f"{target.name} (draft):{n}: {kind}")
            sys.exit(FIX_HINT.format(n=len(findings)) + ". Nothing was written to the share.")
    else:
        text += "\n## Answer\n\n"
        print("Fill in Purpose, Don't and the answer, then run `check` until it prints clean.", file=sys.stderr)
    target.write_text(text, encoding="utf-8")
    print(target)
    return 0


DONE = """# {title}

- **From:** {sender}
- **For:** {receiver}
{replies}
- **Purpose:** close thread `{thread}`: nothing more to do in it
- **Reply needed:** no
- **Don't:** reply in this thread; start a new thread for new work
"""


def cmd_close(args) -> int:
    c = context(args)
    box = outbox(c, args.to)
    folder, original = find_item(c, args.to, args.re)
    base = thread_of(c, args.to, original)
    target = box / f"{base}-done.md"
    if target.exists():
        sys.exit(f"Refusing to overwrite {target}: the thread is already closed from this side.")
    text = DONE.format(title=f"{c['host'].upper()} → {args.to.upper()}: done {base}", sender=f"agent on {c['host']}",
                       receiver=f"agent on {args.to}", replies=replies_to(folder, original), thread=base)
    body = norm(args.body_file).read_text(encoding="utf-8") if args.body_file else (
        "Done: this thread is finished. Its sender may archive it.\n")
    text += "\n" + body
    findings = scan(text.splitlines())
    if findings:
        for n, kind in findings:
            print(f"{target.name} (draft):{n}: {kind}")
        sys.exit(FIX_HINT.format(n=len(findings)) + ". Nothing was written to the share.")
    target.write_text(text, encoding="utf-8")
    print(target)
    return 0


def name_issue(p: Path, stems: set[str]) -> tuple[str | None, str, str | None]:
    """(kind, detail, suggested name) for a top-level entry of a pair folder; kind None when it conforms."""
    s, ext = stem(p), ("" if p.is_dir() else ".md")
    n = parse_name(s)
    shape, topic, date = n["shape"], n["topic"], n["date"]
    if shape == "uppercase":
        low = parse_name(s.lower())["shape"]
        return shape, "names are lowercase", (s.lower() + ext) if low in ("base", "followup") else None
    if shape == "base":
        m = LEGACY_BEFORE_DATE.match(s)
        if m and legacy_base(p, stems):
            return ("suffix-before-date", "follow-ups are <topic>-<date>-<word>.md",
                    f"{m.group(1)}-{m.group(3)}-{m.group(2)}{ext}")
        return None, "", None
    if shape == "followup":
        return None, "", None
    if shape == "second-date":
        rest = re.sub(rf"-?{DATE}", "", n["suffix"]).strip("-")
        return shape, "one date per name, the thread's", (f"{topic}-{date}-{rest}{ext}" if rest else None)
    if shape == "date-first":
        return shape, "the date goes after the topic", f"{topic}-{date}{ext}"
    if shape == "undated":
        return shape, "names end in -YYYY-MM-DD (or -YYYY-MM-DD-<word> for a follow-up)", None
    return shape, "lowercase words joined by hyphens, then -YYYY-MM-DD", None


def cmd_lint(args) -> int:
    """Read-only check of the share's layout. Prints path: kind[: detail][ -> suggestion]; exit 1 on findings."""
    root = find_root(args.root)
    table = host_table(root)
    pairs = [p for p in sorted(root.iterdir()) if p.is_dir() and PAIR.match(p.name)]
    if args.mine:
        me = find_host(args.host, hosts(root))
        pairs = [p for p in pairs if PAIR.match(p.name).group(1) == me]
    if args.pair:
        pairs = [p for p in pairs if p.name == args.pair]
        if not pairs:
            sys.exit(f"No pair folder {args.pair!r} in the share.")
    found: list[dict] = []
    limit = args.max_mb * 1024 * 1024

    def add(p: Path, kind: str, detail: str = "", suggest: str | None = None) -> None:
        found.append({"path": p.relative_to(root).as_posix(), "kind": kind, "detail": detail, "suggest": suggest})

    def large(p: Path) -> None:
        for f in ([p] if p.is_file() else sorted(p.rglob("*"))):
            if f.is_file() and not f.name.endswith(".!sync") and f.stat().st_size > limit:
                add(f, "large-file", f"{f.stat().st_size / 1048576:.1f} MB > {args.max_mb:g} MB; "
                                     "use another copy and send sizes and counts")

    if not args.pair and not args.mine:
        for p in sorted(root.iterdir()):
            if p.name in ROOT_ALLOWED or p.name.endswith(".!sync") or (p.is_dir() and PAIR.match(p.name)):
                continue
            add(p, "root-entry", "only AGENTS.md, CLAUDE.md, desktop.ini, .sync, private and <from>-to-<to> "
                                 "folders belong at the root")
    for pair in pairs:
        a, b = PAIR.match(pair.name).groups()
        unknown = [h for h in (a, b) if h not in table]
        if unknown:
            add(pair, "unknown-host", f"{', '.join(unknown)} not in the AGENTS.md host table")
        stems = {stem(p) for p in items(pair) + items(root / f"{b}-to-{a}")}
        for p in sorted(pair.iterdir()):
            if ignored(p):
                continue
            large(p)
            if p.is_file() and p.suffix != ".md":
                add(p, "stray-file", "messages are .md notes or package folders; put files inside a package")
                continue
            if p.is_dir() and not (p / "README.md").is_file():
                add(p, "no-readme", "a package folder needs a README.md")
            kind, detail, suggest = name_issue(p, stems)
            if kind:
                if suggest and suggest in {x.name for x in pair.iterdir()}:  # case-sensitive, also on Windows
                    detail, suggest = detail + f"; {suggest} already exists", None
                add(p, kind, detail, suggest)
    if args.json:
        print(json.dumps(found, indent=1))
    else:
        for f in found:
            print(f"{f['path']}: {f['kind']}" + (f": {f['detail']}" if f["detail"] else "")
                  + (f" -> {f['suggest']}" if f["suggest"] else ""))
        print("clean" if not found else f"{len(found)} finding(s)")
    return 1 if found else 0


def raw_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_archive(args) -> int:
    """Move this host's finished threads out of its own <me>-to-<peer> folders. Dry run unless --apply.

    A candidate thread starts in <me>-to-<peer> (this host sent its first message) and is done, or with
    --older-than N has had no message in either direction for N days and is finished: an open request
    (waiting on the peer) qualifies only with --include-waiting. Only this host's own entries move
    (the base and its follow-ups there); the peer's folder is never touched. Refused: anything modified
    in the last 10 minutes, *.!sync partials, an existing destination.
    """
    c = context(args)
    root, me, now = c["root"], c["host"], time.time()
    if args.to:
        outbox(c, args.to)
    rel = lambda p: p.relative_to(root).as_posix()  # noqa: E731
    plans, refused, skipped, seen = [], [], [], False
    for peer in ([args.to] if args.to else sorted(c["outgoing"])):
        box, inbox = c["outgoing"].get(peer), c["incoming"].get(peer)
        if box is None:
            continue
        groups, key = thread_groups(c, peer)
        own: dict[str, list[Path]] = {}
        for p in items(box):
            own.setdefault(key(stem(p)), []).append(p)
        for thread, ms in sorted(groups.items()):
            if args.thread and thread != args.thread:
                continue
            seen = True
            entries = own.get(thread, [])
            if not any(stem(p) == thread for p in entries):
                if args.thread:
                    where = inbox.name if inbox is not None and any(stem(p) == thread for p in items(inbox)) \
                        else "no folder (its base is gone)"
                    refused.append((thread, f"its first message is in {where}: only its sender archives it"))
                continue
            idle = (now - ms[-1][0]) / 86400
            if is_done(thread, ms):
                reason = "done"
            elif args.older_than is not None and idle > args.older_than:
                reason = f"idle {idle:.0f} days"
                state = turn_state(ms, peer)
                if state.startswith("waiting on") and not args.include_waiting:
                    why = f"{state} (an open request); pass --include-waiting to archive it"
                    (refused if args.thread else skipped).append((thread, why))
                    continue
            else:
                if args.thread:
                    refused.append((thread, "not done: close it first, or pass --older-than N"))
                continue
            folders = [f for f in (box, inbox) if f is not None]
            partial = any(f.name.endswith(".!sync") and key(stem(Path(f.name[:-6]))) == thread
                          for d in folders for f in d.iterdir())
            partial |= any(x.name.endswith(".!sync") for d in folders for p in items(d)
                           if p.is_dir() and key(stem(p)) == thread for x in p.rglob("*"))
            dest = archive_dir(root, box.name, thread)
            if partial:
                refused.append((thread, "partial download (*.!sync) in the thread; wait for the sync"))
            elif ms[-1][0] > now - SETTLE_SECONDS:
                refused.append((thread, "modified in the last 10 minutes"))
            elif any((dest / p.name).exists() for p in entries):
                taken = next(dest / p.name for p in sorted(entries) if (dest / p.name).exists())
                refused.append((thread, f"destination exists: {rel(taken)}"))
            elif any(p.parent.resolve() != box.resolve() or not box.name.startswith(f"{me}-to-") for p in entries):
                refused.append((thread, f"an entry is outside {box.name}"))
            else:
                plans.append({"peer": peer, "thread": thread, "reason": reason, "dest": dest,
                              "moves": [(p, dest / p.name) for p in sorted(entries)]})
    if args.thread and not seen:
        sys.exit(f"No thread {args.thread!r} with {args.to or 'any peer'}; names are as listed by status --all.")
    for p in plans:
        print(f"{'archive' if args.apply else 'would archive'} {p['peer']} {p['thread']} ({p['reason']}): "
              f"{len(p['moves'])} entries -> {rel(p['dest'])}/")
        for src, _ in p["moves"]:
            print(f"  {rel(src)}{'/' if src.is_dir() else ''}")
    for thread, why in skipped:
        print(f"skipped {thread}: {why}")
    for thread, why in refused:
        print(f"refused {thread}: {why}")
    if not plans and not refused and not skipped:
        print("No threads to archive (done, or idle with --older-than N).")
    if plans and not args.apply:
        print("Dry run: nothing moved. Re-run with --apply to move these entries out of your own folders.")
    if plans and args.apply:
        manifests = root / ARCHIVE / "manifests"
        manifests.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        mpath, n = manifests / f"{me}-{stamp}.json", 2
        while mpath.exists():
            mpath, n = manifests / f"{me}-{stamp}-{n}.json", n + 1
        moves = []
        for p in plans:
            files = []
            for src, dst in p["moves"]:
                for f in ([src] if src.is_file() else sorted(x for x in src.rglob("*") if x.is_file())):
                    files.append({"old": rel(f), "new": rel(dst / f.relative_to(src)) if src.is_dir() else rel(dst),
                                  "sha256": raw_sha256(f), "size": f.stat().st_size})
            moves.append({"peer": p["peer"], "thread": p["thread"], "reason": p["reason"],
                          "entries": [{"old": rel(s), "new": rel(d)} for s, d in p["moves"]], "files": files})
        # Written before the first move, so a crash still leaves the undo list.
        mpath.write_text(json.dumps({"version": VERSION, "host": me, "created": dt.datetime.now().isoformat(
            timespec="seconds"), "moves": moves}, indent=1), encoding="utf-8")
        for p in plans:
            for src, dst in p["moves"]:
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if dst.exists():
                        raise OSError(f"{rel(dst)} appeared during the run")
                    src.rename(dst)  # same share, same volume: a rename, never copy-and-delete
                except OSError as e:
                    print(f"stopped at {rel(src)}: {e}. The lines above list what moved; the manifest "
                          f"{rel(mpath)} also lists moves that did not happen.")
                    return 1
                print(f"moved: {rel(src)} -> {rel(dst)}")
        print(f"manifest: {rel(mpath)}")
        print("Undo: rename each moves[].entries[].new back to its .old path, as listed in the manifest. "
              "Nothing was deleted.")
    return 1 if refused else 0


def snapshot(folders) -> dict[str, tuple[int, int]]:
    snap = {}
    for f in folders:
        for p in f.rglob("*"):
            if p.is_file() and not ignored(p.relative_to(f.parent)):
                st = p.stat()
                snap[str(p)] = (st.st_mtime_ns, st.st_size)
    return snap


def cmd_watch(args) -> int:
    c = context(args)
    folders = list(c["incoming"].values()) + (list(c["outgoing"].values()) if args.all else [])
    if not folders:
        sys.exit("Nothing to watch: no incoming folders for this host.")
    root = c["root"]
    prev = snapshot(folders)
    while True:
        time.sleep(args.interval)
        cur = snapshot(folders)
        events = [f"NEW: {Path(p).relative_to(root).as_posix()}" for p in cur if p not in prev]
        events += [f"CHANGED: {Path(p).relative_to(root).as_posix()}" for p in cur if p in prev and cur[p] != prev[p]]
        for e in sorted(events):
            print(e, flush=True)
        prev = cur
        if events and args.once:
            return 0


def cmd_check(args) -> int:
    hits = 0
    for raw in args.paths:
        base = norm(raw)
        files = [p for p in base.rglob("*") if p.is_file()] if base.is_dir() else [base]
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as e:
                print(f"{f}: unreadable ({e})")
                hits += 1
                continue
            for n, kind in scan(lines, f.suffix.lower() in (".md", ".txt", "")):
                print(f"{f}:{n}: {kind}")  # never echo the matched text
                hits += 1
    print("clean" if not hits else FIX_HINT.format(n=hits))
    return 1 if hits else 0


TEXT_SUFFIXES = {".md", ".py", ".json", ".txt", ".toml", ".yaml", ".yml", ".ps1", ".sh", ".js"}


def sha256(p: Path) -> str:
    """Content hash. Text files hash with CRLF normalized to LF, so a copy from a Windows checkout
    (core.autocrlf=true) and one from an LF tree of the same commit hash the same."""
    data = p.read_bytes()
    if p.suffix.lower() in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def skill_files(skill_dir: Path) -> dict[str, str]:
    """relative path -> sha256 for every file of an installed skill copy (caches excluded)."""
    return {f.relative_to(skill_dir).as_posix(): sha256(f) for f in sorted(skill_dir.rglob("*"))
            if f.is_file() and "__pycache__" not in f.parts and f.suffix not in (".pyc", ".pyo")}


def cmd_manifest(args) -> int:
    """Print SHA256SUMS for a skill copy (default: this one). Writes nothing.

    Build a release manifest from an INSTALLED copy: the installer rewrites Markdown, so a repo copy never matches.
    """
    d = norm(args.dir) if args.dir else SKILL_DIR
    if not (d / "SKILL.md").is_file():
        sys.exit(f"{d} is not a skill directory (no SKILL.md).")
    for rel, digest in skill_files(d).items():
        print(f"{digest}  {rel}")
    return 0


def installed_dirs() -> list[Path]:
    """Existing installed copies of this skill, one per global skill tree."""
    return [norm(t) / SKILL_DIR.name for t in INSTALL_TREES if (norm(t) / SKILL_DIR.name).is_dir()]


def cmd_verify(args) -> int:
    root = find_root(args.root)
    releases = root / RELEASES
    versions = sorted((int(p.name[1:]), p) for p in releases.glob("v*") if p.name[1:].isdigit()) if releases.is_dir() else []
    if not versions:
        sys.exit(f"No release manifest in {releases} (expected v<N>/SHA256SUMS).")
    n, rel_dir = versions[-1]
    if not (rel_dir / "SHA256SUMS").is_file():
        sys.exit(f"{rel_dir} has no SHA256SUMS yet.")
    want = {}
    for line in (rel_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, rel = line.split(None, 1)
            want[rel.strip()] = digest
    installed = installed_dirs()
    copies = [(str(d), d, False) for d in installed]
    if SKILL_DIR not in {d.resolve() for d in installed}:
        copies.insert(0, (f"source {SKILL_DIR}", SKILL_DIR, True))  # not installed: Markdown differs by design
    drift = False
    if VERSION != str(n):
        print(f"running script says v{VERSION}, newest release is v{n}")
        drift = True
    if not installed:
        print("no installed copy in " + ", ".join(INSTALL_TREES))
        drift = True
    for label, d, source in copies:
        have = skill_files(d)
        keys = [r for r in want if not (source and r.endswith(".md"))]
        bad = sorted(r for r in keys if have.get(r) != want[r])
        bad += sorted(r for r in have if r not in want and not (source and r.endswith(".md")))
        scope = " (non-Markdown files)" if source else ""
        print(f"{'same' if not bad else 'DRIFT'} v{n}{scope}  {label}" + ("" if not bad else "  " + ", ".join(bad)))
        drift |= bool(bad)
    return 1 if drift else 0


FIX_HINT = "{n} finding(s): fill in placeholders; move any secret to private storage and point to it instead"


def scan(lines: list[str], text_file: bool = True) -> list[tuple[int, str]]:
    found = []
    for n, line in enumerate(lines, 1):
        for kind, pat in SECRET_PATTERNS + (PLACEHOLDERS if text_file else []):
            if pat.search(line):
                found.append((n, kind))
    return found


def _utf8_stdio() -> None:
    """Print UTF-8 even on a legacy console code page (cp1252 can't encode the templates' arrows)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv=None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", help="share root (default: CROSS_PC_SHARE, config file, common locations)")
    ap.add_argument("--host", help="this host's id (default: CROSS_PC_HOST, then COMPUTERNAME)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("whoami")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.add_argument("--since", type=float, default=14.0, help="only threads changed in the last N days (0 = all)")
    p.add_argument("--all", action="store_true", help="also show done threads")
    p = sub.add_parser("new")
    p.add_argument("--to", required=True)
    p.add_argument("--topic", required=True)
    p.add_argument("--title")
    p.add_argument("--date", help="YYYY-MM-DD (default today)")
    p.add_argument("--note", action="store_true", help="single .md file instead of a package folder")
    p.add_argument("--print", action="store_true", help="print the header template to stdout; write nothing")
    p.add_argument("--from-file", help="use this local draft as the message; refused if `check` would flag it")
    p = sub.add_parser("reply")
    p.add_argument("--to", required=True)
    p.add_argument("--re", required=True, help="incoming item name, e.g. setup-2026-11-14 or note-2026-11-14.md")
    p.add_argument("--kind", choices=REPLY_KINDS, help="default reply")
    p.add_argument("--suffix", help="any other follow-up word instead of --kind, e.g. correction or addendum")
    p.add_argument("--body-file", help="local draft of the answer; the reply is written only if `check` passes")
    p = sub.add_parser("close", help="write <topic>-<date>-done.md: the thread is finished")
    p.add_argument("--to", required=True)
    p.add_argument("--re", required=True, help="any item of the thread, in either folder")
    p.add_argument("--body-file", help="local draft of a closing note; written only if `check` passes")
    p = sub.add_parser("lint", help="read-only: names, root entries, big files, unknown hosts; exit 1 on findings")
    p.add_argument("--pair", help="only this <from>-to-<to> folder")
    p.add_argument("--mine", action="store_true", help="only this host's outgoing folders")
    p.add_argument("--max-mb", type=float, default=25.0, help="flag files larger than this (default 25)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("archive", help="move own done threads to private/archive/cross-pc (dry run unless --apply)")
    p.add_argument("--to", help="only threads with this peer")
    p.add_argument("--thread", help="only this thread, e.g. setup-2026-11-14")
    p.add_argument("--older-than", type=float, help="also finished threads with no message for N days")
    p.add_argument("--include-waiting", action="store_true",
                   help="with --older-than: also threads waiting on the peer (an open request)")
    p.add_argument("--apply", action="store_true", help="really move; prints the manifest and undo info")
    p = sub.add_parser("watch")
    p.add_argument("--once", action="store_true", help="exit after the first batch of events")
    p.add_argument("--all", action="store_true", help="also watch your outgoing folders")
    p.add_argument("--interval", type=float, default=20.0)
    p = sub.add_parser("check")
    p.add_argument("paths", nargs="+")
    p = sub.add_parser("manifest", help="print SHA256SUMS for a skill copy (writes nothing)")
    p.add_argument("dir", nargs="?", help="skill directory, normally an installed copy (default: this one)")
    sub.add_parser("verify", help="compare running and installed copies with the share's newest v<N>/SHA256SUMS")
    args = ap.parse_args(argv)
    return {"whoami": cmd_whoami, "status": cmd_status, "new": cmd_new, "reply": cmd_reply,
            "close": cmd_close, "lint": cmd_lint, "archive": cmd_archive, "watch": cmd_watch, "check": cmd_check, "manifest": cmd_manifest, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
