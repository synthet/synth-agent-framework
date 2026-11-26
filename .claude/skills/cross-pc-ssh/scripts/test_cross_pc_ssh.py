"""Tests for cross_pc_ssh.py pure logic. No network and no ssh; remote scripts are only built, not run."""
from __future__ import annotations

import base64
import importlib.util
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


class RemoteScripts(unittest.TestCase):
    def test_quote_and_encode(self):
        self.assertEqual(cps.ps_quote("it's"), "'it''s'")
        text = base64.b64decode(cps.encode("Get-Date")).decode("utf-16-le")
        self.assertEqual(text, "Get-Date")

    def test_guard_and_locate_fit_cmd_line(self):
        g = cps.guard("E:\\src\\o'neil", ["a" * 40, "b" * 40])
        self.assertIn("'E:\\src\\o''neil'", g)
        self.assertIn(f"exit {cps.EXIT_IDENTITY}", g)
        cps.encode(g + cps.inside("docs\\deep\\file.md") + "Get-ChildItem")
        cps.encode(cps.LOCATE % "'E:\\src'")

    def test_too_long_refused(self):
        with self.assertRaises(cps.UsageError):
            cps.encode("x" * 6000)

    def test_peer_names(self):
        self.assertEqual(cps.resolve_peer("laptop", {}), "laptop")
        self.assertEqual(cps.resolve_peer(None, {"default_peer": "me@desk"}), "me@desk")
        for bad in ["-oProxyCommand=x", "a b", "a;b", ""]:
            with self.subTest(bad=bad), self.assertRaises(cps.UsageError):
                cps.resolve_peer(bad or None, {})


if __name__ == "__main__":
    unittest.main()
