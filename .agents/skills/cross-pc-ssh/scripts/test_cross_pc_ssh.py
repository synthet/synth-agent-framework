"""Tests for cross_pc_ssh.py. No network: remote scripts are checked as text, and the PowerShell path guard
runs locally against a throwaway folder when PowerShell is available (Windows)."""
from __future__ import annotations

import base64
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("cross_pc_ssh", Path(__file__).with_name("cross_pc_ssh.py"))
xpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xpc)

IDENT = {"roots": ["aaa111"], "origin": "github.com/acme/widget"}
same = lambda o: o  # noqa: E731 - resolver that never calls gh


def cand(path, kind="repo", roots=("aaa111",), origin="git@github.com:acme/widget.git", shallow=False):
    return {"path": path, "kind": kind, "roots": list(roots), "origin": origin, "shallow": shallow}


# ---- origin normalisation

@pytest.mark.parametrize("url", [
    "git@github.com:Acme/Widget.git", "https://github.com/acme/widget", "https://github.com/acme/widget.git",
    "ssh://git@github.com/acme/widget.git", "https://user@github.com/acme/widget/"])
def test_norm_origin_shapes(url):
    assert xpc.norm_origin(url) == "github.com/acme/widget"


def test_norm_origin_unknown():
    assert xpc.norm_origin("C:/local/bare.git") is None
    assert xpc.norm_origin(None) is None


# ---- checkout matching

def test_choose_single_match_under_a_different_root_and_name():
    found = [cand("C:\\Repos\\widget-renamed"), cand("C:\\Repos\\other", roots=("zzz",))]
    assert xpc.choose(IDENT, found, resolve=same)["path"] == "C:\\Repos\\widget-renamed"


def test_choose_skips_linked_worktrees_links_and_shallow():
    found = [cand("C:\\P\\widget"), cand("C:\\P\\widget-wt", kind="worktree"),
             cand("C:\\P\\widget-junction", kind="link"), cand("C:\\P\\widget-shallow", shallow=True)]
    assert xpc.choose(IDENT, found, resolve=same)["path"] == "C:\\P\\widget"


def test_choose_dedupes_same_path():
    found = [cand("C:\\P\\widget"), cand("c:\\p\\WIDGET\\")]
    assert xpc.choose(IDENT, found, resolve=same)["path"] == "C:\\P\\widget"


def test_choose_fork_sharing_history_decided_by_origin():
    found = [cand("C:\\P\\upstream", origin="https://github.com/other/upstream.git"), cand("C:\\P\\widget")]
    assert xpc.choose(IDENT, found, resolve=same)["path"] == "C:\\P\\widget"


def test_choose_origin_through_rename_redirect():
    found = [cand("C:\\P\\a", origin="https://github.com/other/upstream.git"),
             cand("C:\\P\\b", origin="git@github.com:acme/old-name.git")]
    redirects = {"github.com/acme/old-name": "github.com/acme/widget"}
    assert xpc.choose(IDENT, found, resolve=lambda o: redirects.get(o, o))["path"] == "C:\\P\\b"


def test_choose_ambiguous_refuses_with_list():
    found = [cand("C:\\P\\one"), cand("C:\\P\\two")]
    with pytest.raises(xpc.XpcError, match="several checkouts"):
        xpc.choose(IDENT, found, resolve=same)


def test_choose_none_mentions_skipped_worktree():
    with pytest.raises(xpc.XpcError, match="skipped worktree"):
        xpc.choose(IDENT, [cand("C:\\P\\widget-wt", kind="worktree")], resolve=same)


def test_choose_nothing_found():
    with pytest.raises(xpc.XpcError, match="no checkout"):
        xpc.choose(IDENT, [], resolve=same)


# ---- path rules

@pytest.mark.parametrize("rel,expected", [
    (None, "."), ("", "."), (".", "."), ("docs/a.md", "docs\\a.md"), ("docs\\sub\\..\\a.md", "docs\\a.md"),
    ("./src/x.py", "src\\x.py")])
def test_check_rel_accepts(rel, expected):
    assert xpc.check_rel(rel) == expected


@pytest.mark.parametrize("rel", [
    "..", "..\\other", "docs/../../x", "C:\\Windows", "C:x", "\\abs", "/abs", "\\\\server\\share\\f",
    "//server/share", "~/x", "file.txt:stream", "a\0b"])
