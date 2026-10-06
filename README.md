# Repo Analysis Tool (RAT)

A web-app dashboard that measures **git repository metrics** from a single
pass over the commit history. Point it at a repository (clone a URL or upload
a zip containing `.git`), then filter by **time window**, **author**, and
**explicit commit set**, and drill from the repository root down into
directories and files.

Every number on the dashboard follows the metric definitions in the
project brief exactly — the same definitions used by the automated engine
tests in `tests/`.

---

## Quick start

Requirements: Python 3.10+, git 2.30+ (Linux, macOS, or WSL).

```bash
bash start.sh        # equivalent: ./run.sh
```

On first run this creates `.venv`, installs the three dependencies
(`fastapi`, `uvicorn`, `python-multipart`), and starts the server:

```
Repo Analysis Tool running at http://localhost:8000
```

Open **http://localhost:8000** in a browser.

Manual equivalent:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```

Analysis data lives in `data/` (SQLite database + cloned repositories) and
is git-ignored. Delete that folder for a clean slate. Use `RAT_DATA_DIR` to
store it elsewhere and `PORT` to change the port.

---

## Using the dashboard

1. **Add a repository** — "Add repository" in the header. Either paste a
   clone URL (`https://`, `ssh://`, scp-like `git@host:repo.git`, or a local
   `file:///path/to/repo`) or upload a `.zip` archive whose root contains the
   `.git` directory. Ingestion runs in the background; a progress bar tracks
   clone → analyse → ready, and can be cancelled.
2. **Read the KPIs** — the eight cards at the top show the current *object*
   (root, directory, or file) within the current *commit set* (filters).
3. **Drill down** — click any directory row in "Contents" to descend; the
   breadcrumb walks back up. Clicking a file row shows that file's metrics.
4. **Filter** — combine any of:
   * *Time window* — presets, exact `since` (inclusive) / `until` (exclusive)
     inputs, or "Use visible range" after zooming the timeline chart;
   * *Authors* — checkboxes in the Author dropdown (canonical authors after
     merging); clicking a donut slice toggles the same filter;
   * *Commit set* — paste hashes (comma/space/newline separated); the `+`
     button on any commit row sets the commit set to that commit.
   Active filters appear as removable chips. Everything recomputes instantly.
5. **Merge authors** — in "Author merging", select identities with the
   checkboxes and merge them into a canonical author. Merges can be undone
   with "Unmerge". Identities recorded in a `.mailmap` are merged
   automatically at ingest time (the file is read from the worktree, or
   materialised from `HEAD` for zips).

---

## Metric definitions

Implemented in `app/metrics.py`, following the brief:

* **H̄** — the non-merge commits reachable from the reference (`HEAD`).
  Merge commits are excluded everywhere.
* **Commit-set** — H̄ restricted by committer-time interval \([i, j)\)
  (`since` inclusive, `until` exclusive), an explicit hash list, and/or a
  set of (possibly merged) authors.
* **Object** — the whole repository (root), a directory (its entire path
  subtree), or one exact file path.

| Metric | Definition |
|---|---|
| Added / Removed | sum of lines added / removed by the commit set on the object |
| Growth | Added − Removed |
| Churn | Added + Removed |
| Modifications | commits in the set with churn > 0 on the object |
| Modification frequency | Modifications ÷ &#124;H&#124; |
| Churn rate | Churn ÷ &#124;H&#124; |
| Ownership (per author) | author churn ÷ total churn, within the commit set |

Semantics that matter:

* **Renames** are detected with 50 % similarity (`git log -M50%`); a rename
  is attributed to the *new* path. A pure rename is a 0/0 change and is
  **not** a modification.
* **Binary files** are ignored entirely — they never contribute added,
  removed, or modifications.
* **Deletions** count their lines as *removed* on the deleted path, so a
  file's Added−Removed can be negative (e.g. a deleted file).
* Directory metrics are exact sums over the whole subtree (index-friendly
  range scan, no `LIKE`).

---

## Architecture

