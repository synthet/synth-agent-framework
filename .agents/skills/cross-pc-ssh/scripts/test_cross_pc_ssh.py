"""Tests for cross_pc_ssh.py pure logic. No network and no ssh; remote scripts are only built, not run."""
from __future__ import annotations

import base64
import importlib.util
import os
import shutil
import subprocess
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("cross_pc_ssh", Path(__file__).with_name("cross_pc_ssh.py"))
cps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cps)

LOCAL = {"name": "widget", "slug": "github.com/acme/widget", "roots": ["a" * 40]}


def cand(path, name="widget", slug: str | None = "github.com/acme/widget", roots=("a" * 40,), worktree=False):
    return {"path": path, "name": name, "slug": slug, "roots": list(roots), "worktree": worktree}


class RepoSlug(unittest.TestCase):
    def test_https_and_scp_forms_agree(self):
        forms = ["https://github.com/Acme/Widget.git", "https://github.com/acme/widget",
                 "git@github.com:acme/widget.git", "ssh://git@github.com/acme/widget.git",
                 "https://user@github.com/acme/widget/"]
        self.assertEqual({cps.repo_slug(f) for f in forms}, {"github.com/acme/widget"})

    def test_unknown_or_empty(self):
        self.assertIsNone(cps.repo_slug(None))
        self.assertIsNone(cps.repo_slug(""))
        self.assertIsNone(cps.repo_slug("not a url"))


class SafeRel(unittest.TestCase):
    def test_accepts_relative(self):
        self.assertEqual(cps.safe_rel("docs/a b/readme.md"), "docs\\a b\\readme.md")
        self.assertEqual(cps.safe_rel("./src//x.py"), "src\\x.py")
        self.assertEqual(cps.safe_rel(""), "")
        self.assertEqual(cps.safe_rel("."), "")

    def test_rejects_escapes(self):
        for bad in ["..", "../x", "docs/../../x", "C:\\Windows", "c:x", "\\\\server\\share", "/etc",
                    "\\x", "file.txt:stream", "*.md", "a\x00b", "a|b"]:
            with self.subTest(bad=bad), self.assertRaises(cps.UsageError):
                cps.safe_rel(bad)

    def test_rejects_names_windows_would_rewrite(self):
        for bad in [".env.", "secrets.json.", "SECRETS.JSON...", ".git.\\config", "SECRET~1.JSO",
                    "docs\\PROGRA~1\\x", "a\\b \\c", "..."]:
            with self.subTest(bad=bad), self.assertRaises(cps.UsageError):
                cps.safe_rel(bad)


class Readable(unittest.TestCase):
    def test_secret_looking_files_refused(self):
        for bad in [".env", ".env.local", "config\\secrets.json", ".git\\config", "x\\.ssh\\known_hosts",
                    "id_ed25519", "deploy.PEM", ".npmrc", ".claude\\settings.local.json", ".cursor\\mcp.json"]:
            with self.subTest(bad=bad), self.assertRaises(cps.UsageError):
                cps.check_readable(bad)

    def test_outer_spaces_are_stripped_before_the_check(self):
        with self.assertRaises(cps.UsageError):
            cps.check_readable(cps.safe_rel(".env "))

    def test_ordinary_files_allowed(self):
        for ok in ["README.md", ".env.example", "docs\\secret-handling.md", "src\\keys.py", ".gitignore"]:
            with self.subTest(ok=ok):
                cps.check_readable(ok)


