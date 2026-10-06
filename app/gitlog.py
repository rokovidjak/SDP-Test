"""Streaming parser for ``git log --numstat -z`` output.

The byte format was verified empirically against git 2.43 on a synthetic
repository exercising renames, binary files, deletions and empty commits:

- every commit record starts with 0x01
- the header holds NUL-separated fields -- hash, parents, author name
  (raw), author email (raw), committer unix timestamp, subject --
  terminated by 0x02
- file entries follow, NUL terminated:
    ``added\\tremoved\\tpath``         normal change
    ``added\\tremoved\\t\\0old\\0new`` rename (50% detection; the new path is used)
    ``-\\t-\\tpath``                   binary file (never measured)
- commits with no file changes (empty commits) have no entries

Only non-merge commits reachable from the reference are emitted, matching
the definition of H-bar in the test brief.

Raw identities (``%an``/``%ae``, not the mailmap-resolved ``%aN``/``%aE``)
are stored so the ingest layer can apply ``.mailmap`` itself via
git-check-mailmap; the resulting merge state stays visible and managable
in the dashboard.
"""
from __future__ import annotations

import subprocess


class ParseError(RuntimeError):
    pass


class CancelledError(RuntimeError):
    pass


LOG_FORMAT = "%x01%H%x00%P%x00%an%x00%ae%x00%ct%x00%s%x02"


def _run_git(repo_path: str, argv):
    proc = subprocess.run(["git", "-C", repo_path, *argv],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()
        raise ParseError(msg or f"git {' '.join(argv)} failed")
    return proc.stdout


def commit_count(repo_path: str, ref: str = "HEAD") -> int:
    """Number of non-merge commits reachable from ``ref`` (the size of H-bar)."""
    out = _run_git(repo_path, ["rev-list", "--no-merges", "--count", ref])
    return int(out.decode().strip() or "0")


def _parse_record(rec: bytes):
    header, sep, rest = rec.partition(b"\x02")
    if not sep:
        return None
    parts = header.split(b"\x00")
    if len(parts) < 6:
        return None
    try:
        ts = int(parts[4])
    except ValueError:
        return None

    if rest.startswith(b"\x00"):
        rest = rest[1:]
    if rest.startswith(b"\n"):
        rest = rest[1:]

    entries = []
    segs = rest.split(b"\x00")
    i, n = 0, len(segs)
    while i < n:
        seg = segs[i]
        i += 1
        if not seg:
            continue
        if seg.endswith(b"\t") and seg.count(b"\t") == 2:
            # rename marker "added\tremoved\t" followed by old and new path
            if i + 1 >= n:
                break
            nums = seg[:-1].split(b"\t", 1)
            path_bytes = segs[i + 1]
            i += 2
        else:
            split = seg.split(b"\t", 2)
            if len(split) != 3:
                continue
            nums = split[:2]
            path_bytes = split[2]
        if nums[0] == b"-" or nums[1] == b"-":
            continue  # binary file: not measured
        try:
            added, removed = int(nums[0]), int(nums[1])
        except ValueError:
            continue
        entries.append((path_bytes.decode("utf-8", "replace"), added, removed))

    return {
        "hash": parts[0].decode("ascii", "replace"),
        "parent": parts[1].decode("ascii", "replace"),
        "name": parts[2].decode("utf-8", "replace"),
        "email": parts[3].decode("utf-8", "replace"),
        "ts": ts,
        "subject": parts[5].decode("utf-8", "replace"),
        "changes": entries,
    }


def iter_commits(repo_path: str, ref: str = "HEAD", chunk_size: int = 1 << 20,
                 should_cancel=None):
    """Yield parsed commit dicts, streaming from a single ``git log`` process."""
    cmd = ["git", "-C", repo_path, "log", "--no-merges", "-M50%", "--numstat",
           "-z", f"--format={LOG_FORMAT}", ref]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err = b""
    try:
        buf = b""
        while True:
            if should_cancel and should_cancel():
                raise CancelledError("cancelled")
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            buf += chunk
            parts = buf.split(b"\x01")
            buf = parts.pop()
            for part in parts:
                if part:
                    rec = _parse_record(part)
                    if rec:
                        yield rec
        if buf:
            rec = _parse_record(buf)
            if rec:
                yield rec
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        err = proc.stderr.read() if proc.stderr else b""
        proc.wait()
        if proc.stderr:
            proc.stderr.close()
    if proc.returncode not in (0, -9) and err:
        raise ParseError(err.decode("utf-8", "replace").strip())
