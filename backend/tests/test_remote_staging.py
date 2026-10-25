"""deploy/remote-staging.sh: switching branches must not strand the database.

Staging hops between feature branches and main, whose migrations are siblings
rather than ancestors of each other. Before this, a switch left the database
stamped with a revision the new code had never heard of; alembic refused it and
the app restart-looped (2026-10-25: main against feat/match-calibration's 0017).
The script now walks the database back to the newest revision both branches
share, using the downgrades of the code still checked out, before it switches.

These run the real script against a real git origin, with `sudo` replaced by a
stub that answers the alembic_version query and records every docker call,
along with the branch the box was on when it was made.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "remote-staging.sh"
VERSIONS = "backend/migrations/versions"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def migration(rev, down, typed=False):
    down_src = "None" if down is None else f'"{down}"'
    if typed:
        return (f'revision: str = "{rev}"\n'
                f"down_revision: Union[str, None] = {down_src}\n")
    return f'revision = "{rev}"\ndown_revision = {down_src}\n'


# main and feat both build on 0015; desc carries main forward.
BRANCHES = {
    "main": {"0015_base": None, "0016_a": "0015_base"},
    "feat": {"0015_base": None, "0016_b": "0015_base", "0017_b": "0016_b"},
    "desc": {"0015_base": None, "0016_a": "0015_base", "0017_c": "0016_a"},
}

SUDO_STUB = """#!/usr/bin/env bash
branch=$(git -C "$HOME/app" rev-parse --abbrev-ref HEAD)
echo "[$branch] $*" >> "$HOME/sudo.log"
case "$*" in
  *psql*)    printf '%s\\n' "$FAKE_DB_REV" ;;
  *pg_dump*) echo DUMP ;;
esac
"""


@pytest.fixture
def home(tmp_path):
    origin = tmp_path / "origin"
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / VERSIONS).mkdir(parents=True)
    (seed / "README").write_text("seed\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    for branch, revs in BRANCHES.items():
        git(seed, "checkout", "-q", "-B", branch, "main" if branch != "main" else "HEAD")
        for f in (seed / VERSIONS).glob("*.py"):
            f.unlink()
        for i, (rev, down) in enumerate(revs.items()):
            # Both spellings alembic has generated over the years are in the tree.
            (seed / VERSIONS / f"{rev}.py").write_text(migration(rev, down, typed=i % 2 == 1))
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "--allow-empty", "-m", branch)
    git(seed, "push", "-q", str(origin), *BRANCHES)

    home = tmp_path / "home"
    home.mkdir()
    git(home, "clone", "-q", str(origin), "app")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "sudo").write_text(SUDO_STUB)
    (bin_dir / "sudo").chmod(0o755)
    return home


def deploy(home, *, on, db, to):
    git(home / "app", "checkout", "-q", on)
    env = {
        **os.environ, **GIT_ENV,
        "HOME": str(home),
        "PATH": f"{home.parent / 'bin'}:{os.environ['PATH']}",
        "BRANCH": to,
        "FAKE_DB_REV": db,
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, timeout=60,
    )


def calls(home):
    log = home / "sudo.log"
    return log.read_text().splitlines() if log.exists() else []


def test_switching_to_a_sibling_branch_rolls_back_to_the_shared_revision(home):
    r = deploy(home, on="feat", db="0017_b", to="main")
    assert r.returncode == 0, r.stderr
    log = calls(home)
    downgrade = [c for c in log if "alembic downgrade" in c]
    # Run with feat still checked out: main has no downgrade() for 0017_b.
    assert downgrade == [downgrade[0]] and downgrade[0].startswith("[feat]")
    assert downgrade[0].endswith("alembic downgrade 0015_base")
    dump = next(i for i, c in enumerate(log) if "pg_dump" in c)
    up = next(i for i, c in enumerate(log) if "up -d --build" in c)
    assert dump < log.index(downgrade[0]) < up
    assert log[up].startswith("[main]")
    backups = list((home / "staging-db-backups").iterdir())
    assert len(backups) == 1 and backups[0].read_text() == "DUMP\n"


def test_moving_forward_along_the_same_chain_touches_nothing(home):
    r = deploy(home, on="main", db="0016_a", to="desc")
    assert r.returncode == 0, r.stderr
    log = calls(home)
    assert not [c for c in log if "pg_dump" in c or "alembic downgrade" in c]
    assert any(c.startswith("[desc]") and "up -d --build" in c for c in log)


def test_redeploying_the_same_branch_touches_nothing(home):
    r = deploy(home, on="feat", db="0017_b", to="feat")
    assert r.returncode == 0, r.stderr
    assert not [c for c in calls(home) if "pg_dump" in c or "alembic downgrade" in c]


def test_a_fresh_database_is_left_for_the_entrypoint_to_migrate(home):
    r = deploy(home, on="main", db="", to="feat")
    assert r.returncode == 0, r.stderr
    assert not [c for c in calls(home) if "pg_dump" in c or "alembic downgrade" in c]


def test_a_revision_the_checkout_does_not_know_stops_the_deploy(home):
    r = deploy(home, on="main", db="0099_nowhere", to="feat")
    assert r.returncode != 0
    assert "0099_nowhere" in r.stderr
    log = calls(home)
    assert not [c for c in log if "alembic downgrade" in c or "up -d" in c]
    assert git(home / "app", "rev-parse", "--abbrev-ref", "HEAD") == "main"
