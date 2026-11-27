"""v3 thread protocol tests for cross_pc_mail.py: naming grammar, lint, close and archive.

Stdlib unittest only (run: python -I -m unittest discover -s <scripts dir> -p "test_cross_pc_threads.py").
Each test builds a throwaway share in a temp folder with synthetic hosts alpha and beta.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_spec = importlib.util.spec_from_file_location("cross_pc_mail_v3", Path(__file__).with_name("cross_pc_mail.py"))
mail = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mail)

AGENTS = """# Share rules

| Host id | Machine | Local path |
|---------|---------|------------|
| `alpha` | ALPHA (desktop) | `C:\\Share` |
| `beta` | BETA (laptop) | `D:\\Share` |
"""
OLD = time.time() - 40 * 86400  # well past the 10-minute guard and a 30-day --older-than


class ShareTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "share"
        for pair in ("alpha-to-beta", "beta-to-alpha"):
            (self.root / pair).mkdir(parents=True)
        (self.root / "AGENTS.md").write_text(AGENTS, encoding="utf-8")
        (self.root / ".sync").mkdir()
        env = mock.patch.dict(os.environ, {"CROSS_PC_SHARE": str(self.root), "CROSS_PC_HOST": "alpha"})
        env.start()
        self.addCleanup(env.stop)
        cand = mock.patch.object(mail, "CANDIDATES", ())
        cand.start()
        self.addCleanup(cand.stop)
        self.out_dir, self.in_dir = self.root / "alpha-to-beta", self.root / "beta-to-alpha"
        self.clock = OLD

    # helpers ---------------------------------------------------------------------------------
    def put(self, rel: str, text: str = "# msg\n", when: float | None = None) -> Path:
        """Write a note, or a package README when rel has no .md suffix; each call is one tick newer."""
        p = self.root / rel
        f = p if p.suffix == ".md" else p / "README.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        self.clock += 60
        t = self.clock if when is None else when
        os.utime(f, (t, t))
        return p

    def run_cli(self, *argv) -> tuple[int, str]:
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            try:
                code = mail.main(list(argv))
            except SystemExit as e:  # sys.exit("message") or sys.exit(n)
                code = e.code if isinstance(e.code, int) else 2
                err.write(str(e.code))
        self.stderr = err.getvalue()
        return code, buf.getvalue()

    def status(self, *extra) -> dict:
        code, out = self.run_cli("status", "--json", "--since", "0", *extra)
        self.assertEqual(code, 0)
        return {r["thread"]: r for r in json.loads(out)}

    def tree(self) -> list[str]:
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))


class NameGrammar(unittest.TestCase):
    def test_parse_shapes(self):
        cases = {
            "disk-audit-2026-11-20": ("disk-audit", "2026-11-20", "", "base"),
            "disk-audit-2026-11-20-fix": ("disk-audit", "2026-11-20", "fix", "followup"),
            "disk-audit-2026-11-20-parity-notice": ("disk-audit", "2026-11-20", "parity-notice", "followup"),
            "lamp-v2-2026-11-23-progress-2026-11-24": ("lamp-v2", "2026-11-23", "progress-2026-11-24", "second-date"),
            "2026-03-22-bug-kiosk": ("bug-kiosk", "2026-03-22", "", "date-first"),
            "keys": ("keys", "", "", "undated"),
            "NOTE-2026-11-15": ("", "", "", "uppercase"),
        }
        for s, (topic, date, suffix, shape) in cases.items():
            n = mail.parse_name(s)
            self.assertEqual((n["topic"], n["date"], n["suffix"], n["shape"]), (topic, date, suffix, shape), s)


class StatusGrouping(ShareTest):
    def test_canonical_followups_join_the_base_thread(self):
        self.put("alpha-to-beta/disk-audit-2026-11-20")
        self.put("beta-to-alpha/disk-audit-2026-11-20-reply.md")
        self.put("alpha-to-beta/disk-audit-2026-11-20-addendum.md")
        self.put("beta-to-alpha/disk-audit-2026-11-20-correction.md")
        rows = self.status()
        self.assertEqual(list(rows), ["disk-audit-2026-11-20"])
        r = rows["disk-audit-2026-11-20"]
        self.assertEqual((r["messages"], r["state"], r["last"]), (4, "your turn", "disk-audit-2026-11-20-correction.md"))

    def test_legacy_suffix_before_date_note_joins_existing_base(self):
        self.put("alpha-to-beta/disk-audit-2026-11-20")
        self.put("alpha-to-beta/disk-audit-fix-2026-11-20.md")          # suffix before date, same pair
        self.put("beta-to-alpha/disk-audit-result-2026-11-20.md")       # ... from the other direction too
        self.put("beta-to-alpha/disk-audit-fix-2026-11-20-ack.md")      # a reply to the legacy note
        self.put("alpha-to-beta/fan-curve-2026-11-21-2026-11-21.md")    # nonsense: left alone
        rows = self.status()
        self.assertEqual(rows["disk-audit-2026-11-20"]["messages"], 4)
        self.assertNotIn("disk-audit-fix-2026-11-20", rows)
        self.assertNotIn("disk-audit-result-2026-11-20", rows)

    def test_legacy_package_with_word_before_date_stays_its_own_thread(self):
        self.put("alpha-to-beta/disk-audit-2026-11-20")
        self.put("alpha-to-beta/disk-audit-extra-2026-11-20")  # a package is always a base by protocol
        rows = self.status()
        self.assertEqual(sorted(rows), ["disk-audit-2026-11-20", "disk-audit-extra-2026-11-20"])

    def test_second_date_shape_and_missing_base_anchor_at_topic_date(self):
        self.put("alpha-to-beta/lamp-v2-2026-11-23")
        self.put("beta-to-alpha/lamp-v2-2026-11-23-progress-2026-11-24.md")
        self.put("beta-to-alpha/heat-map-2026-11-10-result.md")  # its base was already cleaned up
        self.put("alpha-to-beta/heat-map-2026-11-10-ack.md")
        rows = self.status()
        self.assertEqual(rows["lamp-v2-2026-11-23"]["messages"], 2)
        self.assertEqual(rows["heat-map-2026-11-10"]["messages"], 2)
        self.assertEqual(len(rows), 2)


class ReplyNames(ShareTest):
    def test_reply_uses_canonical_thread_name(self):
        self.put("alpha-to-beta/disk-audit-2026-11-20")
        self.put("beta-to-alpha/disk-audit-fix-2026-11-20.md")
        self.put("beta-to-alpha/lamp-2026-11-21-addendum.md")
        self.assertEqual(self.run_cli("reply", "--to", "beta", "--re", "disk-audit-fix-2026-11-20.md")[0], 0)
        self.assertTrue((self.out_dir / "disk-audit-2026-11-20-reply.md").is_file())
        self.assertEqual(self.run_cli("reply", "--to", "beta", "--re", "lamp-2026-11-21-addendum.md",
                                      "--kind", "result")[0], 0)
        self.assertTrue((self.out_dir / "lamp-2026-11-21-result.md").is_file())

    def test_suffix_for_other_followups_and_own_items(self):
        self.put("alpha-to-beta/disk-audit-2026-11-20")
        # A correction to my own message: --re may name an item in my outgoing folder.
        code, out = self.run_cli("reply", "--to", "beta", "--re", "disk-audit-2026-11-20", "--suffix", "correction")
        self.assertEqual(code, 0, self.stderr)
        target = self.out_dir / "disk-audit-2026-11-20-correction.md"
        self.assertEqual(out.strip(), str(target))
        self.assertIn("](../alpha-to-beta/disk-audit-2026-11-20/README.md)", target.read_text(encoding="utf-8"))
        for bad in ("Bad Word", "fix-2026-11-21", "done"):
            self.assertNotEqual(self.run_cli("reply", "--to", "beta", "--re", "disk-audit-2026-11-20",
                                             "--suffix", bad)[0], 0, bad)
        self.assertNotEqual(self.run_cli("reply", "--to", "beta", "--re", "disk-audit-2026-11-20",
                                         "--kind", "ack", "--suffix", "note")[0], 0)


class Close(ShareTest):
    def test_close_writes_done_and_status_hides_it(self):
        self.put("beta-to-alpha/print-queue-2026-11-19")
        self.put("alpha-to-beta/print-queue-2026-11-19-result.md")
        self.put("beta-to-alpha/print-queue-2026-11-19-ack.md")
        code, out = self.run_cli("close", "--to", "beta", "--re", "print-queue-2026-11-19-ack.md")
        self.assertEqual(code, 0, self.stderr)
        done = self.out_dir / "print-queue-2026-11-19-done.md"
        self.assertEqual(out.strip(), str(done))
        text = done.read_text(encoding="utf-8")
        self.assertIn("**From:** agent on alpha", text)
        self.assertIn("**Replies to:** [`beta-to-alpha/print-queue-2026-11-19-ack.md`]"
                      "(../beta-to-alpha/print-queue-2026-11-19-ack.md)", text)
        self.assertEqual(self.run_cli("check", str(done))[0], 0)
        self.assertEqual(self.status(), {})
        self.assertEqual(self.status("--all")["print-queue-2026-11-19"]["state"], "done")
        self.assertNotEqual(self.run_cli("close", "--to", "beta", "--re", "print-queue-2026-11-19")[0], 0)  # exists

    def test_sender_may_close_and_a_later_message_reopens(self):
        self.put("alpha-to-beta/print-queue-2026-11-19")
        self.put("beta-to-alpha/print-queue-2026-11-19-result.md")
        self.assertEqual(self.run_cli("close", "--to", "beta", "--re", "print-queue-2026-11-19")[0], 0, )
        done = self.out_dir / "print-queue-2026-11-19-done.md"
        os.utime(done, (self.clock + 60, self.clock + 60))
        self.assertEqual(self.status("--all")["print-queue-2026-11-19"]["state"], "done")
        self.put("beta-to-alpha/print-queue-2026-11-19-question.md", when=self.clock + 600)
        self.assertEqual(self.status()["print-queue-2026-11-19"]["state"], "your turn")


class Lint(ShareTest):
    def findings(self, *extra) -> tuple[int, list[dict]]:
        code, out = self.run_cli("lint", "--json", *extra)
        return code, json.loads(out)

    def test_clean_share(self):
        self.put("alpha-to-beta/disk-audit-2026-11-20")
        self.put("beta-to-alpha/disk-audit-2026-11-20-reply.md")
        (self.root / "private").mkdir()
        (self.root / "CLAUDE.md").write_text("x", encoding="utf-8")
        (self.root / "desktop.ini").write_text("x", encoding="utf-8")
        (self.in_dir / "disk-audit-2026-11-21.md.!sync").write_text("partial", encoding="utf-8")
        code, out = self.run_cli("lint")
        self.assertEqual((code, out.strip()), (0, "clean"))

    def test_flags_names_root_entries_size_and_unknown_hosts(self):
        self.put("alpha-to-beta/disk-audit-2026-11-20")
        self.put("alpha-to-beta/disk-audit-fix-2026-11-20.md")
        self.put("beta-to-alpha/lamp-v2-2026-11-23-progress-2026-11-24.md")
        self.put("beta-to-alpha/NOTE-2026-11-15.md")
        self.put("beta-to-alpha/2026-03-22-bug-kiosk")
        (self.in_dir / "keys").mkdir()
        (self.in_dir / "keys" / "beta.pub").write_text("k", encoding="utf-8")
        (self.out_dir / "image.bin").write_bytes(b"x" * 3000)
        (self.root / "loose-notes.txt").write_text("x", encoding="utf-8")
        (self.root / "gamma-to-alpha").mkdir()
        self.put("gamma-to-alpha/hello-2026-11-20.md")
        code, found = self.findings("--max-mb", "0.002")
        self.assertEqual(code, 1)
        got = {(f["path"], f["kind"]): f["suggest"] for f in found}
        self.assertEqual(got[("alpha-to-beta/disk-audit-fix-2026-11-20.md", "suffix-before-date")],
                         "disk-audit-2026-11-20-fix.md")
        self.assertEqual(got[("beta-to-alpha/lamp-v2-2026-11-23-progress-2026-11-24.md", "second-date")],
                         "lamp-v2-2026-11-23-progress.md")
        self.assertEqual(got[("beta-to-alpha/NOTE-2026-11-15.md", "uppercase")], "note-2026-11-15.md")
        self.assertEqual(got[("beta-to-alpha/2026-03-22-bug-kiosk", "date-first")], "bug-kiosk-2026-03-22")
        self.assertIsNone(got[("beta-to-alpha/keys", "undated")])
        self.assertIn(("beta-to-alpha/keys", "no-readme"), got)
        self.assertIn(("alpha-to-beta/image.bin", "stray-file"), got)
        self.assertIn(("alpha-to-beta/image.bin", "large-file"), got)
        self.assertIn(("loose-notes.txt", "root-entry"), got)
        self.assertIn(("gamma-to-alpha", "unknown-host"), got)
        self.assertNotIn(("alpha-to-beta/disk-audit-2026-11-20", "bad-name"), got)
        self.assertFalse(any(path.startswith(".sync") or path == "AGENTS.md" for path, _ in got))

    def test_pair_and_mine_restrict_the_scope(self):
        self.put("alpha-to-beta/Bad Name.md")
        self.put("beta-to-alpha/OTHER-2026-11-15.md")
        (self.root / "loose-notes.txt").write_text("x", encoding="utf-8")
        _, mine = self.findings("--mine")
        self.assertEqual({f["path"] for f in mine}, {"alpha-to-beta/Bad Name.md"})
        _, pair = self.findings("--pair", "beta-to-alpha")
        self.assertEqual({f["path"] for f in pair}, {"beta-to-alpha/OTHER-2026-11-15.md"})
        code, out = self.run_cli("lint", "--pair", "alpha-to-beta")
        self.assertEqual(code, 1)
        self.assertIn("alpha-to-beta/Bad Name.md: bad-name", out)
        self.assertNotEqual(self.run_cli("lint", "--pair", "nope-to-beta")[0], 0)

    def test_lint_writes_nothing(self):
        self.put("alpha-to-beta/disk-audit-fix-2026-11-20.md")
        before = self.tree()
        self.run_cli("lint")
        self.assertEqual(self.tree(), before)


class Archive(ShareTest):
    def done_thread(self, topic="print-queue-2026-11-19"):
        self.put(f"alpha-to-beta/{topic}")
        (self.out_dir / topic / "run.ps1").write_text("echo hi\n", encoding="utf-8")
        os.utime(self.out_dir / topic / "run.ps1", (self.clock, self.clock))
        self.put(f"beta-to-alpha/{topic}-result.md")
        self.put(f"alpha-to-beta/{topic}-ack.md")
        self.put(f"beta-to-alpha/{topic}-done.md")
        return topic

    def test_dry_run_is_the_default_and_changes_nothing(self):
        topic = self.done_thread()
        before = self.tree()
        code, out = self.run_cli("archive")
        self.assertEqual(code, 0, self.stderr)
        self.assertIn(f"would archive beta {topic} (done)", out)
        self.assertIn("--apply", out)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / "private").exists())

    def test_apply_moves_only_own_entries_and_writes_manifest(self):
        topic = self.done_thread()
        self.put("alpha-to-beta/other-2026-11-25")  # open thread: not a candidate
        # An older cross-topic reply in my folder that links the done thread still belongs to its own thread
        # (a link counts for activity only: it is never moved with the linked thread).
        self.put("alpha-to-beta/lamp-2026-11-20-reply.md",
                 f"# r\n- **Replies to:** [`beta-to-alpha/{topic}-result.md`](../beta-to-alpha/{topic}-result.md)\n", when=OLD)
        code, out = self.run_cli("archive", "--apply")
        self.assertEqual(code, 0, self.stderr)
        dest = self.root / "private" / "archive" / "cross-pc" / "alpha-to-beta" / topic
        self.assertTrue((dest / topic / "README.md").is_file())
        self.assertTrue((dest / topic / "run.ps1").is_file())
        self.assertTrue((dest / f"{topic}-ack.md").is_file())
        self.assertFalse((self.out_dir / topic).exists())
        self.assertFalse((self.out_dir / f"{topic}-ack.md").exists())
        self.assertTrue((self.out_dir / "other-2026-11-25" / "README.md").is_file())
        self.assertTrue((self.out_dir / "lamp-2026-11-20-reply.md").is_file())
        self.assertTrue((self.in_dir / f"{topic}-result.md").is_file())   # the peer's folder is untouched
        self.assertTrue((self.in_dir / f"{topic}-done.md").is_file())
        manifests = list((self.root / "private" / "archive" / "cross-pc" / "manifests").glob("*.json"))
        self.assertEqual(len(manifests), 1)
        self.assertIn(manifests[0].name, out)
        self.assertIn("undo", out.lower())
        data = json.loads(manifests[0].read_text(encoding="utf-8"))
        files = {f["old"]: f for m in data["moves"] for f in m["files"]}
        f = files[f"alpha-to-beta/{topic}/run.ps1"]
        self.assertEqual(f["new"], f"private/archive/cross-pc/alpha-to-beta/{topic}/{topic}/run.ps1")
        self.assertEqual(f["size"], len(b"echo hi\n") + (1 if os.linesep == "\r\n" else 0))
        self.assertEqual(len(f["sha256"]), 64)
        self.assertEqual(len(files), 3)

    def test_archived_thread_leaves_status_on_both_hosts(self):
        topic = "print-queue-2026-11-19"
        self.put(f"alpha-to-beta/{topic}")
        self.put(f"beta-to-alpha/{topic}-result.md")
        self.assertEqual(self.run_cli("close", "--to", "beta", "--re", topic)[0], 0)  # the sender closes
        done = self.out_dir / f"{topic}-done.md"
        os.utime(done, (self.clock + 60, self.clock + 60))
        self.clock += 60
        self.assertEqual(self.run_cli("archive", "--apply")[0], 0, self.stderr)
        self.assertFalse(done.exists())  # the done marker moved with the archive
        self.assertEqual(self.status(), {})
        self.assertEqual(self.status("--all")[topic]["state"], "archived")
        with mock.patch.dict(os.environ, {"CROSS_PC_HOST": "beta"}):
            self.assertEqual(self.status(), {})  # the peer's leftover reply doesn't revive it
        self.put(f"beta-to-alpha/{topic}-question.md", when=time.time() + 60)  # a newer message reopens it
        self.assertEqual(self.status()[topic]["state"], "your turn")

    def test_open_thread_needs_older_than(self):
        self.put("alpha-to-beta/fan-curve-2026-10-01")
        self.put("beta-to-alpha/fan-curve-2026-10-01-reply.md")
        self.assertIn("No threads to archive", self.run_cli("archive")[1])
        code, out = self.run_cli("archive", "--older-than", "30")
        self.assertEqual(code, 0)
        self.assertIn("would archive beta fan-curve-2026-10-01 (idle", out)
        self.assertIn("No threads to archive", self.run_cli("archive", "--older-than", "60")[1])

    def test_refusals(self):
        recent = self.done_thread("recent-2026-11-26")
        os.utime(self.in_dir / f"{recent}-done.md", None)  # just now
        partial = self.done_thread("partial-2026-11-19")
        (self.out_dir / partial / "big.zip.!sync").write_text("p", encoding="utf-8")
        taken = self.done_thread("taken-2026-11-18")
        (self.root / "private" / "archive" / "cross-pc" / "alpha-to-beta" / taken).mkdir(parents=True)
        self.put("beta-to-alpha/theirs-2026-11-17")
        self.put("beta-to-alpha/theirs-2026-11-17-done.md")
        before = self.tree()
        code, out = self.run_cli("archive", "--apply")
        self.assertEqual(code, 1)
        self.assertIn(f"refused {recent}: modified in the last 10 minutes", out)
        self.assertIn(f"refused {partial}: partial download", out)
        self.assertIn(f"refused {taken}: destination exists", out)
        self.assertNotIn("theirs", out)  # not mine to archive: never a candidate
        self.assertEqual(self.tree(), before)
        code, out = self.run_cli("archive", "--thread", "theirs-2026-11-17", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("refused theirs-2026-11-17: its first message is in beta-to-alpha", out)
        self.assertEqual(self.tree(), before)


if __name__ == "__main__":
    unittest.main()
