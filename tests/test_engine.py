#!/usr/bin/env python3
"""Engine tests: verified, hand-computed expectations on the synthetic fixture.

Every number asserted here was derived by hand from the fixture definition
(see make_synth_repos.py) and cross-checked against raw ``git log`` output:

* root:            |H| = 12, added = 36, removed = 13, growth = 23,
                   churn = 49, modifications = 9
* src/:            added = 14, removed = 6
* docs/:           added = 11, removed = 1
* Alice:           churn = 29, modifications = 5
* Bob (canonical): churn = 20, modifications = 4
* window [day3, day9): |H| = 6, added = 5, removed = 5, modifications = 3

Run from the repository root:

    python3 tests/test_engine.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from app import gitlog, ingest, metrics, store  # noqa: E402
from make_synth_repos import build_fixture, day_ts  # noqa: E402


def _filters(**kw):
    return metrics.Filters(**kw)


class EngineTests(unittest.TestCase):
    """Full engine run against the synthetic fixture (one ingest for all tests)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rat-test-")
        os.environ["RAT_DATA_DIR"] = os.path.join(cls.tmp, "data")
        cls.fixture_dir = os.path.join(cls.tmp, "fixture")
        cls.info = build_fixture(cls.fixture_dir)
        os.environ["RAT_DATA_DIR"] = os.path.join(cls.tmp, "data")
        cls.conn = store.connect()
        store.init_db(cls.conn)
        cls.repo_id = store.create_repo(cls.conn, "synth", "zip", "fixture")
        ingest._analyze(cls.conn, cls.repo_id, cls.fixture_dir, threading.Event())

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    # -- helpers ----------------------------------------------------------

    def m(self, kind="root", path="", **fkw):
        return metrics.object_metrics(self.conn, self.repo_id, kind, path,
                                      _filters(**fkw))

    def author_ids(self):
        rows = self.conn.execute(
            "SELECT id, name, email, merged_into, synthetic FROM authors"
            " WHERE repo_id = ?", (self.repo_id,)
        ).fetchall()
        return {f"{r['name']} <{r['email']}>": r for r in rows}

    def author_row(self, label):
        root = self.conn.execute(
            """SELECT * FROM authors WHERE repo_id = ?
               AND name || ' <' || email || '>' = ?""",
            (self.repo_id, label),
        ).fetchone()
        self.assertIsNotNone(root, f"author {label} not found")
        return root

    # -- commit set -------------------------------------------------------

    def test_commit_set_size(self):
        self.assertEqual(metrics.commit_set_size(self.conn, self.repo_id, _filters()), 12)
        # independent cross-check with rev-list
        self.assertEqual(gitlog.commit_count(self.fixture_dir, "HEAD"), 12)

    def test_commit_rows(self):
        n = self.conn.execute("SELECT COUNT(*) AS n FROM commits WHERE repo_id = ?",
                              (self.repo_id,)).fetchone()["n"]
        self.assertEqual(n, 12)
        merge = self.info["commits"]["m11_merge"]
        row = self.conn.execute("SELECT id FROM commits WHERE repo_id = ? AND hash = ?",
                                (self.repo_id, merge)).fetchone()
        self.assertIsNone(row, "merge commit must not be part of H-bar")
        empty = self.info["commits"]["m8"]
        row = self.conn.execute(
            "SELECT c.id, (SELECT COUNT(*) FROM changes ch WHERE ch.commit_id = c.id) AS n"
            " FROM commits c WHERE c.repo_id = ? AND c.hash = ?",
            (self.repo_id, empty)).fetchone()
        self.assertIsNotNone(row, "empty commit must be part of H-bar")
        self.assertEqual(row["n"], 0)

    # -- root metrics -----------------------------------------------------

    def test_root_metrics(self):
        m = self.m()
        self.assertEqual((m["added"], m["removed"]), (36, 13))
        self.assertEqual(m["growth"], 23)
        self.assertEqual(m["churn"], 49)
        self.assertEqual(m["modifications"], 9)
        self.assertAlmostEqual(m["modification_frequency"], 9 / 12)
        self.assertAlmostEqual(m["churn_rate"], 49 / 12)

    def test_root_total_matches_raw_git(self):
        """Independent cross-check: sum of ``git log --numstat`` text output."""
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        out = subprocess.run(
            ["git", "-C", self.fixture_dir, "log", "--no-merges", "-M50%",
             "--numstat", "--format="],
            stdout=subprocess.PIPE, env=env, check=True,
        ).stdout.decode("utf-8", "replace")
        added = removed = 0
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                added += int(parts[0])
                removed += int(parts[1])
        self.assertEqual((added, removed), (36, 13))

    # -- directory and file metrics --------------------------------------

    def test_directory_metrics(self):
        src = self.m(kind="dir", path="src")
        self.assertEqual((src["added"], src["removed"]), (14, 6))
        self.assertEqual(src["modifications"], 5)
        docs = self.m(kind="dir", path="docs")
        self.assertEqual((docs["added"], docs["removed"]), (11, 1))
        self.assertEqual(docs["modifications"], 2)

    def test_file_metrics(self):
        cases = {
            "src/app.py": (10, 2, 3),       # m1 create, m2 extend, m12 trim
            "src/util.py": (4, 4, 2),       # m3 create, m7 delete
            "docs/guide.md": (1, 1, 1),     # m4 pure rename (0/0) + m5 edit
            "docs/readme.md": (10, 0, 1),   # creation before the rename
            "settings.ini": (6, 6, 2),      # m10 create, m13 rewrite
            "old.ini": (2, 0, 1),           # m13 below-threshold add
            "side.py": (3, 0, 1),           # m9 on branch, reachable
        }
        for path, (a, r, mods) in cases.items():
            m = self.m(kind="file", path=path)
            self.assertEqual((m["added"], m["removed"], m["modifications"]),
                             (a, r, mods), path)

    def test_binary_excluded(self):
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM changes WHERE repo_id = ? AND path = 'logo.bin'",
            (self.repo_id,)).fetchone()["n"]
        self.assertEqual(n, 0)
        p = metrics.paths(self.conn, self.repo_id)
        self.assertNotIn("logo.bin", p["files"])
        self.assertIn("src", p["dirs"])
        self.assertIn("docs", p["dirs"])

    def test_children_consistent_with_root(self):
        rows = metrics.children(self.conn, self.repo_id, "root", "", _filters())
        by_path = {r["path"]: r for r in rows}
        self.assertEqual(set(by_path), {"src", "docs", "side.py", "settings.ini", "old.ini"})
        self.assertEqual((by_path["src"]["added"], by_path["src"]["removed"]), (14, 6))
        self.assertEqual((by_path["docs"]["added"], by_path["docs"]["removed"]), (11, 1))
        # children must add up to the root totals
        self.assertEqual(sum(r["added"] for r in rows), 36)
        self.assertEqual(sum(r["removed"] for r in rows), 13)

    # -- commit-set windows [i, j) ---------------------------------------

    def test_window_semantics(self):
        w = _filters(since=day_ts(3), until=day_ts(9))
        self.assertEqual(metrics.commit_set_size(self.conn, self.repo_id, w), 6)
        m = self.m(since=day_ts(3), until=day_ts(9))
        self.assertEqual((m["added"], m["removed"]), (5, 5))
        self.assertEqual(m["modifications"], 3)

        # until is exclusive: [day3, day10) adds m9 (side.py, +3)
        m2 = self.m(since=day_ts(3), until=day_ts(10))
        self.assertEqual((m2["added"], m2["removed"]), (8, 5))
        self.assertEqual(m2["modifications"], 4)

        # since is inclusive: window starting exactly at day3 contains m3
        self.assertEqual(m["added"] - m["removed"], 0)

    # -- author merging (.mailmap) ---------------------------------------

    def test_mailmap_merge(self):
        authors = self.author_ids()
        self.assertIn("Alice <alice@example.com>", authors)
        canonical = "Bob Smith <bob.smith@canonical.com>"
        self.assertIn(canonical, authors)
        canon = authors[canonical]
        self.assertEqual(canon["synthetic"], 1, "canonical identity is created synthetically")
        self.assertEqual(canon["merged_into"], None)
        for raw in ("Bob <bob@work.com>", "Robert Smith <r.smith@personal.com>"):
            self.assertEqual(authors[raw]["merged_into"], canon["id"], raw)

    def test_author_breakdown(self):
        rows = metrics.author_breakdown(self.conn, self.repo_id, "root", "", _filters())
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(set(by_name), {"Alice", "Bob Smith"})
        alice, bob = by_name["Alice"], by_name["Bob Smith"]
        self.assertEqual((alice["churn"], alice["modifications"]), (29, 5))
        self.assertEqual((bob["churn"], bob["modifications"]), (20, 4))
        self.assertAlmostEqual(alice["ownership"], 29 / 49)
        self.assertAlmostEqual(bob["ownership"], 20 / 49)

    def test_author_filter(self):
        canon = self.author_row("Bob Smith <bob.smith@canonical.com>")
        ids = store.expand_author_filter(self.conn, self.repo_id, [canon["id"]])
        self.assertEqual(len(ids), 3, "canonical + both raw identities form the group")
        raw_ids = {self.author_row("Bob <bob@work.com>")["id"],
                   self.author_row("Robert Smith <r.smith@personal.com>")["id"]}
        self.assertTrue(raw_ids.issubset(set(ids)))
        m = self.m(author_ids=ids)
        self.assertEqual((m["added"], m["removed"], m["churn"], m["modifications"]),
                         (14, 6, 20, 4))
        self.assertEqual(m["commit_set_size"], 6)

    # -- manual merging ---------------------------------------------------

    def test_manual_merge_and_unmerge(self):
        alice = self.author_row("Alice <alice@example.com>")
        bob = self.author_row("Bob Smith <bob.smith@canonical.com>")
        store.apply_author_merges(self.conn, self.repo_id, {alice["id"]: bob["id"]})
        rows = metrics.author_breakdown(self.conn, self.repo_id, "root", "", _filters())
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["churn"], rows[0]["modifications"]), (49, 9))

        # merging the other direction is a no-op: they now share a root
        store.apply_author_merges(self.conn, self.repo_id, {bob["id"]: alice["id"]})
        rows = metrics.author_breakdown(self.conn, self.repo_id, "root", "", _filters())
        self.assertEqual(len(rows), 1)

        # unmerge restores the original grouping
        self.conn.execute("UPDATE authors SET merged_into = NULL WHERE id = ?",
                          (alice["id"],))
        self.conn.commit()
        rows = metrics.author_breakdown(self.conn, self.repo_id, "root", "", _filters())
        self.assertEqual(len(rows), 2)

    # -- explicit commit set ---------------------------------------------

    def test_commit_set_filter(self):
        h1 = self.info["commits"]["m1"]
        h2 = self.info["commits"]["m2"]
        m = self.m(commit_hashes=[h1, h2])
        self.assertEqual(m["commit_set_size"], 2)
        self.assertEqual((m["added"], m["removed"], m["modifications"]), (20, 0, 2))

    # -- timeline ---------------------------------------------------------

    def test_timeline(self):
        series = metrics.timeline(self.conn, self.repo_id, _filters())
        total_added = sum(p["added"] for p in series)
        total_removed = sum(p["removed"] for p in series)
        self.assertEqual((total_added, total_removed), (36, 13))
        self.assertEqual(series[0]["ts"], day_ts(1))