class GitArgs(unittest.TestCase):
    def test_read_only_allowed(self):
        self.assertEqual(cps.check_git_args(["log", "--oneline", "-5"]), ["log", "--oneline", "-5"])

    def test_writes_refused(self):
        for bad in [[], ["push"], ["checkout", "main"], ["reset", "--hard"], ["config", "x", "y"],
                    ["diff", "--output=x.patch"], ["grep", "-O", "foo"], ["log", "-c", "x=y"]]:
            with self.subTest(bad=bad), self.assertRaises(cps.UsageError):
                cps.check_git_args(bad)

    def test_abbreviated_and_clustered_options_refused(self):
        for bad in [["diff", "--outp=x"], ["grep", "--open=cmd", "foo"], ["grep", "--open-files-in-pager"],
                    ["grep", "-nOcmd", "foo"], ["grep", "-nf", "patterns.txt"], ["grep", "--fi=p.txt", "x"],
                    ["diff", "-Oorder.txt"], ["diff", "--orderf=o.txt"], ["log", "--ext-d"]]:
            with self.subTest(bad=bad), self.assertRaises(cps.UsageError):
                cps.check_git_args(bad)

    def test_reads_outside_checkout_refused(self):
        for bad in [["diff", "--no-index", "a", "b"], ["diff", "--no-i", "a", "b"], ["blame", "--contents", "x", "f"],
                    ["blame", "-S", "revs", "f"], ["ls-files", "-X", "ex.txt"], ["ls-files", "--exclude-from=e"],
                    ["diff", "../outside.txt", "x"], ["diff", "C:\\Windows\\win.ini", "x"], ["log", "--", "/etc"],
                    ["show", "HEAD:../x"], ["diff", "C:x", "y"], ["diff", "\\\\server\\share\\f", "x"]]:
            with self.subTest(bad=bad), self.assertRaises(cps.UsageError):
                cps.check_git_args(bad)

    def test_revision_syntax_still_allowed(self):
        for ok in [["log", "HEAD~2..main", "--", "docs/x.md"], ["show", "HEAD:docs/readme.md"],
                   ["log", ":/fix typo"], ["diff", "main...feature", "--stat"], ["log", "-S", "needle"],
                   ["log", "--no-ext-diff", "-p"], ["ls-files", "-o", "--exclude-standard"]]:
            with self.subTest(ok=ok):
                self.assertEqual(cps.check_git_args(ok), ok)


class Choose(unittest.TestCase):
    def test_different_drive_and_folder_with_same_identity(self):
        got = cps.choose(LOCAL, [cand("E:\\src\\other", name="other", slug=None), cand("E:\\src\\renamed", name="renamed")])
        self.assertEqual(got["path"], "E:\\src\\renamed")

    def test_needs_shared_root_commit(self):
        with self.assertRaises(cps.UsageError):
            cps.choose(LOCAL, [cand("E:\\src\\widget", roots=("b" * 40,))])

    def test_origin_beats_folder_name_and_main_beats_worktree(self):
        got = cps.choose(LOCAL, [cand("E:\\src\\widget", slug="github.com/fork/widget"),
                                 cand("E:\\src\\new-name", name="new-name"),
                                 cand("E:\\src\\new-name.wt", name="new-name.wt", worktree=True)])
        self.assertEqual(got["path"], "E:\\src\\new-name")

    def test_tie_is_refused_and_path_settles_it(self):
        cs = [cand("E:\\src\\widget"), cand("F:\\code\\widget")]
        with self.assertRaises(cps.UsageError):
            cps.choose(LOCAL, cs)
        self.assertEqual(cps.choose(LOCAL, cs, path="F:\\code\\widget\\")["path"], "F:\\code\\widget")

    def test_path_must_still_match_identity(self):
        with self.assertRaises(cps.UsageError):
            cps.choose(LOCAL, [cand("E:\\src\\widget", roots=("b" * 40,))], path="E:\\src\\widget")


class Cache(unittest.TestCase):
    LOCAL = {"slug": "github.com/acme/widget", "origin": "https://github.com/acme/widget.git",
             "roots": ["a" * 40], "path": "E:/src/widget"}

    def test_remember_recall_forget(self):
        cache = {}
        cps.remember(cache, self.LOCAL, "laptop", "F:\\code\\widget", "2026-01-01T00:00:00+00:00")
        entry = cache["github.com/acme/widget"]
        self.assertEqual((entry["github"], entry["local"], entry["root"]),
                         ("https://github.com/acme/widget.git", "E:/src/widget", "a" * 40))
        self.assertEqual(cps.recall(cache, self.LOCAL, "laptop"), "F:\\code\\widget")
        self.assertIsNone(cps.recall(cache, self.LOCAL, "desk"))
        cps.forget(cache, self.LOCAL, "laptop")
        self.assertIsNone(cps.recall(cache, self.LOCAL, "laptop"))

    def test_entry_with_other_root_is_ignored(self):
        cache = {}
        cps.remember(cache, self.LOCAL, "laptop", "F:\\code\\widget", "t")
        other = dict(self.LOCAL, roots=["b" * 40])
        self.assertIsNone(cps.recall(cache, other, "laptop"))

    def test_key_falls_back_to_root_without_origin(self):
        self.assertEqual(cps.cache_key(dict(self.LOCAL, slug=None)), "root:" + "a" * 40)

    def test_file_roundtrip_and_damaged_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "locations.json"
            self.assertEqual(cps.load_cache(path), {})
            cache = {}
            cps.remember(cache, self.LOCAL, "laptop", "F:\\code\\widget", "t")
            cps.save_cache(path, cache)
            self.assertEqual(cps.recall(cps.load_cache(path), self.LOCAL, "laptop"), "F:\\code\\widget")
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(cps.load_cache(path), {})


