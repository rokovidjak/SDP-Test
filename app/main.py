"""FastAPI application: serves the dashboard and the JSON API."""
from __future__ import annotations

import os
import shutil
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import ingest, metrics, store

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")


@asynccontextmanager
async def lifespan(_app):
    conn = store.connect()
    try:
        store.init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="Repo Analysis Tool", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def get_conn():
    conn = store.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# --------------------------------------------------------------------------
# repositories
# --------------------------------------------------------------------------

def _repo_json(r):
    return {
        "id": r["id"], "name": r["name"], "source_type": r["source_type"],
        "source": r["source"], "status": r["status"], "progress": r["progress"],
        "progress_label": r["progress_label"], "error": r["error"],
        "commit_count": r["commit_count"], "created_at": r["created_at"],
    }


def _get_repo_or_404(conn, repo_id):
    repo = store.get_repo(conn, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="Unknown repository")
    return repo


@app.get("/api/repos")
def list_repos(conn=Depends(get_conn)):
    return [_repo_json(r) for r in store.list_repos(conn)]


@app.post("/api/repos/clone")
def clone_repo(payload: dict, conn=Depends(get_conn)):
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Repository URL is required")
    supported = url.startswith(("http://", "https://", "git://", "ssh://", "file://"))
    scp_like = ("@" in url and ":" in url and not url.startswith(("http://", "https://")))
    if not (supported or scp_like):
        raise HTTPException(status_code=400, detail="Unsupported repository URL")
    name = url.rstrip("/").rsplit("/", 1)[-1] or "repository"
    if name.endswith(".git"):
        name = name[:-4]
    repo_id = store.create_repo(conn, name, "url", url)
    target = os.path.join(store.repos_dir(), str(repo_id))
    ingest.start_ingest(repo_id, target, "url", url)
    return {"id": repo_id}


