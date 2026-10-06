"""Metric definitions from the test brief, expressed as SQL over ingested data.

Commit-set semantics follow the brief:

* ``H-bar`` is the set of non-merge commits reachable from the reference
  (baked in at ingest time: only non-merge commits are stored).
* ``H_t`` / ``H_i,j`` filter on committer timestamps; the interval is
  ``[i, j)`` -- ``since`` inclusive, ``until`` exclusive.
* An object is a file (exact path), a directory (path subtree), or the
  repository itself (the root directory).
* ``modifications`` counts commits whose churn on the object is > 0, so a
  pure rename (0 added / 0 removed) does not count as a modification.

Per-commit file metrics satisfy, by construction of the stored data:
growth = added - removed and churn = added + removed, so the commit-set
aggregates are plain sums.
"""
from __future__ import annotations


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


class Filters:
    """Composable commit-set filter: repository + time window + commit list + authors."""

    def __init__(self, since=None, until=None, commit_hashes=None, author_ids=None):
        self.since = since
        self.until = until
        self.commit_hashes = list(commit_hashes or [])
        self.author_ids = list(author_ids or [])

    def commit_where(self, repo_id):
        clauses = ["c.repo_id = ?"]
        params = [repo_id]
        if self.since is not None:
            clauses.append("c.committer_ts >= ?")
            params.append(int(self.since))
        if self.until is not None:
            clauses.append("c.committer_ts < ?")
            params.append(int(self.until))
        if self.commit_hashes:
            clauses.append(f"c.hash IN ({_placeholders(len(self.commit_hashes))})")
            params.extend(self.commit_hashes)
        if self.author_ids:
            clauses.append(f"c.author_id IN ({_placeholders(len(self.author_ids))})")
            params.extend(self.author_ids)
        return " AND ".join(clauses), params


def object_where(column: str, kind: str, path: str):
    """SQL predicate selecting a file, a directory subtree, or the root."""
    if kind == "root" or not path:
        return "1 = 1", []
    if kind == "file":
        return f"{column} = ?", [path]
    prefix = path.rstrip("/") + "/"
    # 'src/' -> 'src0': a plain range scan instead of LIKE, so the
    # (repo_id, path) index is used directly.
    upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    return f"({column} >= ? AND {column} < ?)", [prefix, upper]


def commit_set_size(conn, repo_id, f: Filters) -> int:
    where, params = f.commit_where(repo_id)
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM commits c WHERE {where}", params
    ).fetchone()["n"]


