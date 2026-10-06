"""SQLite storage layer for the Repo Analysis Tool.

Holds the schema and small helpers shared by ingestion, metrics and the API.
Every table is keyed by ``repo_id`` so one database file serves any number
of repositories.
"""
from __future__ import annotations

import os
import sqlite3
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,
    source_type      TEXT    NOT NULL DEFAULT 'url',
    source           TEXT,
    ref              TEXT    NOT NULL DEFAULT 'HEAD',
    path             TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT 'pending',
    progress         REAL    NOT NULL DEFAULT 0,
    progress_label   TEXT    NOT NULL DEFAULT '',
    error            TEXT,
    commit_count     INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS authors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id     INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    email       TEXT    NOT NULL,
    merged_into INTEGER,
    synthetic   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (repo_id, name, email)
);

CREATE TABLE IF NOT EXISTS commits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id      INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    hash         TEXT    NOT NULL,
    parent       TEXT,
    subject      TEXT    NOT NULL DEFAULT '',
    author_id    INTEGER NOT NULL REFERENCES authors(id),
    committer_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commits_repo_ts     ON commits(repo_id, committer_ts);
CREATE INDEX IF NOT EXISTS idx_commits_repo_hash   ON commits(repo_id, hash);
CREATE INDEX IF NOT EXISTS idx_commits_repo_author ON commits(repo_id, author_id);

CREATE TABLE IF NOT EXISTS changes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id   INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    commit_id INTEGER NOT NULL REFERENCES commits(id) ON DELETE CASCADE,
    path      TEXT    NOT NULL,
    added     INTEGER NOT NULL,
    removed   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_commit    ON changes(commit_id);
CREATE INDEX IF NOT EXISTS idx_changes_repo_path ON changes(repo_id, path);
"""


def data_dir() -> str:
    d = os.environ.get("RAT_DATA_DIR") or os.path.join(PROJECT_ROOT, "data")
    os.makedirs(d, exist_ok=True)
    return d


def db_path() -> str:
    return os.path.join(data_dir(), "rat.db")


def repos_dir() -> str:
    d = os.path.join(data_dir(), "repos")
    os.makedirs(d, exist_ok=True)
    return d


def connect() -> sqlite3.Connection:
    # check_same_thread=False: FastAPI runs sync endpoints and sync
    # dependencies on a worker-thread pool, so a request's connection may be
    # created, used and closed on different pool threads during its lifetime.
    # Usage is still strictly sequential -- one connection per request.
    conn = sqlite3.connect(db_path(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------
# repos
# --------------------------------------------------------------------------

def create_repo(conn, name, source_type, source) -> int:
    cur = conn.execute(
        "INSERT INTO repos (name, source_type, source, created_at) VALUES (?, ?, ?, ?)",
        (name, source_type, source, int(time.time())),
    )
    conn.commit()
    return cur.lastrowid


def get_repo(conn, repo_id):
    return conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()


def list_repos(conn):
    return conn.execute("SELECT * FROM repos ORDER BY id").fetchall()


def update_repo(conn, repo_id, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE repos SET {cols} WHERE id = ?", (*fields.values(), repo_id))
    conn.commit()


def clear_repo_data(conn, repo_id) -> None:
    """Remove analysis data for a repo but keep the repo row itself."""
    conn.execute("DELETE FROM changes WHERE repo_id = ?", (repo_id,))
    conn.execute("DELETE FROM commits WHERE repo_id = ?", (repo_id,))
    conn.execute("DELETE FROM authors WHERE repo_id = ?", (repo_id,))
    conn.commit()


def delete_repo(conn, repo_id) -> None:
    clear_repo_data(conn, repo_id)
    conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
    conn.commit()


# --------------------------------------------------------------------------
# authors
# --------------------------------------------------------------------------

def author_root_map(conn, repo_id) -> dict:
    """Return ``{author_id: root_author_id}`` with merge chains flattened."""
    rows = conn.execute(
        "SELECT id, merged_into FROM authors WHERE repo_id = ?", (repo_id,)
    ).fetchall()
    parent = {r["id"]: (r["merged_into"] or r["id"]) for r in rows}
    roots = {}
    for aid in parent:
        cur, seen = aid, set()
        while parent.get(cur, cur) != cur and cur not in seen:
            seen.add(cur)
            cur = parent[cur]
        roots[aid] = cur
    return roots


def expand_author_filter(conn, repo_id, root_ids):
    """Map selected canonical author ids to all raw author ids behind them."""
    rmap = author_root_map(conn, repo_id)
    wanted = {int(a) for a in root_ids}
    return sorted(aid for aid, root in rmap.items() if root in wanted)


def apply_author_merges(conn, repo_id, pairs) -> None:
    """Merge authors.

    ``pairs`` maps child author id -> target author id. Chains are flattened
    so every merged author points directly at a single root author, which is
    what metrics grouping and the UI rely on.
    """
    pairs = {int(c): int(t) for c, t in pairs.items() if int(c) != int(t)}
    if not pairs:
        return
    rows = conn.execute(
        "SELECT id, merged_into FROM authors WHERE repo_id = ?", (repo_id,)
    ).fetchall()
    parent = {r["id"]: (r["merged_into"] or r["id"]) for r in rows}

    def root(aid):
        seen = set()
        while parent.get(aid, aid) != aid and aid not in seen:
            seen.add(aid)
            aid = parent[aid]
        return aid

    for child, target in pairs.items():
        if child not in parent or target not in parent:
            continue
        croot, troot = root(child), root(target)
        if croot == troot:
            continue
        for aid in list(parent.keys()):
            if root(aid) == croot:
                parent[aid] = troot

    for aid, par in parent.items():
        conn.execute(
            "UPDATE authors SET merged_into = ? WHERE id = ? AND repo_id = ?",
            (None if aid == par else par, aid, repo_id),
        )
    conn.commit()