@app.post("/api/repos/zip")
def upload_zip(file: UploadFile = File(...), conn=Depends(get_conn)):
    filename = file.filename or "archive.zip"
    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=400,
            detail="Please upload a .zip archive of the repository (including .git)",
        )
    tmp_dir = os.path.join(store.data_dir(), "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, f"upload-{int(time.time() * 1000)}-{filename}")
    with open(tmp_path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    repo_id = store.create_repo(conn, filename[:-4], "zip", filename)
    target = os.path.join(store.repos_dir(), str(repo_id))
    ingest.start_ingest(repo_id, target, "zip", tmp_path)
    return {"id": repo_id}


@app.post("/api/repos/{repo_id}/cancel")
def cancel_repo(repo_id: int, conn=Depends(get_conn)):
    _get_repo_or_404(conn, repo_id)
    ingest.cancel(repo_id)
    return {"ok": True}


@app.delete("/api/repos/{repo_id}")
def delete_repo(repo_id: int, conn=Depends(get_conn)):
    _get_repo_or_404(conn, repo_id)
    ingest.cancel(repo_id)
    store.delete_repo(conn, repo_id)
    shutil.rmtree(os.path.join(store.repos_dir(), str(repo_id)), ignore_errors=True)
    return {"ok": True}


# --------------------------------------------------------------------------
# authors
# --------------------------------------------------------------------------

@app.get("/api/repos/{repo_id}/authors")
def list_authors(repo_id: int, conn=Depends(get_conn)):
    _get_repo_or_404(conn, repo_id)
    rows = conn.execute(
        "SELECT id, name, email, synthetic FROM authors WHERE repo_id = ? ORDER BY id",
        (repo_id,),
    ).fetchall()
    # One aggregate for all authors instead of a per-author correlated count
    # (matters on large repos with thousands of identities).
    counts = {
        r["author_id"]: r["n"]
        for r in conn.execute(
            "SELECT author_id, COUNT(*) AS n FROM commits"
            " WHERE repo_id = ? GROUP BY author_id",
            (repo_id,),
        )
    }
    by_id = {r["id"]: r for r in rows}
    rmap = store.author_root_map(conn, repo_id)
    merged = {}
    for r in rows:
        root = rmap[r["id"]]
        n = counts.get(r["id"], 0)
        m = merged.setdefault(root, {"id": root, "commit_count": 0, "identities": []})
        m["commit_count"] += n
        m["identities"].append({
            "id": r["id"], "name": r["name"], "email": r["email"],
            "synthetic": bool(r["synthetic"]), "commit_count": n,
        })
    for m in merged.values():
        base = by_id.get(m["id"])
        m["name"] = base["name"] if base else "(merged)"
        m["email"] = base["email"] if base else ""
        m["identities"].sort(key=lambda x: -x["commit_count"])
    return {"authors": sorted(merged.values(), key=lambda x: -x["commit_count"])}


@app.post("/api/repos/{repo_id}/authors/merge")
def merge_authors(repo_id: int, payload: dict, conn=Depends(get_conn)):
    _get_repo_or_404(conn, repo_id)
    if payload.get("target_id") is None:
        raise HTTPException(status_code=400, detail="target_id is required")
    target_id = int(payload["target_id"])
    source_ids = [int(i) for i in (payload.get("source_ids") or [])]
    pairs = {sid: target_id for sid in source_ids if sid != target_id}
    store.apply_author_merges(conn, repo_id, pairs)
    return {"ok": True}


@app.post("/api/repos/{repo_id}/authors/unmerge")
def unmerge_author(repo_id: int, payload: dict, conn=Depends(get_conn)):
    _get_repo_or_404(conn, repo_id)
    if payload.get("author_id") is None:
        raise HTTPException(status_code=400, detail="author_id is required")
    conn.execute("UPDATE authors SET merged_into = NULL WHERE id = ? AND repo_id = ?",
                 (int(payload["author_id"]), repo_id))
    conn.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def _make_filters(conn, repo_id, since, until, commits_csv, authors_csv):
    hashes = [h.strip() for h in commits_csv.split(",") if h.strip()] if commits_csv else []
    root_ids = [int(a) for a in authors_csv.split(",") if a.strip()] if authors_csv else []
    author_ids = store.expand_author_filter(conn, repo_id, root_ids) if root_ids else []
    return metrics.Filters(since=since, until=until, commit_hashes=hashes,
                           author_ids=author_ids)


@app.get("/api/repos/{repo_id}/info")
def repo_info(repo_id: int, conn=Depends(get_conn)):
    _get_repo_or_404(conn, repo_id)
    return metrics.repo_info(conn, repo_id)


@app.get("/api/repos/{repo_id}/metrics")
def object_metrics(repo_id: int, kind: str = "root", path: str = "",
                   since: float | None = None, until: float | None = None,
                   commits: str = "", authors: str = "", conn=Depends(get_conn)):
    _get_repo_or_404(conn, repo_id)
    f = _make_filters(conn, repo_id, since, until, commits, authors)
    return metrics.object_metrics(conn, repo_id, kind, path, f)


@app.get("/api/repos/{repo_id}/children")
def children(repo_id: int, kind: str = "root", path: str = "",
             metric: str = "churn", limit: int = 100,
             since: float | None = None, until: float | None = None,
             commits: str = "", authors: str = "", conn=Depends(get_conn)):
    _get_repo_or_404(conn, repo_id)
    f = _make_filters(conn, repo_id, since, until, commits, authors)
    return metrics.children(conn, repo_id, kind, path, f, metric=metric, limit=limit)


@app.get("/api/repos/{repo_id}/timeline")
def timeline(repo_id: int, since: float | None = None, until: float | None = None,
             commits: str = "", authors: str = "", conn=Depends(get_conn)):
    _get_repo_or_404(conn, repo_id)
    f = _make_filters(conn, repo_id, since, until, commits, authors)
    return metrics.timeline(conn, repo_id, f)


@app.get("/api/repos/{repo_id}/author_breakdown")
def author_breakdown(repo_id: int, kind: str = "root", path: str = "",
                     since: float | None = None, until: float | None = None,
                     commits: str = "", authors: str = "", conn=Depends(get_conn)):
    _get_repo_or_404(conn, repo_id)
    f = _make_filters(conn, repo_id, since, until, commits, authors)
    return metrics.author_breakdown(conn, repo_id, kind, path, f)


@app.get("/api/repos/{repo_id}/commits")
def commits(repo_id: int, limit: int = 50, offset: int = 0, q: str = "",
            since: float | None = None, until: float | None = None,
            commits: str = "", authors: str = "", conn=Depends(get_conn)):
    _get_repo_or_404(conn, repo_id)
    f = _make_filters(conn, repo_id, since, until, commits, authors)
    return metrics.commit_list(conn, repo_id, f, limit=limit, offset=offset, query=q)


@app.get("/api/repos/{repo_id}/paths")
def paths(repo_id: int, q: str = "", limit: int = 20000, conn=Depends(get_conn)):
    _get_repo_or_404(conn, repo_id)
    return metrics.paths(conn, repo_id, query=q, limit=limit)