def test_check_rel_refuses(rel):
    with pytest.raises(xpc.XpcError):
        xpc.check_rel(rel)


@pytest.mark.parametrize("rel", [
    ".env", "config\\.env.local", "secrets.json", "keys\\server.pem", "id_ed25519", "id_rsa.pub",
    ".npmrc", ".claude\\settings.local.json", ".cursor\\mcp.json", ".git\\config", "x\\.ssh\\known_hosts"])
def test_check_readable_refuses_secrets(rel):
    with pytest.raises(xpc.XpcError):
        xpc.check_readable(rel)


@pytest.mark.parametrize("rel", [".env.example", "README.md", "src\\keyboard.py"])
def test_check_readable_allows(rel):
    xpc.check_readable(rel)


# ---- remote script construction

def test_ps_quote_escapes_single_quotes():
    assert xpc.ps_quote("it's") == "'it''s'"


def test_parse_marker_takes_last_marker_line():
    out = "noise\n@@XPC@@{\"a\": 1}\nmore\n@@XPC@@{\"b\": 2}\n"
    assert xpc.parse_marker(out) == {"b": 2}
    assert xpc.parse_marker("nothing") is None


def test_agent_args_read_only_by_default():
    exe, args = xpc.agent_args("claude", False, None, "C:\\P\\w")
    assert exe == "claude" and "dontAsk" in args and args[-len(xpc.CLAUDE_READ_TOOLS):] == xpc.CLAUDE_READ_TOOLS
    exe, args = xpc.agent_args("codex", False, None, "C:\\P\\w")
    assert args[args.index("-s") + 1] == "read-only"


def test_agent_args_write_needs_flag():
    assert "acceptEdits" in xpc.agent_args("claude", True, None, "p")[1]
    assert "workspace-write" in xpc.agent_args("codex", True, None, "p")[1]


def test_agent_prompt_travels_base64_not_on_the_command_line():
    prompt = "line one\nit's \"quoted\" & <odd>"
    body = xpc.body_agent("claude", ["-p"], prompt, 30)
    assert prompt not in body and "line one" not in body
    assert base64.b64encode(prompt.encode()).decode() in body


def test_locate_uses_cache_and_peer_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(xpc, "CACHE", tmp_path / "loc.json")
    cfg = {"hosts": {"beta": {"ssh": "beta", "roots": ["C:\\P"]}}, "self": "alpha", "_path": "x"}
    calls = []
    def fake(h, body, timeout=120):
        calls.append(body)
        return {"candidates": cand("C:\\P\\widget")}  # one-item array collapsed, as ConvertTo-Json does
    monkeypatch.setattr(xpc, "run_remote", fake)
    monkeypatch.setattr(xpc, "canonical", same)
    assert xpc.locate(cfg, "beta", IDENT) == "C:\\P\\widget"
    assert xpc.locate(cfg, "beta", IDENT) == "C:\\P\\widget"
    assert len(calls) == 1
    with pytest.raises(xpc.XpcError, match="this PC"):
        xpc.locate({**cfg, "hosts": {**cfg["hosts"], "alpha": cfg["hosts"]["beta"]}}, "alpha", IDENT)


# ---- the remote guard, run locally

POWERSHELL = shutil.which("powershell") if sys.platform == "win32" else None


def run_ps(body: str) -> dict:
    assert POWERSHELL
    script = xpc.wrap(body)
    boot = base64.b64encode(xpc.BOOT.encode("utf-16-le")).decode()
    r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", boot],
                       input=base64.b64encode(script.encode()).decode(), capture_output=True, text=True, timeout=60)
    return xpc.parse_marker(r.stdout)


@pytest.mark.skipif(not POWERSHELL, reason="needs Windows PowerShell")
def test_remote_confine_refuses_junction_and_escape(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.md").write_text("hello", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "b.md").write_text("secret", encoding="utf-8")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "jump"), str(outside)], check=True, capture_output=True)
    p = "$p = " + xpc.ps_quote(str(root)) + "\n"
    assert run_ps(p + xpc.body_cat("docs\\a.md", 1000))["text"] == "hello"
    assert "junction" in run_ps(p + xpc.body_cat("jump\\b.md", 1000))["error"]
    assert "outside" in run_ps(p + "$x = Confine $p '..\\outside\\b.md'\nEmit @{ x = $x }")["error"]
