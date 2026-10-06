#!/usr/bin/env python3
"""Build the synthetic git fixture used by the engine tests.

The fixture is fully deterministic: every commit gets explicit author
identities and committer dates (base 2024-01-01T12:00:00Z, day N starts at
base + (N-1) days), so all metric expectations can be computed by hand.

Repository layout (13 commits: 12 non-merge + 1 merge):

  m1  day1  Alice   create src/app.py (8 lines) + docs/readme.md (10 lines)
  m2  day2  Bob     append 2 lines to src/app.py
  m3  day3  Robert  create src/util.py (4 lines)
  m4  day4  Alice   pure rename docs/readme.md -> docs/guide.md (0/0)
  m5  day5  Alice   edit one line of docs/guide.md (+1/-1)
  m6  day6  Bob     add binary logo.bin (must be ignored by metrics)
  m7  day7  Alice   delete src/util.py (-4)
  m8  day8  Bob     empty commit (counts in |H|, no metrics)
  m9  day9  Alice   branch 'side': create side.py (3 lines)
  m10 day10 Bob     create settings.ini (6 lines)
  m11 day11 --      merge 'side' into 'main' (excluded from H)
  m12 day12 Alice   remove the 2 lines added in m2 (-2)
  m13 day13 Robert  rewrite settings.ini as old.ini below the 50%
                    rename threshold -> delete settings.ini (-6) + add
                    old.ini (+2)

An untracked .mailmap maps Bob <bob@work.com> and
Robert Smith <r.smith@personal.com> onto the canonical
Bob Smith <bob.smith@canonical.com>.

Run standalone to build a copy and eyeball the raw git data:

    python3 tests/make_synth_repos.py /tmp/synth
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys

DAY = 86400
BASE_TS = 1704110400  # 2024-01-01T12:00:00+00:00

ALICE = ("Alice", "alice@example.com")
BOB = ("Bob", "bob@work.com")
ROBERT = ("Robert Smith", "r.smith@personal.com")
COMMITTER = ("Fixture Committer", "fixture@fixture.local")

MAILMAP_TEXT = (
    "Bob Smith <bob.smith@canonical.com> <bob@work.com>\n"
    "Bob Smith <bob.smith@canonical.com> Robert Smith <r.smith@personal.com>\n"
)

APP_V1 = [
    "import sys",
    "",
    "def main():",
    '    print("app")',
    '    print("v1")',
    "",
    'if __name__ == "__main__":',
    "    main()",
]
APP_V2 = APP_V1 + ["# v2 changelog", "# tweak"]
README_V1 = [
    "# Readme",
    "",
    "Intro line 1",
    "Intro line 2",
    "",
    "## Usage",
    "run it",
    "",
    "## Notes",
    "none",
]
GUIDE_V2 = list(README_V1)
GUIDE_V2[9] = "see docs"  # single-line edit in m5
UTIL_V1 = ["def helper():", "    return 42", "", "# end"]
SIDE_V1 = ["def side():", "    return 'side'", "# end"]
SETTINGS_V1 = ["[core]", "threads = 4", "mode = fast", "", "[paths]", "root = ."]
OLD_INI = ["mode = fast", "root = ."]  # 2 of 6 lines kept -> similarity < 50%


def day_ts(n: int) -> int:
    """Unix timestamp of 12:00 UTC on day N of the fixture timeline."""
    return BASE_TS + (n - 1) * DAY


def _iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )


class Fixture:
    """Small helper around git for building the fixture deterministically."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(path, exist_ok=True)

    def git(self, *args, extra_env=None, check=True) -> str:
        env = dict(os.environ)
        env.update({
            "GIT_CONFIG_NOSYSTEM": "1",           # ignore /etc/gitconfig
            "GIT_CONFIG_GLOBAL": os.devnull,       # ignore ~/.gitconfig
            "GIT_COMMITTER_NAME": COMMITTER[0],
            "GIT_COMMITTER_EMAIL": COMMITTER[1],
        })
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            ["git", "-C", self.path, *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        if check and proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {msg}")
        return proc.stdout.decode("utf-8", "replace").strip()

    def write(self, rel: str, lines) -> None:
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def write_bytes(self, rel: str, data: bytes) -> None:
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full) or self.path, exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)

    def rm(self, rel: str) -> None:
        self.git("rm", "--quiet", rel)

    def commit(self, message: str, author, day: int, allow_empty: bool = False) -> str:
        env = {
            "GIT_AUTHOR_NAME": author[0],
            "GIT_AUTHOR_EMAIL": author[1],
            "GIT_AUTHOR_DATE": _iso(day_ts(day)),
            "GIT_COMMITTER_DATE": _iso(day_ts(day)),
        }
        if not allow_empty:
            self.git("add", "--all", extra_env=env)
        args = ["commit", "--quiet", "-m", message]
        if allow_empty:
            args.insert(1, "--allow-empty")
        self.git(*args, extra_env=env)
        return self.git("rev-parse", "HEAD")


