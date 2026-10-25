"""The migration chain has exactly one head.

Two branches that each add a migration on top of the same parent both pass CI
on their own, then fork the chain once they meet on main. `alembic upgrade
head` refuses a forked chain, so the entrypoint fails and the app
restart-loops -- in production, since merging is deploying. That nearly
happened on 2026-10-25: feat/match-calibration carries 0016/0017 on top of
0015, and main had moved on to 0016-0021 from the same parent.

CI runs pull requests against the merge commit, so this test sees the chain as
it would be after merging. A red run here means: renumber the branch's
migrations onto main's current head (point the first one's down_revision at
it), not add a merge migration.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).resolve().parents[1]


def test_migrations_have_a_single_head():
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, (
        f"migration chain has forked into heads {sorted(heads)} -- rebase the "
        "newer branch's migrations onto main's head"
    )
