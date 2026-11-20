"""Every foreign key into what erasure deletes from is accounted for.

A migration that adds a column pointing at a sighting, a capture, a photo, ...
must say what happens to it when that row is deleted, in
`app.erasure.REFERENCES`. Otherwise the new rows either survive a deletion
(the data outlives what its owner deleted) or refuse it (nothing can be
deleted any more -- which is how `confirmations.proposal_id` blocked every
matched sighting until 0031).
"""
import pytest

from app.erasure import ERASED_TABLES, REFERENCES


@pytest.mark.asyncio
async def test_every_reference_into_erased_tables_is_classified(migrated_db):
    rows = await migrated_db.fetch(
        """SELECT src.relname AS tbl, att.attname AS col, dst.relname AS target, con.confdeltype::text AS rule
           FROM pg_constraint con
           JOIN pg_class src ON src.oid = con.conrelid
           JOIN pg_class dst ON dst.oid = con.confrelid
           JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
           WHERE con.contype = 'f' AND dst.relname = ANY($1::text[])""",
        ERASED_TABLES)
    found = {(r["tbl"], r["col"], r["target"]): r["rule"] for r in rows}
    unlisted = sorted(set(found) - set(REFERENCES))
    assert not unlisted, f"classify these in app.erasure.REFERENCES: {unlisted}"
    stale = sorted(set(REFERENCES) - set(found))
    assert not stale, f"no longer in the schema, remove from REFERENCES: {stale}"
    changed = {k: (REFERENCES[k], found[k]) for k in found if REFERENCES[k] != found[k]}
    assert not changed, f"ON DELETE rule differs from REFERENCES (listed, actual): {changed}"
