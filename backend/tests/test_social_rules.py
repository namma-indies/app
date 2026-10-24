"""Who and what may go out on @nammaindies, and what the caption may say.

Eligibility is one rule used twice -- when a moderator picks, and again when
the daily run posts -- so each disqualifier is its own case here: a regression
names the condition it let through.
"""

import pytest

from app.ids import uuid7
from app.social import (
    area_name,
    default_caption,
    is_postable,
    postable_photos,
    validate_caption,
)
from tests import social_fixtures as fx


async def test_postable_photos_legacy_and_capture_paths(migrated_db):
    oid = await fx.observer(migrated_db)
    legacy, pids = await fx.legacy_sighting(migrated_db, oid, photos=2)
    assert await postable_photos(migrated_db, legacy) == pids

    cap, source, crop = await fx.capture_sighting(migrated_db, oid)
    assert await postable_photos(migrated_db, cap) == [source]
    assert await is_postable(migrated_db, cap, source)
    assert not await is_postable(migrated_db, cap, crop)
    # A photo belonging to another sighting is not this sighting's to post.
    assert not await is_postable(migrated_db, legacy, source)


@pytest.mark.parametrize("case", [
    "rejected", "duplicate", "unshared", "no_consent", "no_handle",
    "legacy_video", "capture_video",
])
async def test_not_postable_when_rejected_duplicate_unshared_unconsented_or_video(
        migrated_db, case):
    oid = await fx.observer(
        migrated_db,
        consent=case != "no_consent",
        handle=None if case == "no_handle" else "a_handle")
    if case == "capture_video":
        sid, _, _ = await fx.capture_sighting(migrated_db, oid, kind="video")
    else:
        sid, _ = await fx.legacy_sighting(
            migrated_db, oid,
            review="rejected" if case == "rejected" else "valid",
            shared=case != "unshared",
            source="video" if case == "legacy_video" else None)
    if case == "duplicate":
        original, _ = await fx.legacy_sighting(migrated_db, oid)
        await migrated_db.execute(
            "UPDATE sightings SET duplicate_of=$2 WHERE id=$1", sid, original)
    assert await postable_photos(migrated_db, sid) == []


async def test_cats_are_postable(migrated_db):
    oid = await fx.observer(migrated_db)
    sid, pids = await fx.legacy_sighting(migrated_db, oid, species="cat")
    assert await postable_photos(migrated_db, sid) == pids


async def test_area_name_uses_covering_ward_else_none(migrated_db):
    oid = await fx.observer(migrated_db)
    await fx.ward(migrated_db, "Indiranagar")
    inside, _ = await fx.legacy_sighting(migrated_db, oid)
    outside, _ = await fx.legacy_sighting(migrated_db, oid, at=(13.5, 78.5))
    nowhere, _ = await fx.legacy_sighting(migrated_db, oid, at=None)
    assert await area_name(migrated_db, inside) == "Indiranagar"
    assert await area_name(migrated_db, outside) is None
    assert await area_name(migrated_db, nowhere) is None
    assert await area_name(migrated_db, uuid7()) is None


def test_default_caption_exact():
    assert default_caption("Indiranagar", "a_handle") == "Spotted in Indiranagar 📸 @a_handle"
    assert default_caption(None, "a_handle") == "Spotted in {area} 📸 @a_handle"


@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "see https://x.co",
    "www.nammaindies.org",
    "nammaindies.org/app",
    "Spotted in {area} 📸 @a",
    "x" * 261,
])
def test_validate_caption_rejects(bad):
    assert validate_caption(bad) is not None


def test_validate_caption_accepts():
    assert validate_caption("Spotted in Indiranagar 📸 @a_handle") is None
    # A handle or a full stop is not a link.
    assert validate_caption("Spotted in HAL 2nd Stage. 📸 @second_handle") is None


@pytest.mark.parametrize("bad", [
    "Spotted at indie.dog 📸 @a",
    "via nammaindies.pet",
    "blr.city",
    "foo.uk",
    "bar.to",
])
def test_validate_caption_rejects_any_bare_domain(bad):
    """X links bare domains on far more endings than any short list, and a
    linked post costs ~13x -- so anything shaped like name.tld is refused."""
    assert validate_caption(bad) is not None
