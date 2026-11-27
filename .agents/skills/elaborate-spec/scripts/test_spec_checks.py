"""Tests for spec_checks.py on a tiny fixture (run: python -m pytest <skill>/scripts -q)."""
import json
import subprocess
import sys
from pathlib import Path

H = Path(__file__).with_name("spec_checks.py")
BT = chr(96)


def run(tmp, *args):
    r = subprocess.run([sys.executable, "-I", str(H), *args], cwd=tmp, capture_output=True, text=True)
    return r.returncode, json.loads(r.stdout)


def make(tmp: Path, deps_b="AA-01.1"):
    d = tmp / "specs" / "X"
    (d / "items").mkdir(parents=True)
    (d / "spec.md").write_text("| [AA-01](items/AA-01.md) |\n| [BB-02](items/BB-02.md) |\n", encoding="utf-8")
    (d / "items" / "AA-01.md").write_text("- **AA-01/AC-1**: The tool shall\n  do one thing.\n", encoding="utf-8")
    (d / "items" / "BB-02.md").write_text(f"- **BB-02/AC-1**: It shall work. See {BT}specs/X/spec.md{BT} and {BT}lib/new.py{BT} (new).\n", encoding="utf-8")
    (d / "tasks.md").write_text(
        "| ID | Task | Owner role | Files / areas | Depends on | ACs | Verification |\n|---|---|---|---|---|---|---|\n"
        "| AA-01.1 | First | dev | a.py | - | AA-01/AC-1 | pytest |\n"
        f"| BB-02.1 | Second | dev | b.py | {deps_b} | BB-02/AC-1 | pytest |\n"
        "| BB-02.2 | Covered by tkt-1 - no new ticket | existing | - | - | - | - |\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp)
    return "specs/X"


def test_coverage_graph_paths_and_import(tmp_path):
    rel = make(tmp_path)
    assert run(tmp_path, "coverage", rel)[0] == 0
    code, g = run(tmp_path, "graph", rel)
    assert code == 0 and g["edges"] == 1 and not g["cycles"]
    code, p = run(tmp_path, "paths", rel)
    assert code == 0 and p["marked_new"] == 1
    code, imp = run(tmp_path, "import-table", rel)
    assert imp["tasks"] == 2 and imp["skipped_covered"] == 1
    text = (tmp_path / rel / "tasks-import.md").read_text(encoding="utf-8")
    assert "| T-2 | [BB-02.1] Second | dev | b.py | T-1 | AC-2 |" in text
    assert "[AA-01/AC-1] The tool shall do one thing." in text


def test_cycle_detected(tmp_path):
    rel = make(tmp_path, deps_b="AA-01.1")
    t = tmp_path / rel / "tasks.md"
    t.write_text(t.read_text(encoding="utf-8").replace("| AA-01.1 | First | dev | a.py | - |", "| AA-01.1 | First | dev | a.py | BB-02 |"), encoding="utf-8")
    code, g = run(tmp_path, "graph", rel)
    assert code == 1 and g["cycles"]