def object_metrics(conn, repo_id, kind, path, f: Filters) -> dict:
    """Added / removed / growth / churn / modifications / frequencies for one object."""
    cwhere, cparams = f.commit_where(repo_id)
    owhere, oparams = object_where("ch.path", kind, path)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(ch.added), 0)   AS added,
                   COALESCE(SUM(ch.removed), 0) AS removed,
                   COUNT(DISTINCT CASE WHEN ch.added + ch.removed > 0
                                       THEN ch.commit_id END) AS modifications
            FROM changes ch
            JOIN commits c ON c.id = ch.commit_id
            WHERE {cwhere} AND {owhere}""",
        [*cparams, *oparams],
    ).fetchone()
    size = commit_set_size(conn, repo_id, f)
    added, removed = row["added"], row["removed"]
    mods = int(row["modifications"] or 0)
    growth, churn = added - removed, added + removed
    return {
        "object": {"kind": kind, "path": path},
        "commit_set_size": size,
        "added": added,
        "removed": removed,
        "growth": growth,
        "churn": churn,
        "modifications": mods,
        "modification_frequency": (mods / size) if size else 0.0,
        "churn_rate": (churn / size) if size else 0.0,
    }


def author_breakdown(conn, repo_id, kind, path, f: Filters):
    """Per-author modifications, churn and ownership for one object."""
    cwhere, cparams = f.commit_where(repo_id)
    owhere, oparams = object_where("ch.path", kind, path)
    rows = conn.execute(
        f"""SELECT root.id    AS author_id,
                   root.name  AS name,
                   root.email AS email,
                   COALESCE(SUM(ch.added + ch.removed), 0) AS churn,
                   COUNT(DISTINCT CASE WHEN ch.added + ch.removed > 0
                                       THEN ch.commit_id END) AS modifications
            FROM changes ch
            JOIN commits c ON c.id = ch.commit_id
            JOIN authors a ON a.id = c.author_id
            JOIN authors root ON root.id = COALESCE(a.merged_into, a.id)
            WHERE {cwhere} AND {owhere}
            GROUP BY root.id
            ORDER BY churn DESC""",
        [*cparams, *oparams],
    ).fetchall()
    total_churn = sum(r["churn"] for r in rows)
    return [
        {
            "author_id": r["author_id"],
            "name": r["name"],
            "email": r["email"],
            "modifications": int(r["modifications"] or 0),
            "churn": r["churn"],
            "ownership": (r["churn"] / total_churn) if total_churn else 0.0,
        }
        for r in rows
    ]


def timeline(conn, repo_id, f: Filters, max_points: int = 3000):
    """Per-commit added/removed series; bucketed per day when very large."""
    cwhere, cparams = f.commit_where(repo_id)
    rows = conn.execute(
        f"""SELECT c.committer_ts AS ts,
                   SUM(ch.added)   AS added,
                   SUM(ch.removed) AS removed
            FROM changes ch
            JOIN commits c ON c.id = ch.commit_id
            WHERE {cwhere}
            GROUP BY c.id
            ORDER BY c.committer_ts""",
        cparams,
    ).fetchall()
    series = [{"ts": r["ts"], "added": r["added"], "removed": r["removed"]} for r in rows]
    if len(series) > max_points:
        buckets = {}
        for p in series:
            key = p["ts"] // 86400
            b = buckets.setdefault(key, {"ts": key * 86400, "added": 0, "removed": 0})
            b["added"] += p["added"]
            b["removed"] += p["removed"]
        series = [buckets[k] for k in sorted(buckets)]
    return series


def children(conn, repo_id, kind, path, f: Filters, metric: str = "churn",
             limit: int = 100, modifications_for: int = 40):
    """Immediate children (files and subdirectories) of a directory object.

    added/removed/growth/churn come from a single grouped query. The heavier
    modifications counter is computed only for the most relevant children
    (top ``modifications_for`` by churn) so that huge directories stay fast.
    """
    if kind == "file":
        return []
    base_kind = "root" if (kind == "root" or not path) else "dir"
    cwhere, cparams = f.commit_where(repo_id)
    owhere, oparams = object_where("ch.path", base_kind, path)
    rows = conn.execute(
        f"""SELECT ch.path AS path,
                   SUM(ch.added)   AS added,
                   SUM(ch.removed) AS removed
            FROM changes ch
            JOIN commits c ON c.id = ch.commit_id
            WHERE {cwhere} AND {owhere}
            GROUP BY ch.path""",
        [*cparams, *oparams],
    ).fetchall()

    prefix = (path.rstrip("/") + "/") if path else ""
    nodes = {}
    for r in rows:
        rest = r["path"][len(prefix):]
        head, sep, _ = rest.partition("/")
        key = (head, bool(sep))
        node = nodes.get(key)
        if node is None:
            node = nodes[key] = {
                "name": head,
                "kind": "dir" if sep else "file",
                "path": prefix + head,
                "added": 0,
                "removed": 0,
            }
        node["added"] += r["added"]
        node["removed"] += r["removed"]

    lst = list(nodes.values())
    for n in lst:
        n["growth"] = n["added"] - n["removed"]
        n["churn"] = n["added"] + n["removed"]
        n["modifications"] = None

    lst.sort(key=lambda n: n["churn"], reverse=True)
    for n in lst[:modifications_for]:
        m = object_metrics(conn, repo_id, n["kind"], n["path"], f)
        n["modifications"] = m["modifications"]

    if metric in ("added", "removed", "growth", "churn", "modifications"):
        lst.sort(key=lambda n: (n.get(metric) or 0), reverse=True)
    return lst[:limit]


def commit_list(conn, repo_id, f: Filters, limit: int = 50, offset: int = 0,
                query: str = ""):
    """Newest-first commit rows (with line stats) for the selected commit set."""
    cwhere, cparams = f.commit_where(repo_id)
    extra = ""
    params = list(cparams)
    if query:
        extra = " AND c.subject LIKE ?"
        params.append(f"%{query}%")
    ids = conn.execute(
        f"""SELECT c.id FROM commits c
            WHERE {cwhere}{extra}
            ORDER BY c.committer_ts DESC, c.id DESC
            LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    ).fetchall()
    if not ids:
        return []
    id_list = [r["id"] for r in ids]
    rows = conn.execute(
        f"""SELECT c.id, c.hash, c.committer_ts, c.subject,
                   root.name  AS author_name,
                   root.email AS author_email,
                   COALESCE(SUM(ch.added), 0)   AS added,
                   COALESCE(SUM(ch.removed), 0) AS removed
            FROM commits c
            JOIN authors a ON a.id = c.author_id
            JOIN authors root ON root.id = COALESCE(a.merged_into, a.id)
            LEFT JOIN changes ch ON ch.commit_id = c.id
            WHERE c.id IN ({_placeholders(len(id_list))})
            GROUP BY c.id
            ORDER BY c.committer_ts DESC, c.id DESC""",
        id_list,
    ).fetchall()
    return [dict(r) for r in rows]


def paths(conn, repo_id, query: str = "", limit: int = 20000):
    """Distinct file paths (and derived directories) for path-filter suggestions."""
    sql = "SELECT DISTINCT path FROM changes WHERE repo_id = ?"
    params = [repo_id]
    if query:
        sql += " AND path LIKE ?"
        params.append(f"%{query}%")
    sql += " ORDER BY path LIMIT ?"
    params.append(limit)
    files = [r["path"] for r in conn.execute(sql, params).fetchall()]
    dirs = set()
    for p in files:
        parts = p.split("/")[:-1]
        acc = ""
        for part in parts:
            acc = f"{acc}/{part}" if acc else part
            dirs.add(acc)
    return {"files": files, "dirs": sorted(dirs)}


def repo_info(conn, repo_id) -> dict:
    row = conn.execute(
        """SELECT COUNT(*) AS commits,
                  MIN(committer_ts) AS first_ts,
                  MAX(committer_ts) AS last_ts
           FROM commits WHERE repo_id = ?""",
        (repo_id,),
    ).fetchone()
    authors = conn.execute(
        "SELECT COUNT(*) AS n FROM authors WHERE repo_id = ? AND merged_into IS NULL",
        (repo_id,),
    ).fetchone()["n"]
    files = conn.execute(
        "SELECT COUNT(DISTINCT path) AS n FROM changes WHERE repo_id = ?",
        (repo_id,),
    ).fetchone()["n"]
    return {
        "commits": row["commits"],
        "first_ts": row["first_ts"],
        "last_ts": row["last_ts"],
        "authors": authors,
        "files": files,
    }
