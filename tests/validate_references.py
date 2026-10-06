#!/usr/bin/env python3
"""Validate the metric engine against the official reference CSV bundle.

Usage:
    python3 tests/validate_references.py <reference.csv|reference.zip> <git-repo-dir>
                                         [--data-dir DIR]

The reference CSVs (one per provided test repository: cJSON, redis, git.git)
contain the expected value of every metric for every repository / directory /
file object, both for the whole commit set (author=ALL) and for each raw
author identity.  This script ingests the local clone with the real engine
(app.ingest), computes the same values again in bulk SQL over the ingested
tables (an independent re-implementation -- SQL vs Python), and compares every
row.  Integers must match exactly; fractions within 1e-9.

Exit code 0 iff every reference row matches.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import io
import os
import sys
import tempfile
import threading
import time
import zipfile
from collections import defaultdict

TOL = 1e-9
INT_COLS = ("added", "removed", "growth", "churn", "modifications")
FLOAT_COLS = ("modification_frequency", "churn_rate", "ownership")


def read_reference(path: str, repo_name: str):
    """Load the CSV matching ``repo_name`` from a zip bundle or plain file."""
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist()
                     if os.path.basename(n).lower().startswith(repo_name.lower() + "_")
                     and n.endswith(".csv")]
            if len(names) != 1:
                raise SystemExit(
                    f"expected exactly one CSV for {repo_name!r} in {path}, "
                    f"found {names}"
                )
            data = zf.read(names[0]).decode("utf-8")
    else:
        with open(path, encoding="utf-8") as fh:
            data = fh.read()
    return list(csv.DictReader(io.StringIO(data)))


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate engine metrics vs reference CSV")
    ap.add_argument("reference", help="reference csv or zip bundle")
    ap.add_argument("repo", help="local git clone of the reference repository")
    ap.add_argument("--data-dir", help="where to keep the analysis DB (reused if present)")
    args = ap.parse_args()

    repo_path = os.path.abspath(args.repo.rstrip("/"))
    repo_name = os.path.basename(repo_path)
    rows = read_reference(args.reference, repo_name)
    csv_commits = int(rows[0]["commit_count"])
    assert all(r["commit_set"] == "all" for r in rows), "unexpected commit_set in reference"
    ref_dirs = {r["path"] for r in rows if r["object_type"] == "directory"}
    ref_files = {r["path"] for r in rows if r["object_type"] == "file"}

    data_dir = args.data_dir or tempfile.mkdtemp(prefix=f"rat-validate-{repo_name}-")
    os.environ["RAT_DATA_DIR"] = data_dir
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import ingest, store  # noqa: E402  (env must be set first)

    conn = store.connect()
    store.init_db(conn)
    existing = conn.execute(
        "SELECT id, commit_count FROM repos WHERE path = ? AND commit_count > 0",
        (repo_path,),
    ).fetchone()
    if existing:
        repo_id, n = existing["id"], existing["commit_count"]
        note = f"reused existing analysis ({n:,} commits)"
    else:
        repo_id = store.create_repo(conn, repo_name, "clone", repo_path)
        t0 = time.time()
        ingest._analyze(conn, repo_id, repo_path, threading.Event())
        dt = time.time() - t0
        n = conn.execute("SELECT COUNT(*) AS c FROM commits WHERE repo_id = ?",
                         (repo_id,)).fetchone()["c"]
        conn.execute(
            "UPDATE repos SET status='ready', commit_count=?, progress=100,"
            " progress_label='Ready', path=? WHERE id=?",
            (n, repo_path, repo_id),
        )
        conn.commit()
        note = f"ingested {n:,} commits in {dt:.1f}s ({n / max(dt, 1e-9):,.0f} commits/s)"

    print(f"[{repo_name}] {note}")
    ok_count = "OK" if n == csv_commits else "*** MISMATCH ***"
    print(f"[{repo_name}] commit count: engine {n:,} vs reference {csv_commits:,}  {ok_count}")
    if n != csv_commits:
        return 1

    # ------------------------------------------------------------------
    # bulk aggregates: per (author id, path) -> added, removed, modifications
    # ------------------------------------------------------------------
    t0 = time.time()
    # The reference identities are .mailmap-resolved, so aggregate metrics by
    # canonical root author (for repos without a mailmap the roots are the
    # raw identities themselves, making this the identity mapping).
    rmap = store.author_root_map(conn, repo_id)

    def root_of(aid):
        return rmap.get(aid, aid)

    per = {}
    for r in conn.execute(
        """SELECT c.author_id AS aid, ch.path AS path,
                  SUM(ch.added) AS added, SUM(ch.removed) AS removed
           FROM changes ch JOIN commits c ON c.id = ch.commit_id
           WHERE ch.repo_id = ?
           GROUP BY c.author_id, ch.path""",
        (repo_id,),
    ):
        key = (root_of(r["aid"]), r["path"])
        v = per.get(key)
        if v is None:
            per[key] = [r["added"], r["removed"], 0]
        else:
            v[0] += r["added"]
            v[1] += r["removed"]

    for r in conn.execute(
        """SELECT aid, path, COUNT(*) AS mods FROM (
               SELECT c.author_id AS aid, ch.path AS path, ch.commit_id AS cid
               FROM changes ch JOIN commits c ON c.id = ch.commit_id
               WHERE ch.repo_id = ?
               GROUP BY c.author_id, ch.path, ch.commit_id
               HAVING SUM(ch.added) + SUM(ch.removed) > 0
           ) GROUP BY aid, path""",
        (repo_id,),
    ):
        key = (root_of(r["aid"]), r["path"])
        per.setdefault(key, [0, 0, 0])[2] += r["mods"]
    agg_secs = time.time() - t0
    print(f"[{repo_name}] aggregates built in {agg_secs:.1f}s "
          f"({len(per):,} author-path entries)")

    # Per-commit modification counts for directories / repository: a commit
    # counts once for directory D if any of its changed paths with per-path
    # churn > 0 lies inside the subtree of D (files inside D do not each
    # count separately).
    t0 = time.time()
    keep_dirs = ref_dirs | {""}
    dir_mods = defaultdict(lambda: defaultdict(int))
    last = [None, None, None]  # cid, aid, ancestor set

    def flush():
        cid, aid, anc = last
        if cid is not None:
            for d in anc:
                if d in keep_dirs:
                    dir_mods[aid][d] += 1

    for r in conn.execute(
        """SELECT c.id AS cid, c.author_id AS aid, ch.path AS path
           FROM changes ch JOIN commits c ON c.id = ch.commit_id
           WHERE ch.repo_id = ?
           GROUP BY c.id, ch.path
           HAVING SUM(ch.added) + SUM(ch.removed) > 0
           ORDER BY c.id""",
        (repo_id,),
    ):
        if r["cid"] != last[0]:
            flush()
            last[0], last[1], last[2] = r["cid"], root_of(r["aid"]), {""}
        p = r["path"]
        idx = p.find("/")
        while idx != -1:
            last[2].add(p[:idx])
            idx = p.find("/", idx + 1)
    flush()
    all_dir_mods = {}
    for m in dir_mods.values():
        for d, c2 in m.items():
            all_dir_mods[d] = all_dir_mods.get(d, 0) + c2
    dir_secs = time.time() - t0
    print(f"[{repo_name}] commit-level directory modifications computed in "
          f"{dir_secs:.1f}s")

    authors = {r["id"]: f"{r['name']} <{r['email']}>"
               for r in conn.execute("SELECT id, name, email FROM authors WHERE repo_id = ?",
                                     (repo_id,))}
    by_ident = {}
    for raw_id, root_id in rmap.items():
        by_ident[authors[root_id]] = root_id

    # per-author sorted arrays + totals
    aid_items = defaultdict(list)
    for (aid, path), v in per.items():
        aid_items[aid].append((path, v))
    aid_data = {}
    aid_totals = {}
    for aid, items in aid_items.items():
        items.sort()
        aid_data[aid] = (items, [p for p, _ in items])
        aid_totals[aid] = [sum(v[i] for _, v in items) for i in range(3)]

    # global (ALL) totals
    gmap = {}
    gtot = [0, 0, 0]
    for (aid, path), v in per.items():
        g = gmap.setdefault(path, [0, 0, 0])
        for i in range(3):
            g[i] += v[i]
    for v in gmap.values():
        for i in range(3):
            gtot[i] += v[i]
    g_sorted = sorted(gmap.items())
    g_items = g_sorted
    g_paths = [p for p, _ in g_sorted]

    def subtree(items, paths, prefix):
        lo = bisect.bisect_left(paths, prefix + "/")
        hi = bisect.bisect_left(paths, prefix + chr(ord("/") + 1))
        a = r = m = 0
        for _, v in items[lo:hi]:
            a += v[0]
            r += v[1]
            m += v[2]
        return [a, r, m]

    def lookup(items, paths, path):
        i = bisect.bisect_left(paths, path)
        if i < len(paths) and paths[i] == path:
            return items[i][1]
        return [0, 0, 0]

    def object_values(object_type, path, author):
        if author == "ALL":
            items, paths, total = g_items, g_paths, gtot
            aid = None
        else:
            aid = by_ident.get(author)
            if aid is None:
                return None
            items, paths = aid_data.get(aid, ([], []))
            total = aid_totals.get(aid, [0, 0, 0])
        if object_type == "repository":
            base = list(total)
            key = ""
        elif object_type == "directory":
            base = subtree(items, paths, path)
            key = path
        elif object_type == "file":
            base = list(lookup(items, paths, path))
            key = None
        else:
            raise ValueError(f"unknown object_type {object_type!r}")
        # modifications: files use per-(author, path) distinct commit counts
        # (already in ``base``); directories / repository use the commit-level
        # counts built above so a multi-file commit counts once.
        if key is not None:
            if aid is None:
                base[2] = all_dir_mods.get(key, 0)
            else:
                base[2] = dir_mods.get(aid, {}).get(key, 0)
        return base

    # ------------------------------------------------------------------
    # compare every reference row
    # ------------------------------------------------------------------
    t0 = time.time()
    total = len(rows)
    failed = []
    col_fail = defaultdict(int)
    for r in rows:
        got = object_values(r["object_type"], r["path"], r["author"])
        if got is None:
            failed.append((r, "identification", "author not present in engine"))
            col_fail["(author identification)"] += 1
            continue
        vals = {"added": got[0], "removed": got[1], "modifications": got[2]}
        vals["growth"] = got[0] - got[1]
        vals["churn"] = got[0] + got[1]
        if r["author"] == "ALL":
            vals["modification_frequency"] = (got[2] / n) if n else 0.0
            vals["churn_rate"] = (vals["churn"] / n) if n else 0.0
        else:
            tot = object_values(r["object_type"], r["path"], "ALL")
            tot_churn = tot[0] + tot[1]
            vals["ownership"] = (vals["churn"] / tot_churn) if tot_churn else 0.0
        for col in INT_COLS + FLOAT_COLS:
            ref = r[col]
            if ref == "":
                continue
            if col in INT_COLS:
                if int(ref) != int(vals[col]):
                    failed.append((r, col, f"engine {int(vals[col])} vs reference {int(ref)}"))
                    col_fail[col] += 1
            else:
                ref_f = float(ref)
                if abs(ref_f - vals[col]) > TOL * max(1.0, abs(ref_f)):
                    failed.append((r, col, f"engine {vals[col]!r} vs reference {ref}"))
                    col_fail[col] += 1
    cmp_secs = time.time() - t0

    extra = sorted(set(gmap) - ref_files)
    print(f"[{repo_name}] compared {total:,} reference rows in {cmp_secs:.1f}s")
    if extra:
        print(f"[{repo_name}] note: {len(extra)} engine file paths absent from the "
              f"reference rows, e.g. {extra[:5]}")

    if failed:
        print(f"[{repo_name}] MISMATCHES: {len(failed):,} "
              f"(by column: {dict(col_fail)})")
        for r, col, msg in failed[:25]:
            auth = r["author"][:42]
            print(f"    {r['object_type']:<10} {r['path'][:48]:<48} {auth:<42} "
                  f"{col}: {msg}")
        if len(failed) > 25:
            print(f"    ... and {len(failed) - 25:,} more")
        return 1

    print(f"[{repo_name}] RESULT: PASS -- all {total:,} rows match "
          f"(integers exact, fractions within {TOL:g})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