class ZipIngestTests(unittest.TestCase):
    """The zip path (extract + analyse) must reproduce the same metrics."""

    def test_zip_roundtrip(self):
        tmp = tempfile.mkdtemp(prefix="rat-zip-")
        os.environ["RAT_DATA_DIR"] = os.path.join(tmp, "data")
        fixture_dir = os.path.join(tmp, "fixture")
        build_fixture(fixture_dir)

        zip_path = os.path.join(tmp, "fixture.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(fixture_dir):
                for name in files:
                    full = os.path.join(root, name)
                    zf.write(full, os.path.relpath(full, fixture_dir))

        conn = store.connect()
        try:
            store.init_db(conn)
            repo_id = store.create_repo(conn, "zip", "zip", "fixture.zip")
            target = os.path.join(store.repos_dir(), str(repo_id))
            path = ingest._extract_zip(zip_path, target)
            ingest._analyze(conn, repo_id, path, threading.Event())
            m = metrics.object_metrics(conn, repo_id, "root", "", _filters())
            self.assertEqual((m["added"], m["removed"], m["modifications"]), (36, 13, 9))
            rows = metrics.author_breakdown(conn, repo_id, "root", "", _filters())
            self.assertEqual(len(rows), 2, "mailmap from the worktree must be applied")
        finally:
            conn.close()

    def test_zip_slip_rejected(self):
        tmp = tempfile.mkdtemp(prefix="rat-zipslip-")
        os.environ["RAT_DATA_DIR"] = os.path.join(tmp, "data")
        bad = os.path.join(tmp, "evil.zip")
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../evil.txt", "boom")
        conn = store.connect()
        try:
            store.init_db(conn)
            with self.assertRaises(ValueError):
                ingest._extract_zip(bad, os.path.join(tmp, "target"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
