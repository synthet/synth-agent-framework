#!/usr/bin/env python3
"""Mechanical checks for an elaborated spec folder (spec.md, tasks.md, items/<ID>.md).

Subcommands:
  paths         every repo path cited in code spans exists (on disk, or <branch>:<path> via git)
  coverage      every item has a file and tasks; every <ID>/AC-n is covered by a task row
  graph         dependency graph of task rows: unknown refs and cycles
  import-table  write a T-n / global AC-n table that agent-flow's framework importer accepts

Task rows are markdown table rows whose first cell is <ITEM>.<n> (e.g. AGT-02.3), with columns
ID | Task | Owner role | Files / areas | Depends on | ACs | Verification. A dependency on a bare
item ID means every task of that item. A row whose Task starts with "Covered by" is existing
work: it is checked but never imported. Stdlib only. Run from inside the target git repository.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

BT = chr(96)  # backtick, kept out of the source so the file survives template tooling
CODE_SPAN = re.compile(BT + "([^" + BT + r"\s]+)" + BT)
PATH_EXT = re.compile(r"\.(rs|md|toml|ps1|sh|py|ya?ml|json|mjs|js|ts|tsx|iss|desktop|txt|hbs|svg|png|lock|sql|wgsl|go|cs|kt|swift)$")
NEW_MARK = re.compile(r"\(new\b|\bnew file|\bnew module|\bnew crate|proposed|planned", re.I)
BRANCH_PATH = re.compile(r"^((?:origin/)?[\w.\-/]+):((?:[\w.\-]+/)+[\w.\-]*)$")
DASHES = ("", "-", chr(0x2014))


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8")


def split_row(line: str) -> list[str]:
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def md_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.md"))


def task_rows(tasks_md: Path, id_re: str) -> list[dict]:
    key = re.compile(rf"^({id_re})\.\d+$")
    rows = []
    for line in tasks_md.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        c = split_row(line)
        if len(c) < 7 or not key.match(c[0]):
            continue
        rows.append({"id": c[0], "item": c[0].rsplit(".", 1)[0], "task": c[1], "role": c[2], "files": c[3],
                     "deps": c[4], "acs": c[5], "verify": c[6], "covered": c[1].lower().startswith("covered by")})
    return rows


def item_acs(items_dir: Path) -> dict[str, dict[str, str]]:
    """{item: {"<item>/AC-n": text}}; wrapped AC lines are joined into one line."""
    out: dict[str, dict[str, str]] = {}
    for f in sorted(items_dir.glob("*.md")):
        item, lines, acs = f.stem, f.read_text(encoding="utf-8").splitlines(), {}
        pat = re.compile(rf"\*\*({re.escape(item)}/AC-\d+)\*\*:?\s*(.+)")
        for i, line in enumerate(lines):
            m = pat.search(line)
            if not m or m.group(1) in acs:
                continue
            text = m.group(2).strip()
            for nxt in lines[i + 1:]:
                if not nxt.strip() or re.match(r"^\s*([-*]\s|#|\|)", nxt):
                    break
                text += " " + nxt.strip()
            acs[m.group(1)] = text
        out[item] = acs
    return out


def cmd_paths(args) -> int:
    root = Path(args.dir)
    tracked = git("ls-files").stdout.splitlines()
    seen: dict[str, list] = {}
    for f in md_files(root):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for raw in CODE_SPAN.findall(line):
                p = re.sub(r":\d+(-\d+)?$", "", raw).rstrip(",;)")
                if re.match(r"^(https?:|\$|--|~|%|[A-Za-z]:)", p) or any(ch in p for ch in "*<{") or "://" in p:
                    continue
                if "/" not in p or not (PATH_EXT.search(p) or p.endswith("/")):
                    continue
                is_new = bool(NEW_MARK.search(line))
                if p in seen:
                    seen[p][1] = seen[p][1] and is_new
                else:
                    seen[p] = [f"{f.relative_to(root)}:{n}", is_new]
    missing, new, expansions = [], 0, {}
    for p, (where, is_new) in sorted(seen.items()):
        bm = BRANCH_PATH.match(p)
        if bm and not Path(p).exists():
            ok = git("cat-file", "-e", f"{bm.group(1)}:{bm.group(2).rstrip('/')}").returncode == 0
        else:
            ok = Path(p.rstrip("/")).exists()
        if ok:
            continue
        if is_new:
            new += 1
            continue
        if p.endswith("/"):
            hits = sorted({t[: t.index("/" + p) + len(p) + 1] for t in tracked if ("/" + p) in t})
        else:
            hits = sorted(t for t in tracked if t.endswith("/" + p))
        if len(hits) == 1:
            expansions[p] = hits[0]
        hint = f"  -> {hits[0]}" if len(hits) == 1 else (f"  ({len(hits)} candidates)" if hits else "")
        missing.append(f"{p}  @{where}{hint}")
    report = {"checked": len(seen), "missing": missing, "marked_new": new, "unique_expansions": expansions}
    if args.fix_short and expansions:
        for f in md_files(root):
            text = f.read_text(encoding="utf-8")
            fixed = text
            for short, full in expansions.items():
                fixed = re.sub(BT + re.escape(short) + r"(:\d+(?:-\d+)?)?" + BT,
                               lambda m, full=full: BT + full + (m.group(1) or "") + BT, fixed)
            if fixed != text:
                f.write_text(fixed, encoding="utf-8")
        report["fixed"] = len(expansions)
    print(json.dumps(report, indent=2))
    unresolved = [m for m in missing if not (args.fix_short and m.split("  @")[0] in expansions)]
    return 1 if unresolved else 0


def expand(rows: list[dict], id_re: str):
    ids = {r["id"] for r in rows}
    live = [r for r in rows if not r["covered"]]
    edges, unknown, external = [], [], []
    for r in rows:
        for d in (x.strip() for x in re.split(r"[,;]", r["deps"])):
            if d in DASHES:
                continue
            if d in ids:
                edges.append((r["id"], d))
            elif re.fullmatch(id_re, d):
                if not any(t["item"] == d for t in rows):
                    unknown.append(f"{r['id']} -> {d}")
                edges += [(r["id"], t["id"]) for t in live if t["item"] == d]
            elif re.fullmatch(rf"({id_re})\.\d+", d):
                unknown.append(f"{r['id']} -> {d}")
            else:
                external.append(f"{r['id']} -> {d}")
    return edges, unknown, external


def find_cycles(nodes, edges) -> list[str]:
    adj: dict[str, list[str]] = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
    state: dict[str, int] = {}
    cycles: list[str] = []
    sys.setrecursionlimit(10000)

    def dfs(n, stack):
        state[n] = 1
        stack.append(n)
        for m in adj.get(n, []):
            if state.get(m) == 1:
                cycles.append(" > ".join(stack[stack.index(m):] + [m]))
            elif not state.get(m):
                dfs(m, stack)
        stack.pop()
        state[n] = 2

    for n in nodes:
        if not state.get(n):
            dfs(n, [])
    return cycles


def cmd_graph(args) -> int:
    rows = task_rows(Path(args.dir) / "tasks.md", args.id_re)
    edges, unknown, external = expand(rows, args.id_re)
    cycles = find_cycles([r["id"] for r in rows], edges)
    print(json.dumps({"tasks": len(rows), "edges": len(edges), "unknown": unknown,
                      "external_refs": external, "cycles": cycles[:20]}, indent=2))
    return 1 if unknown or cycles else 0


def cmd_coverage(args) -> int:
    root = Path(args.dir)
    rows = task_rows(root / "tasks.md", args.id_re)
    acs = item_acs(root / "items")
    spec_ids = sorted(set(re.findall(rf"\[({args.id_re})\]\(items/", (root / "spec.md").read_text(encoding="utf-8"))))
    covered = {m for r in rows for m in re.findall(rf"(?:{args.id_re})/AC-\d+", r["acs"])}
    report = {
        "spec_items": len(spec_ids), "item_files": len(acs),
        "missing_files": [i for i in spec_ids if i not in acs],
        "files_not_in_spec": [i for i in acs if i not in spec_ids],
        "items_without_tasks": [i for i in spec_ids if not any(r["item"] == i for r in rows)],
        "items_without_acs": [i for i, a in acs.items() if not a],
        "acs": sum(len(a) for a in acs.values()),
        "uncovered_acs": [a for item in acs.values() for a in item if a not in covered],
        "task_rows": len(rows), "covered_rows": sum(r["covered"] for r in rows),
    }
    print(json.dumps(report, indent=2))
    keys = ("missing_files", "files_not_in_spec", "items_without_tasks", "uncovered_acs")
    return 1 if any(report[k] for k in keys) else 0


def cmd_import_table(args) -> int:
    root = Path(args.dir)
    rows = task_rows(root / "tasks.md", args.id_re)
    acs = {k: v for item in item_acs(root / "items").values() for k, v in item.items()}
    live = [r for r in rows if not r["covered"]]
    tn = {r["id"]: f"T-{i + 1}" for i, r in enumerate(live)}
    edges, _, _ = expand(rows, args.id_re)
    deps: dict[str, list[str]] = {}
    for a, b in edges:
        if a in tn and b in tn and tn[b] not in deps.setdefault(a, []):
            deps[a].append(tn[b])
    gac: dict[str, str] = {}
    ac_lines, out_rows = [], []
    for r in live:
        ids = []
        for a in re.findall(rf"(?:{args.id_re})/AC-\d+", r["acs"]):
            if a not in gac:
                gac[a] = f"AC-{len(gac) + 1}"
                ac_lines.append(f"- **{gac[a]}**: [{a}] {acs.get(a, '(see item file)')}")
            if gac[a] not in ids:
                ids.append(gac[a])
        ext = [d.strip() for d in r["deps"].split(",")
               if d.strip() not in DASHES and not re.fullmatch(rf"({args.id_re})(\.\d+)?", d.strip())]
        title = f"[{r['id']}] {r['task']}" + (f" (also needs: {', '.join(ext)})" if ext else "")
        out_rows.append(f"| {tn[r['id']]} | {title.replace('|', chr(92) + '|')} | {r['role']} | {r['files']} | "
                        f"{', '.join(deps.get(r['id'], [])) or '-'} | {', '.join(ids)} | {r['verify']} |")
    text = "\n".join([
        f"# {args.title}", "",
        f"Generated by elaborate-spec {BT}spec_checks.py import-table{BT} from tasks.md for agent-flow's framework",
        f"importer, which needs {BT}T-n{BT} IDs and global {BT}AC-n{BT}. tasks.md and items/ stay the source of truth;",
        "rows marked \"Covered by\" are left out.",
        "", "## Acceptance criteria", "", *ac_lines, "", "## Task table", "",
        "| ID | Task | Owner role | Files / areas | Depends on | ACs | Verification |",
        "|----|------|------------|---------------|------------|-----|--------------|", *out_rows, ""])
    out = Path(args.out) if args.out else root / "tasks-import.md"
    out.write_text(text, encoding="utf-8")
    print(json.dumps({"out": str(out), "tasks": len(live), "acs": len(gac),
                      "edges": sum(len(v) for v in deps.values()), "skipped_covered": len(rows) - len(live)}, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id-re", default=r"[A-Z]{2,5}-\d{2,3}", help="regex for item IDs (default: AGT-01 style)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("paths", "coverage", "graph", "import-table"):
        sp = sub.add_parser(name)
        sp.add_argument("dir", help="spec folder holding spec.md, tasks.md and items/")
        if name == "paths":
            sp.add_argument("--fix-short", action="store_true", help="rewrite short paths that resolve to one tracked file")
        if name == "import-table":
            sp.add_argument("--out")
            sp.add_argument("--title", default="Import table")
    args = ap.parse_args(argv)
    return {"paths": cmd_paths, "coverage": cmd_coverage, "graph": cmd_graph, "import-table": cmd_import_table}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