def build_fixture(path: str) -> dict:
    """Create the fixture repository at ``path`` and return useful handles."""
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path)
    fx = Fixture(path)
    fx.git("init", "--quiet", "-b", "main")
    fx.git("config", "commit.gpgsign", "false")

    hashes = {}
    fx.write("src/app.py", APP_V1)
    fx.write("docs/readme.md", README_V1)
    hashes["m1"] = fx.commit("Create app and readme", ALICE, 1)

    fx.write("src/app.py", APP_V2)
    hashes["m2"] = fx.commit("Extend app", BOB, 2)

    fx.write("src/util.py", UTIL_V1)
    hashes["m3"] = fx.commit("Add util", ROBERT, 3)

    fx.git("mv", "docs/readme.md", "docs/guide.md")
    hashes["m4"] = fx.commit("Rename readme to guide", ALICE, 4)

    fx.write("docs/guide.md", GUIDE_V2)
    hashes["m5"] = fx.commit("Tweak guide", ALICE, 5)

    # 256 bytes containing NULs -> git classifies it as binary.
    fx.write_bytes("logo.bin", bytes(range(256)) * 4)
    hashes["m6"] = fx.commit("Add logo", BOB, 6)

    fx.rm("src/util.py")
    hashes["m7"] = fx.commit("Remove util", ALICE, 7)

    hashes["m8"] = fx.commit("Empty housekeeping commit", BOB, 8, allow_empty=True)

    branch = fx.git("rev-parse", "--abbrev-ref", "HEAD")
    assert branch == "main", branch
    fx.git("checkout", "--quiet", "-b", "side")
    fx.write("side.py", SIDE_V1)
    hashes["m9"] = fx.commit("Add side module", ALICE, 9)

    fx.git("checkout", "--quiet", "main")
    fx.write("settings.ini", SETTINGS_V1)
    hashes["m10"] = fx.commit("Add settings", ROBERT, 10)

    fx.git("merge", "--no-ff", "--no-edit", "side",
           extra_env={
               "GIT_AUTHOR_NAME": COMMITTER[0],
               "GIT_AUTHOR_EMAIL": COMMITTER[1],
               "GIT_AUTHOR_DATE": _iso(day_ts(11)),
               "GIT_COMMITTER_DATE": _iso(day_ts(11)),
           })
    hashes["m11_merge"] = fx.git("rev-parse", "HEAD")

    fx.write("src/app.py", APP_V1)  # removes the 2 appended lines
    hashes["m12"] = fx.commit("Trim app", ALICE, 12)

    fx.rm("settings.ini")
    fx.write("old.ini", OLD_INI)
    hashes["m13"] = fx.commit("Move settings to old.ini", ROBERT, 13)

    with open(os.path.join(path, ".mailmap"), "w", encoding="utf-8") as fh:
        fh.write(MAILMAP_TEXT)

    return {
        "path": path,
        "commits": hashes,
        "head": hashes["m13"],
        "base_ts": BASE_TS,
        "day_ts": {n: day_ts(n) for n in range(1, 14)},
    }


def _demo(path: str) -> None:
    info = build_fixture(path)
    print(f"fixture built at {info['path']}")
    print(f"commits: {info['commits']}")
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    out = subprocess.run(
        ["git", "-C", path, "log", "--no-merges", "-M50%", "--numstat",
         "--format=COMMIT %h %ct %an"],
        stdout=subprocess.PIPE, env=env,
    ).stdout.decode("utf-8", "replace")
    print(out)
    added = removed = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            added += int(parts[0])
            removed += int(parts[1])
    n = subprocess.run(
        ["git", "-C", path, "rev-list", "--no-merges", "--count", "HEAD"],
        stdout=subprocess.PIPE, env=env,
    ).stdout.decode().strip()
    print(f"TOTAL non-merge commits={n} added={added} removed={removed}")


if __name__ == "__main__":
    _demo(sys.argv[1] if len(sys.argv) > 1 else "/tmp/rat-synth")
