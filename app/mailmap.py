"""Author identity resolution using git's own mailmap machinery.

``.mailmap`` support is delegated to ``git check-mailmap`` so we inherit
git's exact matching rules instead of re-implementing them. Identities that
are not remapped resolve to themselves.
"""
from __future__ import annotations

import subprocess

CHUNK = 500


def _fmt(name: str, email: str) -> str:
    return f"{name} <{email}>"


def _parse_contact(line: str):
    line = line.strip()
    if line.endswith(">"):
        idx = line.rfind("<")
        if idx > 0:
            return line[:idx].strip(), line[idx + 1:-1].strip()
    return None


def resolve_identities(repo_path: str, identities):
    """Resolve ``{(name, email)}`` pairs to canonical ``(name, email)`` pairs.

    Uses ``git -C <repo> check-mailmap --stdin`` in batches; if a batch fails
    (a single malformed contact makes git abort), falls back to per-contact
    resolution so one bad identity cannot break the whole ingest.
    """
    identities = list(identities)
    out = {}
    for start in range(0, len(identities), CHUNK):
        chunk = identities[start:start + CHUNK]
        payload = "".join(_fmt(n, e) + "\n" for n, e in chunk).encode("utf-8", "replace")
        proc = subprocess.run(
            ["git", "-C", repo_path, "check-mailmap", "--stdin"],
            input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        resolved = proc.stdout.decode("utf-8", "replace").splitlines()
        if proc.returncode != 0 or len(resolved) != len(chunk):
            resolved = []
            for n, e in chunk:
                p = subprocess.run(
                    ["git", "-C", repo_path, "check-mailmap", _fmt(n, e)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                resolved.append(
                    p.stdout.decode("utf-8", "replace").strip() if p.returncode == 0 else ""
                )
        for (n, e), line in zip(chunk, resolved):
            parsed = _parse_contact(line or "")
            out[(n, e)] = parsed or (n, e)
    return out
