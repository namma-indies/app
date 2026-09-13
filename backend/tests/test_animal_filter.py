"""What the shared surfaces count, once the detector has an opinion.

Three properties, and the third is the one that makes the review pass worth
doing once: a human ruling outranks the model and survives a retune.
"""

import pytest

from app.ids import uuid7

pytestmark = pytest.mark.asyncio


async def _sighting(conn, *, conf=None, override=None):
    oid, sid = uuid7(), uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')", oid)
    await conn.execute(
        "INSERT INTO sightings (id, observer_id, captured_at, geog, geo_source, "
        "                       animal_confidence, animal_override) "
        "VALUES ($1,$2,now(), ST_SetSRID(ST_MakePoint(77.59,12.97),4326)::geography, "
        "        'device_gps', $3, $4)",
        sid, oid, conf, override)
    return sid


async def _countable(conn):
    from app.aggregates import countable_sighting

    return await conn.fetchval(
        f"SELECT count(*) FROM sightings s WHERE {countable_sighting()}")


def test_the_filter_is_inert_by_default():
    """It ships switched off. The threshold is chosen in phase 2, against
    rescored numbers -- not from the mixed corpus #67 measured."""
    from app.config import settings

    assert settings.animal_confidence_min == 0.0


async def test_unscored_sightings_stay_visible(migrated_db, monkeypatch):
    """Fail open. NULL means the detector never ran or failed, and that must
    not take someone's photograph off the map."""
    from app.config import settings

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    await _sighting(migrated_db, conf=None)
    assert await _countable(migrated_db) == 1


async def test_a_low_score_drops_out_once_a_threshold_is_set(migrated_db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    await _sighting(migrated_db, conf=0.05)
    await _sighting(migrated_db, conf=0.91)
    assert await _countable(migrated_db) == 1


async def test_a_human_verdict_outranks_the_model_both_ways(migrated_db, monkeypatch):
    """The property that makes a review pass a one-time job: the ruling
    survives a threshold change and the next detector swap."""
    from app.config import settings

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    await _sighting(migrated_db, conf=0.01, override=True)   # "it IS a dog"
    await _sighting(migrated_db, conf=0.99, override=False)  # "no it isn't"
    assert await _countable(migrated_db) == 1

    monkeypatch.setattr(settings, "animal_confidence_min", 0.90)
    assert await _countable(migrated_db) == 1


async def test_review_status_still_counts(migrated_db, monkeypatch):
    """The animal rule is composed with the moderation rule, not instead of
    it. A rejected sighting stays off regardless of how doggy it is."""
    from app.config import settings

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    sid = await _sighting(migrated_db, conf=0.99)
    await migrated_db.execute(
        "UPDATE sightings SET review_status='rejected' WHERE id=$1", sid)
    assert await _countable(migrated_db) == 0


def test_the_predicate_only_touches_the_s_alias():
    """`routes/match.py` re-aliases this to `a.` and `b.` with a blind string
    replace. That is only safe while EVERY alias-qualified column in the
    string is on `s` -- a `t.`-aliased join column added here would survive
    the replace untouched and produce a query referencing a table that side
    of the join does not have. Guard it."""
    import re

    from app.aggregates import animal_present

    aliases = set(re.findall(r"\b([a-z_]+)\.", animal_present()))
    assert aliases == {"s"}, f"non-`s` alias in the predicate: {aliases - {'s'}}"
