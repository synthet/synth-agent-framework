"""Tests for repo_inventory.py on synthetic git repositories in temp folders. No network, no ssh."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_spec = importlib.util.spec_from_file_location("repo_inventory", Path(__file__).with_name("repo_inventory.py"))
ri = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ri)

HAVE_GIT = shutil.which("git") is not None
_ENV_KEYS = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "CROSS_PC_SHARE", "CROSS_PC_HOST", "REPO_INVENTORY_CONFIG",
             "REPO_INVENTORY_ROOT", "GIT_DIR", "GIT_WORK_TREE")
_saved_env: dict = {}
_saved_attrs: dict = {}
_TMP = None


def setUpModule():
    """Hermetic git and no real share: global/system git config, env and the share candidates are replaced."""
    global _TMP
    _TMP = Path(tempfile.mkdtemp(prefix="repo-inv-env-"))
    gitconfig = _TMP / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n"
                         "[core]\n\tautocrlf = false\n[commit]\n\tgpgsign = false\n", encoding="utf-8")
    for k in _ENV_KEYS:
        _saved_env[k] = os.environ.get(k)
        os.environ.pop(k, None)
    os.environ["GIT_CONFIG_GLOBAL"] = str(gitconfig)
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["REPO_INVENTORY_CONFIG"] = str(_TMP / "no-config.json")
    for name, value in (("MAIL_CONFIG", _TMP / "no-mail-config.json"), ("CANDIDATES", ())):
        _saved_attrs[name] = getattr(ri, name)
        setattr(ri, name, value)


def tearDownModule():
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    for name, value in _saved_attrs.items():
        setattr(ri, name, value)
    shutil.rmtree(_TMP, ignore_errors=True)


def g(cwd, *args) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {r.stderr}")
    return r.stdout.strip()


def commit(repo: Path, name: str, text: str, msg: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    g(repo, "add", name)
    g(repo, "commit", "-q", "-m", msg)
    return g(repo, "rev-parse", "HEAD")


def run_main(argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = ri.main(argv)
        except SystemExit as e:  # argparse
            code = e.code
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- pure logic

class RemoteKeys(unittest.TestCase):
    def test_https_ssh_and_scp_forms_agree(self):
        forms = ["https://github.com/Acme/Widget.git", "https://github.com/acme/widget",
                 "git@github.com:acme/widget.git", "ssh://git@github.com/acme/widget.git",
                 "https://fakeuser:fakepass@github.com/acme/widget/", "http://github.com/ACME/widget.git/"]
        self.assertEqual({ri.parse_remote(f)[1] for f in forms}, {"acme/widget"})
        self.assertEqual(ri.parse_remote("git@github.com:acme/widget.git")[0], "github.com")

    def test_local_paths_use_last_two_segments(self):
        for f in ["/srv/git/acme/widget.git", "C:/git/acme/widget.git", "C:\\git\\acme\\widget.git",
                  "file:///srv/git/acme/widget.git", "../acme/widget.git"]:
            with self.subTest(f=f):
                self.assertEqual(ri.parse_remote(f), ("local", "acme/widget"))

    def test_other_hosts_keep_the_full_path(self):
        self.assertEqual(ri.parse_remote("https://gitlab.example.com/group/sub/proj.git"),
                         ("gitlab.example.com", "group/sub/proj"))

    def test_empty(self):
        self.assertEqual(ri.parse_remote(""), (None, None))
        self.assertEqual(ri.parse_remote(None), (None, None))


class Credentials(unittest.TestCase):
    def test_userinfo_is_stripped_from_http_urls(self):
        self.assertEqual(ri.strip_credentials("https://fakeuser:fakepass@github.com/acme/w.git"),
                         "https://github.com/acme/w.git")
        self.assertEqual(ri.strip_credentials("https://faketoken@github.com/acme/w.git?x=1"),
                         "https://github.com/acme/w.git")

    def test_ssh_user_kept_password_dropped(self):
        self.assertEqual(ri.strip_credentials("git@github.com:acme/w.git"), "git@github.com:acme/w.git")
        self.assertEqual(ri.strip_credentials("ssh://git:fakepass@host.example/acme/w.git"),
                         "ssh://git@host.example/acme/w.git")

    def test_error_text_is_scrubbed(self):
        text = ri.scrub("fatal: unable to access 'https://fakeuser:fakepass@github.com/acme/w/': 403")
        self.assertNotIn("fakepass", text)
        self.assertNotIn("fakeuser", text)


class Aliases(unittest.TestCase):
    def test_full_and_bare_names(self):
        aliases = ri.normalize_aliases({"Acme/Old-Name": "acme/new-name", "legacy": "modern"})
        self.assertEqual(ri.apply_alias("acme/old-name", aliases), "acme/new-name")
        self.assertEqual(ri.apply_alias("acme/legacy", aliases), "acme/modern")
        self.assertEqual(ri.apply_alias("acme/other", aliases), "acme/other")
        self.assertIsNone(ri.apply_alias(None, aliases))

    def test_alias_cycle_terminates(self):
        aliases = ri.normalize_aliases({"acme/a": "acme/b", "acme/b": "acme/a"})
        self.assertIn(ri.apply_alias("acme/a", aliases), {"acme/a", "acme/b"})


class StatusParse(unittest.TestCase):
    def test_porcelain_v2(self):
        raw = ("# branch.oid " + "a" * 40 + "\0# branch.head main\0# branch.upstream origin/main\0"
               "# branch.ab +2 -3\0"
               "1 M. N... 100644 100644 100644 " + "b" * 40 + " " + "b" * 40 + " staged.txt\0"
               "1 .M N... 100644 100644 100644 " + "b" * 40 + " " + "b" * 40 + " dir/un staged.txt\0"
               "2 R. N... 100644 100644 100644 " + "b" * 40 + " " + "b" * 40 + " R100 new.txt\0old.txt\0"
               "u UU N... 100644 100644 100644 100644 " + "c" * 40 + " " + "c" * 40 + " " + "c" * 40 + " both.txt\0"
               "? new folder/x.txt\0")
        st = ri.parse_status(raw, max_paths=10)
        self.assertEqual(st["branch"], "main")
        self.assertEqual(st["upstream"], "origin/main")
        self.assertEqual((st["ahead"], st["behind"]), (2, 3))
        self.assertEqual(st["dirty"], {"staged": 2, "unstaged": 1, "untracked": 1, "unmerged": 1})
        self.assertEqual(st["unmerged_paths"], ["both.txt"])
        self.assertIn("dir/un staged.txt", st["paths"])
        self.assertIn("new folder/x.txt", st["paths"])
        self.assertNotIn("old.txt", st["paths"])

    def test_detached_and_path_cap(self):
        raw = "# branch.oid " + "a" * 40 + "\0# branch.head (detached)\0? a\0? b\0? c\0"
        st = ri.parse_status(raw, max_paths=2)
        self.assertIsNone(st["branch"])
        self.assertTrue(st["detached"])
        self.assertEqual(len(st["paths"]), 2)
        self.assertTrue(st["paths_truncated"])


def entry(folder="widget", key="acme/widget", branch="main", sha="1" * 40, origin_sha="1" * 40,
          origin_date="2026-01-02T00:00:00+00:00", ahead=0, behind=0, dirty=None, in_progress=None,
          unmerged=(), branches=None, worktrees=(), stash=0, detached=False, error=None, default="main",
          url="https://github.com/acme/widget.git", root="/srv/src"):
    return {
        "folder": folder, "path": f"{root}/{folder}", "key": key,
        "remote": {"name": "origin", "url": url, "host": "github.com", "key": key} if key else None,
        "default_branch": default, "origin_ref": f"origin/{default}", "origin_sha": origin_sha,
        "origin_date": origin_date,
        "head": {"branch": None if detached else branch, "sha": sha, "detached": detached,
                 "upstream": f"origin/{branch}", "ahead": ahead, "behind": behind,
                 "vs_default": {"ahead": ahead, "behind": behind}},
        "branches": branches if branches is not None else [
            {"name": default, "sha": sha, "upstream": f"origin/{default}", "ahead": ahead, "behind": behind,
             "gone": False, "merged": True, "unpushed": ahead}],
        "worktrees": list(worktrees),
        "dirty": dirty or {"staged": 0, "unstaged": 0, "untracked": 0, "unmerged": 0},
        "dirty_paths": [], "dirty_paths_truncated": False, "stash_count": stash,
        "in_progress": in_progress, "unmerged_paths": list(unmerged), "last_commit": "2026-01-02T00:00:00+00:00",
        "error": error,
    }


def snap(host, *repos, root="/srv/src"):
    return {"schema": "repo-inventory", "schema_version": 1, "host": host, "generated_at": "2026-01-03T00:00:00Z",
            "root": root, "depth": 2, "fetched": False, "max_paths": 20, "repos": list(repos)}


class CompareLogic(unittest.TestCase):
    def kinds(self, result, key="acme/widget"):
        repo = next(r for r in result["repos"] if r["key"] == key)
        return {(i["host"], i["kind"]) for i in repo["issues"]}

    def test_identical_clean_hosts_have_no_drift(self):
        result = ri.compare([snap("alpha", entry()), snap("beta", entry())])
        self.assertFalse(result["drift"])

    def test_stale_origin_is_attributed_to_the_older_host(self):
        result = ri.compare([snap("alpha", entry()),
                             snap("beta", entry(origin_sha="2" * 40, origin_date="2026-01-05T00:00:00+00:00"))])
        self.assertIn(("alpha", "stale-origin"), self.kinds(result))
        self.assertTrue(result["drift"])

    def test_missing_folder_branch_only_and_unpushed(self):
        feat = {"name": "feat", "sha": "3" * 40, "upstream": None, "ahead": 0, "behind": 0, "gone": False,
                "merged": False, "unpushed": 2}
        main = entry()["branches"][0]
        result = ri.compare([snap("alpha", entry(branches=[main, feat]), entry(folder="gizmo", key="acme/gizmo")),
                             snap("beta", entry(folder="Widget2"))])
        kinds = self.kinds(result)
        self.assertIn(("alpha", "branch-only"), kinds)
        self.assertIn(("alpha", "unpushed"), kinds)
        self.assertIn((None, "folder-differs"), kinds)
        self.assertIn(("beta", "missing"), self.kinds(result, "acme/gizmo"))

    def test_dirty_conflict_detached_and_off_default(self):
        result = ri.compare([
            snap("alpha", entry(dirty={"staged": 1, "unstaged": 0, "untracked": 0, "unmerged": 1},
                                in_progress=["merge"], unmerged=["f.txt"])),
            snap("beta", entry(detached=True)),
            snap("gamma", entry(branch="feat"))])
        kinds = self.kinds(result)
        for want in [("alpha", "dirty"), ("alpha", "conflict"), ("alpha", "in-progress"), ("beta", "detached"),
                     ("gamma", "off-default")]:
            self.assertIn(want, kinds)

    def test_alias_groups_renamed_repos(self):
        a = snap("alpha", entry(key="acme/old-name", url="https://github.com/acme/old-name.git"))
        b = snap("beta", entry(key="acme/new-name", url="git@github.com:acme/new-name.git"))
        self.assertEqual({r["key"] for r in ri.compare([a, b])["repos"]}, {"acme/old-name", "acme/new-name"})
        merged = ri.compare([a, b], aliases=ri.normalize_aliases({"old-name": "new-name"}))
        self.assertEqual([r["key"] for r in merged["repos"]], ["acme/new-name"])
        self.assertFalse(merged["drift"])

    def test_repos_without_remote_are_never_missing(self):
        result = ri.compare([snap("alpha", entry(folder="scratch", key=None)), snap("beta")])
        self.assertNotIn("missing", {i["kind"] for r in result["repos"] for i in r["issues"]})

    def test_two_checkouts_of_one_repo_on_a_host_are_reported(self):
        a, b = entry(folder="widget"), entry(folder="widget-copy")
        a["real_path"], b["real_path"] = "/srv/src/widget", "/srv/src/widget-copy"
        result = ri.compare([snap("alpha", a, b), snap("beta", entry())])
        repo = next(r for r in result["repos"] if r["key"] == "acme/widget")
        dup = [i for i in repo["issues"] if i["kind"] == "duplicate-clones"]
        self.assertEqual([i["host"] for i in dup], ["alpha"])
        self.assertIn("widget-copy", dup[0]["detail"])
        self.assertTrue(result["drift"])
        self.assertEqual(len(ri.plan(result)["hosts"]["alpha"]), 2)

    def test_same_checkout_listed_twice_is_collapsed(self):
        # An older snapshot (or another tool) may list a linked folder as a second repo: same real path.
        a, b = entry(folder="widget"), entry(folder="widget-link")
        a["real_path"] = b["real_path"] = "/srv/src/widget"
        result = ri.compare([snap("alpha", a, b), snap("beta", entry())])
        repo = next(r for r in result["repos"] if r["key"] == "acme/widget")
        self.assertEqual(len(repo["entries"]["alpha"]), 1)
        self.assertIn("widget-link", repo["entries"]["alpha"][0]["aliases"])
        self.assertFalse(result["drift"])
        steps = ri.plan(result)["hosts"]["alpha"]
        self.assertEqual(len(steps), 1)

    def test_table_and_exit_code(self):
        result = ri.compare([snap("alpha", entry()), snap("beta", entry(folder="w2"))])
        text = ri.render_compare(result)
        self.assertIn("acme/widget", text)
        self.assertIn("w2", text)


class PlanLogic(unittest.TestCase):
    def steps(self, plan, host, key="acme/widget"):
        return next(r for r in plan["hosts"][host] if r["key"] == key)["steps"]

    def human(self, plan, host, key="acme/widget"):
        return " ".join(s["reason"] for s in self.steps(plan, host, key) if s["kind"] == "human")

    def safe(self, plan, host, key="acme/widget"):
        return [s["command"] for s in self.steps(plan, host, key) if s["kind"] == "safe"]

    def test_classification(self):
        plan = ri.plan(ri.compare([
            snap("alpha", entry(behind=2)),
            snap("beta", entry(ahead=1, behind=1)),
            snap("gamma", entry(dirty={"staged": 0, "unstaged": 1, "untracked": 0, "unmerged": 0}, behind=1)),
            snap("delta", entry(detached=True)),
            snap("eps", entry(ahead=3)),
        ]))
        self.assertTrue(any("pull --ff-only" in c for c in self.safe(plan, "alpha")))
        self.assertIn("diverged", self.human(plan, "beta"))
        self.assertIn("dirty", self.human(plan, "gamma"))
        self.assertIn("detached", self.human(plan, "delta"))
        self.assertIn("unpushed", self.human(plan, "eps"))
        for host in ("beta", "gamma", "delta", "eps"):
            self.assertFalse(any("pull" in c for c in self.safe(plan, host)), host)
        for host in ("alpha", "beta", "gamma", "delta", "eps"):
            self.assertTrue(any("fetch" in c for c in self.safe(plan, host)), host)

    def test_stale_but_clean_host_gets_ff_pull(self):
        plan = ri.plan(ri.compare([snap("alpha", entry()),
                                   snap("beta", entry(origin_sha="2" * 40, origin_date="2026-02-01T00:00:00+00:00"))]))
        self.assertTrue(any("pull --ff-only" in c for c in self.safe(plan, "alpha")))
        self.assertFalse(any("pull" in c for c in self.safe(plan, "beta")))

    def test_missing_clone_suggests_url_without_credentials(self):
        plan = ri.plan(ri.compare([
            snap("alpha", entry(folder="gizmo", key="acme/gizmo", url="https://github.com/acme/gizmo.git")),
            snap("beta", root="/home/beta/src")]))
        step = next(s for s in self.steps(plan, "beta", "acme/gizmo") if s["kind"] == "human")
        self.assertIn("missing", step["reason"])
        self.assertIn('git -C "/home/beta/src" clone -- "https://github.com/acme/gizmo.git" "gizmo"', step["suggest"])

    def test_option_like_clone_url_is_not_suggested(self):
        plan = ri.plan(ri.compare([
            snap("alpha", entry(folder="gizmo", key="acme/gizmo", url="--upload-pack=touch x")), snap("beta")]))
        step = next(s for s in self.steps(plan, "beta", "acme/gizmo") if s["kind"] == "human")
        self.assertNotIn("suggest", step)

    def test_control_characters_cannot_forge_lines(self):
        s = ri.validate_snapshot(snap("alpha", entry(folder="w\n  SAFE         git evil", behind=1)), "test")
        self.assertNotIn("\n", s["repos"][0]["folder"])
        text = ri.render_plan(ri.plan(ri.compare([s])))
        self.assertFalse([l for l in text.splitlines() if l.lstrip().startswith("SAFE") and "evil" in l])

    def test_unsafe_path_is_not_printed_as_command(self):
        plan = ri.plan(ri.compare([snap("alpha", entry(folder='w"; rm -rf x', behind=1))]))
        self.assertEqual(self.safe(plan, "alpha"), [])
        self.assertIn("unsafe", self.human(plan, "alpha"))

    def test_every_applicable_reason_is_reported(self):
        """Each fact is judged on its own: no fact hides another, and a pull is planned only when clean,
        on the default branch, with nothing in progress and behind only."""
        import itertools
        counts = {"even": (0, 0), "ahead": (2, 0), "behind": (0, 3), "diverged": (2, 3)}
        for dirty, ab, where, op in itertools.product((False, True), counts, ("default", "off-default", "detached"),
                                                      ("none", "conflict", "rebase")):
            a, b = counts[ab]
            e = entry(branch="feat" if where == "off-default" else "main", detached=where == "detached",
                      ahead=a, behind=b,
                      dirty={"staged": int(dirty), "unstaged": int(dirty), "untracked": 0,
                             "unmerged": int(op == "conflict")},
                      in_progress={"none": None, "conflict": ["merge"], "rebase": ["rebase"]}[op],
                      unmerged=["f.txt"] if op == "conflict" else ())
            # Branch counts as snapshot writes them: main tracks origin/main; feat has no upstream.
            main = {"name": "main", "sha": "1" * 40, "upstream": "origin/main", "ahead": 0, "behind": 0,
                    "gone": False, "merged": True, "unpushed": 0}
            if where == "default":
                main.update(ahead=a, behind=b, unpushed=a, merged=not a)
                e["branches"] = [main]
            elif where == "off-default":
                e["branches"] = [main, {"name": "feat", "sha": "3" * 40, "upstream": None, "ahead": None,
                                        "behind": None, "gone": False, "merged": not a, "unpushed": a}]
            else:
                e["branches"] = [main]
            steps = ri._host_steps(e, stale=False)
            text = " | ".join(s.get("reason") or s.get("command") for s in steps)
            human = " | ".join(s["reason"] for s in steps if s["kind"] == "human")
            notes = " | ".join(s["reason"] for s in steps if s["kind"] == "note")
            pulls = [s for s in steps if s["kind"] == "safe" and "pull" in s["command"]]
            want = []
            if dirty:
                want.append("dirty: ")
            if op == "conflict":
                want.append("conflict: ")
            if op == "rebase":
                want.append("rebase in progress")
            if where == "off-default":
                want.append("on branch feat")
            if where == "detached":
                want.append("detached HEAD")
            if where == "default" and ab == "diverged":
                want.append("diverged")
            if where != "detached" and a:
                want.append("unpushed: " + ("main" if where == "default" else "feat"))
            if where == "detached" and a:
                want.append(f"{a} commit(s) not on origin/main")
            with self.subTest(dirty=dirty, ab=ab, where=where, op=op):
                self.assertTrue(any("fetch" in s.get("command", "") for s in steps), text)
                for w in want:
                    self.assertIn(w, human, text)
                clean_pull = not dirty and op == "none" and where == "default" and ab == "behind"
                self.assertEqual(bool(pulls), clean_pull, text)
                wait_note = where == "default" and ab == "behind" and not clean_pull
                self.assertEqual("once" in notes, wait_note, text)

    def test_dirty_is_reported_off_default_and_detached(self):
        dirty = {"staged": 1, "unstaged": 2, "untracked": 0, "unmerged": 0}
        plan = ri.plan(ri.compare([snap("alpha", entry(branch="feat", dirty=dirty)),
                                   snap("beta", entry(detached=True, dirty=dirty)),
                                   snap("gamma", entry(dirty=dirty, in_progress=["rebase"]))]))
        for host, other in (("alpha", "on branch feat"), ("beta", "detached"), ("gamma", "in progress")):
            with self.subTest(host=host):
                reasons = self.human(plan, host)
                self.assertIn("dirty: 1 staged, 2 unstaged", reasons)
                self.assertIn(other, reasons)
                self.assertFalse(any("pull" in c for c in self.safe(plan, host)))

    def test_backslash_values_are_never_quoted(self):
        # A trailing backslash would escape the closing quote of a printed command.
        plan = ri.plan(ri.compare([snap("alpha", entry(folder="w\\", behind=1))]))
        self.assertEqual(self.safe(plan, "alpha"), [])
        self.assertIn("unsafe", self.human(plan, "alpha"))
        for url in ("https://github.com/acme/gizmo\\", "https://github.com/acme/gizmo\\\" x"):
            with self.subTest(url=url):
                plan = ri.plan(ri.compare([snap("alpha", entry(folder="gizmo", key="acme/gizmo", url=url)),
                                           snap("beta")]))
                step = next(s for s in self.steps(plan, "beta", "acme/gizmo") if s["kind"] == "human")
                self.assertNotIn("suggest", step)

    def test_windows_local_origin_is_suggested_with_forward_slashes(self):
        plan = ri.plan(ri.compare([
            snap("alpha", entry(folder="gizmo", key="acme/gizmo", url="C:\\git\\acme\\gizmo.git")),
            snap("beta", root="/home/beta/src")]))
        step = next(s for s in self.steps(plan, "beta", "acme/gizmo") if s["kind"] == "human")
        self.assertIn('clone -- "C:/git/acme/gizmo.git" "gizmo"', step["suggest"])

    def test_render(self):
        text = ri.render_plan(ri.plan(ri.compare([snap("alpha", entry(behind=1))])))
        self.assertIn("SAFE", text)
        self.assertIn("alpha", text)


class Summary(unittest.TestCase):
    def test_only_drift_relevant_repos_and_cap(self):
        clean = [entry(folder=f"clean{i}", key=f"acme/clean{i}") for i in range(5)]
        dirty = [entry(folder=f"dirty{i}", key=f"acme/dirty-repo-with-a-long-name-{i}", behind=i % 3,
                       dirty={"staged": 1, "unstaged": 2, "untracked": 3, "unmerged": 0}, stash=1)
                 for i in range(600)]
        text = ri.render_summary(snap("alpha", *(clean + dirty)))
        self.assertLess(len(text.encode("utf-8")), 16 * 1024)
        self.assertIn("dirty-repo-with-a-long-name-0", text)
        self.assertNotIn("[clean1]", text)
        self.assertIn("more", text)


class Ssh(unittest.TestCase):
    def test_peer_snapshot_over_ssh(self):
        calls = []

        def fake(argv, timeout):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, ("banner\n" + json.dumps(snap("beta", entry()))).encode(), b"")

        with mock.patch.object(ri, "ssh_run", fake):
            s = ri.fetch_ssh("beta", "python -I x.py snapshot")
        self.assertEqual(s["host"], "beta")
        self.assertEqual(calls[0][0], "ssh")
        self.assertIn("BatchMode=yes", calls[0])
        self.assertEqual(calls[0][-2:], ["beta", "python -I x.py snapshot"])

    def test_option_like_peer_refused(self):
        for bad in ["-oProxyCommand=x", "a b", "", "beta;x"]:
            with self.subTest(bad=bad), self.assertRaises(ri.UsageError):
                ri.fetch_ssh(bad, "x")

    def test_ssh_failure_and_bad_output(self):
        with mock.patch.object(ri, "ssh_run", lambda a, t: subprocess.CompletedProcess(a, 255, b"", b"denied")):
            with self.assertRaises(ri.UsageError):
                ri.fetch_ssh("beta", "x")
        with mock.patch.object(ri, "ssh_run", lambda a, t: subprocess.CompletedProcess(a, 0, b"not json", b"")):
            with self.assertRaises(ri.UsageError):
                ri.fetch_ssh("beta", "x")


class LoadValidation(unittest.TestCase):
    def test_rejects_foreign_json_and_bad_host(self):
        d = Path(tempfile.mkdtemp(prefix="repo-inv-load-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "repos-alpha.json").write_text(json.dumps({"schema": "other"}), encoding="utf-8")
        with self.assertRaises(ri.UsageError):
            ri.load_inputs([str(d)])
        bad = snap("../x")
        (d / "repos-alpha.json").write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(ri.UsageError):
            ri.load_inputs([str(d)])
        (d / "repos-alpha.json").write_text(json.dumps(snap("alpha", {"folder": "x", "head": "oops"})), encoding="utf-8")
        with self.assertRaises(ri.UsageError):
            ri.load_inputs([str(d)])

    def test_malformed_values_exit_2_not_traceback(self):
        d = Path(tempfile.mkdtemp(prefix="repo-inv-bad-"))
        self.addCleanup(shutil.rmtree, d, True)
        bad = entry()
        bad["dirty"] = {"unmerged": "x"}
        (d / "repos-alpha.json").write_text(json.dumps(snap("alpha", bad)), encoding="utf-8")
        code, _, err = run_main(["compare", str(d)])
        self.assertEqual(code, 2)
        self.assertIn("malformed", err)


# ---------------------------------------------------------------- synthetic repositories

@unittest.skipUnless(HAVE_GIT, "git not installed")
class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = cls.tmp = Path(tempfile.mkdtemp(prefix="repo-inv-"))
        origins = t / "origins" / "acme"
        origins.mkdir(parents=True)
        for name in ("widget", "gizmo"):
            g(t, "init", "-q", "--bare", "-b", "main", str(origins / f"{name}.git"))
            seed = t / "seed" / name
            g(t, "clone", "-q", str(origins / f"{name}.git"), str(seed))
            commit(seed, "f.txt", "base\n", "base")
            g(seed, "push", "-q", "origin", "main")
        cls.alpha, cls.beta = t / "alpha", t / "beta"
        cls.alpha.mkdir()
        cls.beta.mkdir()

        # beta clones widget before the origin moves on, then fetches: behind 1, clean, on main.
        bw = cls.beta / "widget-renamed"
        g(t, "clone", "-q", str(origins / "widget.git"), str(bw))
        cls.gamma = t / "gamma"
        g(t, "clone", "-q", str(origins / "widget.git"), str(cls.gamma / "widget"))  # never fetched until a test
        cls.origin_widget = commit(t / "seed" / "widget", "g.txt", "two\n", "second")
        g(t / "seed" / "widget", "push", "-q", "origin", "main")
        g(bw, "fetch", "-q")

        # alpha/widget: unpushed branch, gone upstream, stash, dirty tree, linked worktree.
        aw = cls.alpha / "widget"
        g(t, "clone", "-q", str(origins / "widget.git"), str(aw))
        g(aw, "switch", "-q", "-c", "pushed-feat")
        commit(aw, "p.txt", "p\n", "pushed")
        g(aw, "push", "-q", "-u", "origin", "pushed-feat")
        g(aw, "push", "-q", "origin", "--delete", "pushed-feat")
        g(aw, "fetch", "-q", "--prune")
        g(aw, "switch", "-q", "main")
        g(aw, "switch", "-q", "-c", "feat")
        commit(aw, "feat.txt", "feat\n", "feat")
        g(aw, "switch", "-q", "main")
        (aw / "f.txt").write_text("stashed\n", encoding="utf-8")
        g(aw, "stash", "-q")
        (aw / "a.txt").write_text("a\n", encoding="utf-8")
        g(aw, "add", "a.txt")
        (aw / "f.txt").write_text("changed\n", encoding="utf-8")
        (aw / "u.txt").write_text("u\n", encoding="utf-8")
        cls.wt = cls.alpha / "widget.worktrees" / "wt1"
        g(aw, "worktree", "add", "-q", "-b", "wt-branch", str(cls.wt))
        (cls.wt / "wt-untracked.txt").write_text("x\n", encoding="utf-8")
        gone_wt = cls.alpha / "widget.worktrees" / "wt-gone"
        g(aw, "worktree", "add", "-q", "-b", "wt-gone", str(gone_wt))
        shutil.rmtree(gone_wt)
        dotfiles = cls.alpha / ".dotfiles"  # a checkout with a hidden name is still a checkout
        g(t, "init", "-q", "-b", "main", str(dotfiles))
        commit(dotfiles, "rc", "x\n", "rc")

        # alpha/gizmo: a conflicted merge and a local commit on main.
        ag = cls.alpha / "gizmo"
        g(t, "clone", "-q", str(origins / "gizmo.git"), str(ag))
        g(ag, "switch", "-q", "-c", "side")
        commit(ag, "f.txt", "side\n", "side")
        g(ag, "switch", "-q", "main")
        commit(ag, "f.txt", "main\n", "main change")
        subprocess.run(["git", "-C", str(ag), "merge", "-q", "side"], capture_output=True)

        # alpha/thing: a credentialed https origin (never contacted); beta has the renamed repo.
        th = cls.alpha / "thing"
        g(t, "init", "-q", "-b", "main", str(th))
        commit(th, "t.txt", "t\n", "t")
        g(th, "remote", "add", "origin", "https://fakeuser:fakepass@github.com/Acme/Thing.git")
        th2 = cls.beta / "thing2"
        g(t, "init", "-q", "-b", "main", str(th2))
        commit(th2, "t.txt", "t\n", "t")
        g(th2, "remote", "add", "origin", "git@github.com:acme/thing-renamed.git")

        # Skipped: a repo inside node_modules; recorded with an error: a broken .git file.
        nm = cls.alpha / "node_modules" / "pkg"
        g(t, "init", "-q", "-b", "main", str(nm))
        (cls.alpha / "broken").mkdir()
        (cls.alpha / "broken" / ".git").write_text("gitdir: missing-dir\n", encoding="utf-8")

        cls.index = aw / ".git" / "index"
        cls.index_mtime = cls.index.stat().st_mtime_ns
        cls.snap_a = ri.snapshot(cls.alpha, host="alpha", depth=2, max_paths=20)
        cls.snap_b = ri.snapshot(cls.beta, host="beta", depth=2, max_paths=20)

    @classmethod
    def tearDownClass(cls):
        def onerror(func, path, _):
            os.chmod(path, 0o700)
            func(path)
        shutil.rmtree(cls.tmp, onerror=onerror)

    def repo(self, folder, snapshot=None):
        return next(r for r in (snapshot or self.snap_a)["repos"] if r["folder"] == folder)

    def test_header(self):
        s = self.snap_a
        self.assertEqual((s["schema"], s["schema_version"], s["host"]), ("repo-inventory", 1, "alpha"))
        self.assertTrue(s["generated_at"].endswith("Z"))

    def test_discovery_skips_worktrees_and_node_modules(self):
        self.assertEqual(sorted(r["folder"] for r in self.snap_a["repos"]), [".dotfiles", "broken", "gizmo", "thing", "widget"])

    def test_widget_fields(self):
        w = self.repo("widget")
        self.assertEqual(w["key"], "acme/widget")
        self.assertEqual(w["default_branch"], "main")
        self.assertEqual(w["origin_sha"], self.origin_widget)
        self.assertEqual(w["head"]["branch"], "main")
        self.assertFalse(w["head"]["detached"])
        self.assertEqual(w["head"]["vs_default"], {"ahead": 0, "behind": 0})
        self.assertEqual(w["dirty"], {"staged": 1, "unstaged": 1, "untracked": 1, "unmerged": 0})
        self.assertIn("a.txt", w["dirty_paths"])
        self.assertEqual(w["stash_count"], 1)
        self.assertIsNone(w["in_progress"])
        self.assertIsNone(w["error"])
        self.assertTrue(w["last_commit"])
        br = {b["name"]: b for b in w["branches"]}
        self.assertEqual(set(br), {"main", "feat", "pushed-feat", "wt-branch", "wt-gone"})
        self.assertEqual(br["main"]["upstream"], "origin/main")
        self.assertTrue(br["main"]["merged"])
        self.assertIsNone(br["feat"]["upstream"])
        self.assertEqual(br["feat"]["unpushed"], 1)
        self.assertFalse(br["feat"]["merged"])
        self.assertTrue(br["pushed-feat"]["gone"])
        self.assertEqual(br["pushed-feat"]["unpushed"], 1)

    def test_linked_worktree_recorded_under_main_repo(self):
        wts = {w["branch"]: w for w in self.repo("widget")["worktrees"]}
        self.assertEqual(set(wts), {"wt-branch", "wt-gone"})
        wt = wts["wt-branch"]
        self.assertEqual(Path(wt["path"]).resolve(), self.wt.resolve())
        self.assertEqual(wt["branch"], "wt-branch")
        self.assertEqual(wt["dirty"]["untracked"], 1)
        self.assertFalse(wt["locked"])
        self.assertFalse(wt["prunable"])
        self.assertTrue(wts["wt-gone"]["prunable"])
        self.assertTrue(wts["wt-gone"]["missing"])
        self.assertIsNone(wts["wt-gone"]["dirty"])

    def test_fetch_updates_only_remote_tracking_refs(self):
        gw = self.gamma / "widget"
        main_before = g(gw, "rev-parse", "refs/heads/main")
        index = gw / ".git" / "index"
        mtime = index.stat().st_mtime_ns
        before = ri.snapshot(self.gamma, host="gamma")["repos"][0]
        self.assertNotEqual(before["origin_sha"], self.origin_widget)
        self.assertEqual(before["head"]["vs_default"], {"ahead": 0, "behind": 0})
        after_snap = ri.snapshot(self.gamma, host="gamma", fetch=True)
        after = after_snap["repos"][0]
        self.assertTrue(after_snap["fetched"])
        self.assertIsNone(after["fetch_error"])
        self.assertEqual(after["origin_sha"], self.origin_widget)
        self.assertEqual(after["head"]["vs_default"], {"ahead": 0, "behind": 1})
        self.assertEqual(g(gw, "rev-parse", "refs/heads/main"), main_before)
        self.assertEqual(index.stat().st_mtime_ns, mtime)

    def test_compare_and_plan_over_ssh_with_local(self):
        payload = json.dumps(self.snap_b).encode()
        calls = []

        def fake(argv, timeout):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, payload, b"")

        with mock.patch.object(ri, "ssh_run", fake):
            code, out, err = run_main(["compare", "--ssh", "beta", "--local", "--root", str(self.alpha),
                                       "--host", "alpha", "--json"])
        self.assertEqual(code, 1, err)
        self.assertEqual({h["host"] for h in json.loads(out)["hosts"]}, {"alpha", "beta"})
        self.assertEqual(calls[-1][-2:], ["beta", ri.DEFAULT_SSH_COMMAND])
        d = Path(tempfile.mkdtemp(prefix="repo-inv-ssh-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "repos-alpha.json").write_text(json.dumps(self.snap_a), encoding="utf-8")
        with mock.patch.object(ri, "ssh_run", fake):
            code, out, err = run_main(["plan", str(d), "--ssh", "beta", "--ssh-command", "py -3 x.py snapshot"])
        self.assertEqual(code, 0, err)
        self.assertIn("== beta", out)
        self.assertEqual(calls[-1][-1], "py -3 x.py snapshot")

    def test_conflicted_merge(self):
        gz = self.repo("gizmo")
        self.assertEqual(gz["in_progress"], ["merge"])
        self.assertEqual(gz["unmerged_paths"], ["f.txt"])
        self.assertEqual(gz["dirty"]["unmerged"], 1)
        self.assertEqual(gz["head"]["ahead"], 1)

    def test_credentials_never_recorded(self):
        th = self.repo("thing")
        self.assertEqual(th["key"], "acme/thing")
        self.assertEqual(th["remote"]["url"], "https://github.com/Acme/Thing.git")
        blob = json.dumps(self.snap_a)
        self.assertNotIn("fakepass", blob)
        self.assertNotIn("fakeuser", blob)

    def test_broken_repo_recorded_with_error(self):
        self.assertTrue(self.repo("broken")["error"])

    def test_snapshot_is_read_only(self):
        self.assertEqual(self.index.stat().st_mtime_ns, self.index_mtime)
        self.assertTrue((self.alpha / "gizmo" / ".git" / "MERGE_HEAD").exists())

    def test_beta_behind_and_compare_plan(self):
        bw = self.repo("widget-renamed", self.snap_b)
        self.assertEqual(bw["head"]["vs_default"], {"ahead": 0, "behind": 1})
        result = ri.compare([self.snap_a, self.snap_b])
        self.assertTrue(result["drift"])
        widget = next(r for r in result["repos"] if r["key"] == "acme/widget")
        kinds = {(i["host"], i["kind"]) for i in widget["issues"]}
        self.assertIn((None, "folder-differs"), kinds)
        self.assertIn(("beta", "behind"), kinds)
        self.assertIn(("alpha", "dirty"), kinds)
        gizmo = next(r for r in result["repos"] if r["key"] == "acme/gizmo")
        self.assertIn(("beta", "missing"), {(i["host"], i["kind"]) for i in gizmo["issues"]})
        plan = ri.plan(result)
        beta_widget = next(r for r in plan["hosts"]["beta"] if r["key"] == "acme/widget")
        self.assertTrue(any(s["kind"] == "safe" and "pull --ff-only" in s["command"] for s in beta_widget["steps"]))
        alpha_gizmo = next(r for r in plan["hosts"]["alpha"] if r["key"] == "acme/gizmo")
        self.assertTrue(any(s["kind"] == "human" and "conflict" in s["reason"] for s in alpha_gizmo["steps"]))

    def test_alias_file_on_the_command_line(self):
        d = Path(tempfile.mkdtemp(prefix="repo-inv-cli-"))
        self.addCleanup(shutil.rmtree, d, True)
        for s in (self.snap_a, self.snap_b):
            (d / f"repos-{s['host']}.json").write_text(json.dumps(s), encoding="utf-8")
        (d / "aliases.json").write_text(json.dumps({"acme/thing": "acme/thing-renamed"}), encoding="utf-8")
        code, out, _ = run_main(["compare", str(d), "--json", "--aliases", str(d / "aliases.json")])
        self.assertEqual(code, 1)
        keys = {r["key"] for r in json.loads(out)["repos"]}
        self.assertIn("acme/thing-renamed", keys)
        self.assertNotIn("acme/thing", keys)

    def test_plan_never_executes(self):
        d = Path(tempfile.mkdtemp(prefix="repo-inv-plan-"))
        self.addCleanup(shutil.rmtree, d, True)
        for s in (self.snap_a, self.snap_b):
            (d / f"repos-{s['host']}.json").write_text(json.dumps(s), encoding="utf-8")
        with mock.patch.object(ri.subprocess, "run", side_effect=AssertionError("plan ran a command")):
            code, out, _ = run_main(["plan", str(d)])
        self.assertEqual(code, 0)
        self.assertIn("NEEDS HUMAN", out)
        self.assertIn(" clone ", out)

    def test_share_write_and_default_read(self):
        share = Path(tempfile.mkdtemp(prefix="repo-inv-share-"))
        self.addCleanup(shutil.rmtree, share, True)
        (share / "alpha-to-beta").mkdir()
        code, out, err = run_main(["snapshot", "--root", str(self.beta), "--host", "beta", "--share",
                                   "--share-root", str(share)])
        self.assertEqual(code, 0, err)
        target = share / "private" / "host-profile" / "repos-beta.json"
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["host"], "beta")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["repos-beta.json"])
        (target.parent / "repos-alpha.json").write_text(json.dumps(self.snap_a), encoding="utf-8")
        code, out, err = run_main(["compare", "--share-root", str(share), "--json"])
        self.assertEqual(code, 1, err)
        self.assertEqual({h["host"] for h in json.loads(out)["hosts"]}, {"alpha", "beta"})

    def test_invalid_host_id_refused(self):
        code, _, err = run_main(["snapshot", "--root", str(self.beta), "--host", "../evil"])
        self.assertEqual(code, 2)

    def test_snapshot_cli_out_and_summary(self):
        d = Path(tempfile.mkdtemp(prefix="repo-inv-out-"))
        self.addCleanup(shutil.rmtree, d, True)
        code, out, err = run_main(["snapshot", "--root", str(self.alpha), "--host", "alpha", "--out",
                                   str(d / "s.json"), "--max-paths", "1"])
        self.assertEqual(code, 0, err)
        s = json.loads((d / "s.json").read_text(encoding="utf-8"))
        w = next(r for r in s["repos"] if r["folder"] == "widget")
        self.assertEqual(len(w["dirty_paths"]), 1)
        self.assertTrue(w["dirty_paths_truncated"])
        code, out, err = run_main(["snapshot", "--root", str(self.alpha), "--host", "alpha", "--summary"])
        self.assertEqual(code, 0, err)
        self.assertIn("acme/widget", out)
        self.assertLess(len(out.encode("utf-8")), 16 * 1024)



def make_link(link: Path, target: Path) -> bool:
    """A junction on Windows (no admin rights needed), a symlink elsewhere. False when not permitted."""
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return r.returncode == 0 and link.is_dir()
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


@unittest.skipUnless(HAVE_GIT, "git not installed")
class LinkedFolders(unittest.TestCase):
    """A folder that is a junction or symlink to another checkout is an alias, not a second repo."""

    @classmethod
    def setUpClass(cls):
        t = cls.tmp = Path(tempfile.mkdtemp(prefix="repo-inv-link-"))
        origin = t / "origins" / "acme" / "widget.git"
        g(t, "init", "-q", "--bare", "-b", "main", str(origin))
        seed = t / "seed"
        g(t, "clone", "-q", str(origin), str(seed))
        commit(seed, "f.txt", "x\n", "base")
        g(seed, "push", "-q", "origin", "main")
        cls.root = t / "delta"
        cls.root.mkdir()
        g(t, "clone", "-q", str(origin), str(cls.root / "widget"))
        g(t, "clone", "-q", str(origin), str(cls.root / "widget-copy"))  # a real second checkout
        cls.linked = make_link(cls.root / "widget-link", cls.root / "widget")
        cls.snap = ri.snapshot(cls.root, host="delta")

    @classmethod
    def tearDownClass(cls):
        link = cls.root / "widget-link"
        if os.name == "nt" and link.exists():
            os.rmdir(link)  # removes the junction only, never its target
        elif link.is_symlink():
            link.unlink()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if not self.linked:
            self.skipTest("creating a junction or symlink is not permitted here")

    def test_link_is_an_alias_of_the_real_checkout(self):
        repos = {r["folder"]: r for r in self.snap["repos"]}
        self.assertEqual(sorted(repos), ["widget", "widget-copy"])
        self.assertEqual(repos["widget"]["aliases"], ["widget-link"])
        self.assertEqual(repos["widget-copy"]["aliases"], [])
        self.assertEqual(Path(repos["widget"]["real_path"]), (self.root / "widget").resolve())

    def test_compare_reports_the_duplicate_and_plan_lists_each_checkout_once(self):
        result = ri.compare([self.snap])
        repo = next(r for r in result["repos"] if r["key"] == "acme/widget")
        self.assertEqual(len(repo["entries"]["delta"]), 2)
        dup = [i for i in repo["issues"] if i["kind"] == "duplicate-clones"]
        self.assertEqual(len(dup), 1)
        self.assertNotIn("widget-link", dup[0]["detail"])
        paths = [s["command"] for r in ri.plan(result)["hosts"]["delta"] for s in r["steps"] if s["kind"] == "safe"]
        self.assertEqual(len(paths), 2)
        self.assertEqual(len(set(paths)), 2)
        self.assertFalse([p for p in paths if "widget-link" in p])


if __name__ == "__main__":
    unittest.main()