TRICKY = ["it's", "a\u2018b\u2019c\u201ad\u201be", "$(Write-Host x)", "`n`$x", "\u2192 \u00e9 \u041f\u0440\u0438\u0432\u0435\u0442", ""]


class RemoteScripts(unittest.TestCase):
    def test_quote_embeds_base64_only(self):
        for s in TRICKY:
            with self.subTest(s=s):
                q = cps.ps_quote(s)
                b64 = q.split("'")[1]
                self.assertEqual(q, f"([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))")
                self.assertEqual(base64.b64decode(b64).decode("utf-8"), s)

    def test_encode(self):
        text = base64.b64decode(cps.encode("Get-Date")).decode("utf-16-le")
        self.assertEqual(text, "Get-Date")

    def test_command_line_is_fixed_bootstrap(self):
        argv = cps.ssh_argv("laptop")
        self.assertEqual(argv[:-1], ["ssh", *cps.SSH_OPTS, "laptop"])
        self.assertLess(len(argv[-1]), cps.MAX_ENCODED)
        g = cps.guard("E:\\src\\o'neil", ["a" * 40, "b" * 40])
        self.assertIn(cps.ps_quote("E:\\src\\o'neil"), g)
        self.assertIn(f"exit {cps.EXIT_IDENTITY}", g)

    def test_agent_read_only_by_default(self):
        body = cps.agent_body("claude", False, "do not edit")
        self.assertIn(" ".join(cps.CLAUDE_READ_ONLY), body)
        self.assertIn("--tools Read,Grep,Glob", body)
        self.assertIn("--strict-mcp-config", body)
        self.assertNotIn("acceptEdits", body)
        self.assertNotIn("do not edit", body)  # the prompt is embedded as base64
        self.assertIn("--permission-mode acceptEdits", cps.agent_body("claude", True, "x"))
        self.assertIn("-s read-only -", cps.agent_body("codex", False, "x"))
        self.assertIn("-s workspace-write -", cps.agent_body("codex", True, "x"))
        with self.assertRaises(cps.UsageError):
            cps.agent_body("other", False, "x")

    def test_too_long_refused(self):
        with self.assertRaises(cps.UsageError):
            cps.encode("x" * 6000)

    def test_peer_names(self):
        self.assertEqual(cps.resolve_peer("laptop", {}), "laptop")
        self.assertEqual(cps.resolve_peer(None, {"default_peer": "me@desk"}), "me@desk")
        for bad in ["-oProxyCommand=x", "a b", "a;b", ""]:
            with self.subTest(bad=bad), self.assertRaises(cps.UsageError):
                cps.resolve_peer(bad or None, {})


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class LocalPowerShell(unittest.TestCase):
    """Runs the remote scripts in local Windows PowerShell through the same stdin bootstrap ssh uses."""

    def run_ps(self, script: str) -> tuple[int, str]:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", cps.encode(cps.BOOT)],
                           input=base64.b64encode((cps.PRELUDE + script).encode("utf-8")), capture_output=True,
                           timeout=60)
        return r.returncode, r.stdout.decode("utf-8", "replace")

    def test_strings_round_trip(self):
        for s in TRICKY:
            with self.subTest(s=s):
                code, out = self.run_ps("[Console]::Out.Write(" + cps.ps_quote(s) + ")")
                self.assertEqual((code, out), (0, s))

    def test_unicode_quotes_in_a_path_do_not_run_code(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for q in "\u2018\u2019\u201a\u201b'":
                rel = cps.safe_rel("a" + q + "+(Write-Host INJECTED)+" + q)
                with self.subTest(q=hex(ord(q))):
                    code, out = self.run_ps("$c=" + cps.ps_quote(d) + ";" + cps.inside(rel) + "'reached'")
                    self.assertNotIn("INJECTED", out)
                    self.assertNotEqual(code, 0)  # the odd name simply does not exist

    def test_exit_code_survives_bootstrap(self):
        self.assertEqual(self.run_ps("exit 3")[0], 3)


if __name__ == "__main__":
    unittest.main()