```
Browser (vanilla JS + ECharts, no CDN)
   │  fetch /api/...
FastAPI (app/main.py) ── app/ingest.py  background workers
   │                        │  clone URL / unzip archive
   │                        ▼
   │                 app/gitlog.py   single streaming `git log`
   │                        │  --no-merges -M50% --numstat -z
   │                        ▼
   └───────────────► SQLite (app/store.py)   repos/authors/commits/changes
                     app/metrics.py   metric SQL + author merge logic
                     app/mailmap.py   .mailmap via `git check-mailmap`
```

* **One pass, one process** — the whole history is parsed from a single
  streaming `git log` invocation (custom NUL-delimited format, verified
  against raw bytes) and written to SQLite. Dashboards queries are then
  plain SQL aggregates — no repository re-reads.
* **Commit rows are stored only for H̄** (non-merge commits); the parser
  handles renames, binaries, deletions, and empty commits.
* **Author merging** is two-layer: `.mailmap` at ingest time
  (`git check-mailmap` per identity, canonical rows created synthetically if
  needed), plus manual merges stored as `authors.merged_into` with chains
  flattened to a single root. All metrics group by `COALESCE(merged_into,id)`.
* **Scale** — inserts are batched (20k rows per `executemany`, WAL,
  `synchronous=OFF` during ingest); the timeline auto-buckets per day above
  3000 points; the "modifications" column for directory children is computed
  only for the top children so huge directories stay responsive.

### Repository layout

```
app/main.py        FastAPI app: static hosting + JSON API
app/ingest.py      background ingestion workers (zip / clone / cancel)
app/gitlog.py      streaming parser for `git log --numstat -z`
app/store.py       SQLite schema + repo/author helpers
app/metrics.py     metric definitions and queries
app/mailmap.py     .mailmap resolution via git check-mailmap
static/            dashboard (index.html, style.css, app.js, charts.js)
static/vendor/     echarts.min.js (vendored, no network access needed)
tests/             synthetic fixture builder + engine tests
start.sh           one-command launcher (bash start.sh; run.sh is an alias)
```

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/repos` | list repositories with ingest status/progress |
| `POST /api/repos/clone` | `{"url": …}` → start cloning + analysing |
| `POST /api/repos/zip` | multipart `.zip` upload (must contain `.git`) |
| `POST /api/repos/{id}/cancel` | cancel a running ingest |
| `DELETE /api/repos/{id}` | delete repo + data |
| `GET /api/repos/{id}/authors` | canonical authors with their identities |
| `POST /api/repos/{id}/authors/merge` | `{"target_id", "source_ids":[…]}` |
| `POST /api/repos/{id}/authors/unmerge` | `{"author_id"}` detach identity |
| `GET /api/repos/{id}/info` | commit count, first/last timestamps, authors, files |
| `GET /api/repos/{id}/metrics` | the metric family for one object |
| `GET /api/repos/{id}/children` | immediate children table (drill-down) |
| `GET /api/repos/{id}/timeline` | per-commit (or per-day) added/removed series |
| `GET /api/repos/{id}/author_breakdown` | per-author churn/modifications/ownership |
| `GET /api/repos/{id}/commits` | paginated commit list (search, filters) |
| `GET /api/repos/{id}/paths` | distinct file paths + derived directories |

All metric endpoints accept `since` (epoch seconds), `until`, `commits`
(comma-separated hashes), `authors` (comma-separated canonical author ids),
plus `kind` (`root`/`dir`/`file`) and `path` for object scoping.

---

## Tests

The engine ships with a deterministic synthetic fixture and hand-computed
expectations (verified against raw `git log` output):

```bash
python3 tests/test_engine.py
```

Covers: H̄ size and merge exclusion · empty commits · pure rename (0/0, not a
modification) · rename below the 50 % threshold (delete+add) · binary
exclusion · deletions · file/directory/root metrics · `[since, until)`
boundary semantics · explicit commit sets · author filters · .mailmap
merging · manual merge/unmerge chains · timeline totals · zip ingest
round-trip and zip-slip rejection.

To build a copy of the fixture repository and inspect its raw git data:

```bash
python3 tests/make_synth_repos.py /tmp/synth
```

---

## AI usage declaration

This project was developed with AI assistance (the Qoder agent) as permitted
by the assignment rules. All architecture choices, metric definitions and
results were reviewed and validated: the engine test suite pins every metric
semantic to hand-computed values, and totals were cross-checked against
independent `git log` output on real repositories (cJSON clone and zip).
