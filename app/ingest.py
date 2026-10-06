"""Repository ingestion: zip upload / remote clone, then analysis into SQLite.

Each repository is processed on a background thread so the HTTP API stays
responsive (cloning Redis or git.git takes a while). Progress is written to
the ``repos`` row and polled by the dashboard.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import zipfile

from . import gitlog, mailmap, store

_CANCEL = {}
_LOCK = threading.Lock()

INSERT_CHANGES = ("INSERT INTO changes (repo_id, commit_id, path, added, removed)"
                  " VALUES (?, ?, ?, ?, ?)")


def _register(repo_id) -> threading.Event:
    ev = threading.Event()
    with _LOCK:
        _CANCEL[repo_id] = ev
    return ev


def _unregister(repo_id) -> None:
    with _LOCK:
        _CANCEL.pop(repo_id, None)


def cancel(repo_id) -> bool:
    with _LOCK:
        ev = _CANCEL.get(repo_id)
    if ev is not None:
        ev.set()
        return True
    return False


def start_ingest(repo_id: int, target_dir: str, kind: str, source: str) -> None:
    """Spawn the background worker for a repo row (kind: 'zip' | 'url')."""
    ev = _register(repo_id)
    threading.Thread(
        target=_worker, args=(repo_id, target_dir, kind, source, ev), daemon=True
    ).start()


def _worker(repo_id, target_dir, kind, source, ev) -> None:
    conn = store.connect()
    try:
        store.init_db(conn)
        if kind == "zip":
            store.update_repo(conn, repo_id, status="ingesting", progress=0.0,
                              progress_label="Extracting archive...")
            repo_path = _extract_zip(source, target_dir)
            try:
                os.remove(source)
            except OSError:
                pass
        else:
            store.update_repo(conn, repo_id, status="ingesting", progress=0.0,
                              progress_label="Cloning repository...")
            repo_path = _clone(source, target_dir, ev)
        if ev.is_set():
            raise gitlog.CancelledError("cancelled")
        store.update_repo(conn, repo_id, path=repo_path)
        _analyze(conn, repo_id, repo_path, ev)
        store.update_repo(conn, repo_id, status="ready", progress=100.0,
                          progress_label="Ready")
    except gitlog.CancelledError:
        store.clear_repo_data(conn, repo_id)
        shutil.rmtree(target_dir, ignore_errors=True)
        store.update_repo(conn, repo_id, status="cancelled", progress=0.0,
                          progress_label="Cancelled")
    except Exception as exc:  # noqa: BLE001 - surface any ingest failure to the UI
        store.clear_repo_data(conn, repo_id)
        store.update_repo(conn, repo_id, status="error", error=str(exc)[:500],
                          progress_label="Failed")
    finally:
        _unregister(repo_id)
        conn.close()


# --------------------------------------------------------------------------
# ingestion sources
# --------------------------------------------------------------------------

def _extract_zip(zip_path, target_dir):
    os.makedirs(target_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                name = member.filename
                if os.path.isabs(name) or ".." in name.split("/"):
                    raise ValueError(f"unsafe path in archive: {name}")
            zf.extractall(target_dir)
    except zipfile.BadZipFile as exc:
        raise ValueError("The uploaded file is not a valid zip archive") from exc
    root = _find_repo_root(target_dir)
    if root is None:
        raise ValueError("The archive does not contain a git repository (no .git found)")
    return root


def _find_repo_root(d):
    if os.path.exists(os.path.join(d, ".git")):
        return d
    for entry in sorted(os.listdir(d)):
        sub = os.path.join(d, entry)
        if os.path.isdir(sub) and os.path.exists(os.path.join(sub, ".git")):
            return sub
    return None


def _clone(url, target_dir, ev):
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir, ignore_errors=True)
    proc = subprocess.Popen(
        ["git", "clone", "--quiet", "--", url, target_dir],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    while proc.poll() is None:
        if ev.is_set():
            proc.kill()
            proc.wait()
            raise gitlog.CancelledError("cancelled")
        time.sleep(0.3)
    if proc.returncode != 0:
        err = proc.stderr.read().decode("utf-8", "replace").strip()
        shutil.rmtree(target_dir, ignore_errors=True)
        raise RuntimeError(f"git clone failed: {err.splitlines()[-1] if err else 'unknown error'}")
    if not os.path.exists(os.path.join(target_dir, ".git")):
        raise ValueError("clone did not produce a git repository")
    return target_dir


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def _analyze(conn, repo_id, repo_path, ev):
    ref = "HEAD"
    total = gitlog.commit_count(repo_path, ref)
    store.update_repo(conn, repo_id, status="analyzing", progress=0.0,
                      progress_label=f"Analyzing {total:,} commits...",
                      commit_count=0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = OFF")

    author_ids = {}
    pending = []
    seen = 0
    for rec in gitlog.iter_commits(repo_path, ref, should_cancel=ev.is_set):
        key = (rec["name"], rec["email"])
        aid = author_ids.get(key)
        if aid is None:
            aid = _get_or_create_author(conn, repo_id, rec["name"], rec["email"])
            author_ids[key] = aid
        cur = conn.execute(
            "INSERT INTO commits (repo_id, hash, parent, subject, author_id, committer_ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (repo_id, rec["hash"], rec["parent"] or None, rec["subject"], aid, rec["ts"]),
        )
        cid = cur.lastrowid
        for path, added, removed in rec["changes"]:
            pending.append((repo_id, cid, path, added, removed))
        seen += 1
        if len(pending) >= 20000:
            conn.executemany(INSERT_CHANGES, pending)
            pending.clear()
        if seen % 5000 == 0:
            conn.commit()
            store.update_repo(
                conn, repo_id,
                progress=round(100.0 * seen / max(total, 1), 1),
                progress_label=f"Analyzing {seen:,} / {total:,} commits",
                commit_count=seen,
            )
            if ev.is_set():
                raise gitlog.CancelledError("cancelled")
    if pending:
        conn.executemany(INSERT_CHANGES, pending)
        pending.clear()
    conn.commit()

    store.update_repo(conn, repo_id,
                      progress_label="Applying mailmap (author merging)...")
    _apply_mailmap(conn, repo_id, repo_path)

    conn.execute("PRAGMA synchronous = NORMAL")
    store.update_repo(conn, repo_id, commit_count=seen, progress=100.0)


def _get_or_create_author(conn, repo_id, name, email):
    conn.execute(
        "INSERT OR IGNORE INTO authors (repo_id, name, email) VALUES (?, ?, ?)",
        (repo_id, name, email),
    )
    row = conn.execute(
        "SELECT id FROM authors WHERE repo_id = ? AND name = ? AND email = ?",
        (repo_id, name, email),
    ).fetchone()
    return row["id"]


def _ensure_mailmap_file(repo_path) -> None:
    """Make sure the worktree has a .mailmap (zips may lack checked-out files)."""
    mm = os.path.join(repo_path, ".mailmap")
    if os.path.exists(mm):
        return
    proc = subprocess.run(["git", "-C", repo_path, "show", "HEAD:.mailmap"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode == 0 and proc.stdout:
        with open(mm, "wb") as fh:
            fh.write(proc.stdout)


def _apply_mailmap(conn, repo_id, repo_path) -> None:
    _ensure_mailmap_file(repo_path)
    rows = conn.execute(
        "SELECT id, name, email FROM authors WHERE repo_id = ?", (repo_id,)
    ).fetchall()
    by_ident = {(r["name"], r["email"]): r["id"] for r in rows}
    mapping = mailmap.resolve_identities(repo_path, by_ident.keys())
    pairs = {}
    for ident, canonical in mapping.items():
        if canonical == ident:
            continue
        src = by_ident.get(ident)
        if src is None:
            continue
        tgt = by_ident.get(canonical)
        if tgt is None:
            conn.execute(
                "INSERT OR IGNORE INTO authors (repo_id, name, email, synthetic)"
                " VALUES (?, ?, ?, 1)",
                (repo_id, canonical[0], canonical[1]),
            )
            tgt = conn.execute(
                "SELECT id FROM authors WHERE repo_id = ? AND name = ? AND email = ?",
                (repo_id, canonical[0], canonical[1]),
            ).fetchone()["id"]
            by_ident[canonical] = tgt
        pairs[src] = tgt
    if pairs:
        store.apply_author_merges(conn, repo_id, pairs)
