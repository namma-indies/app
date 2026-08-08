import pytest

from app.auth.email_login import get_or_create_observer_by_email


@pytest.mark.asyncio
async def test_creates_observer_on_first_sight(migrated_db):
    oid = await get_or_create_observer_by_email(
        migrated_db, email="akash@dognosis.tech", display_name="Akash")
    row = await migrated_db.fetchrow(
        "SELECT email, display_name, created_via FROM observers WHERE id = $1", oid)
    assert row["email"] == "akash@dognosis.tech"
    assert row["display_name"] == "Akash"
    assert row["created_via"] == "email"


@pytest.mark.asyncio
async def test_same_email_resolves_to_same_observer(migrated_db):
    """The whole point: a returning person is one identity, not many."""
    first = await get_or_create_observer_by_email(migrated_db, email="a@dognosis.tech")
    second = await get_or_create_observer_by_email(migrated_db, email="a@dognosis.tech")
    assert first == second
    assert await migrated_db.fetchval("SELECT count(*) FROM observers") == 1


@pytest.mark.asyncio
async def test_existing_display_name_is_not_overwritten(migrated_db):
    oid = await get_or_create_observer_by_email(
        migrated_db, email="a@dognosis.tech", display_name="Akash")
    await get_or_create_observer_by_email(
        migrated_db, email="a@dognosis.tech", display_name="Someone Else")
    name = await migrated_db.fetchval(
        "SELECT display_name FROM observers WHERE id = $1", oid)
    assert name == "Akash"


@pytest.mark.asyncio
async def test_falls_back_to_local_part_when_no_display_name(migrated_db):
    oid = await get_or_create_observer_by_email(migrated_db, email="priya@dognosis.tech")
    name = await migrated_db.fetchval(
        "SELECT display_name FROM observers WHERE id = $1", oid)
    assert name == "priya"


@pytest.mark.asyncio
async def test_passcode_observers_are_untouched(migrated_db):
    """A NULL-email passcode observer must not be matched or clobbered."""
    from app.auth.magiclink import create_observer
    pc = await create_observer(migrated_db, display_name="Field Tester", created_via="passcode")
    oid = await get_or_create_observer_by_email(migrated_db, email="a@dognosis.tech")
    assert oid != pc
    assert await migrated_db.fetchval("SELECT count(*) FROM observers") == 2


# --- carry-over: absorbing unverified passcode observers -------------------
#
# Once testers type their work email into the passcode form's name field, that
# string becomes a real key -- so a person's pre-email sightings can follow them
# to their verified identity instead of being stranded.

from app.auth.email_login import absorb_passcode_observers  # noqa: E402


async def _passcode_observer(conn, name: str):
    from app.auth.magiclink import create_observer
    return await create_observer(conn, display_name=name, created_via="passcode")


async def _sighting(conn, observer_id):
    from app.ids import uuid7
    sid = uuid7()
    await conn.execute(
        "INSERT INTO sightings (id, observer_id, captured_at) VALUES ($1,$2, now())",
        sid, observer_id,
    )
    return sid


@pytest.mark.asyncio
async def test_absorbs_sightings_from_matching_passcode_observer(migrated_db):
    old = await _passcode_observer(migrated_db, "akash@dognosis.tech")
    await _sighting(migrated_db, old)
    await _sighting(migrated_db, old)
    new = await get_or_create_observer_by_email(migrated_db, email="akash@dognosis.tech")

    n = await absorb_passcode_observers(migrated_db, target=new, email="akash@dognosis.tech")

    assert n == 1
    assert await migrated_db.fetchval(
        "SELECT count(*) FROM sightings WHERE observer_id = $1", new) == 2
    assert await migrated_db.fetchval(
        "SELECT count(*) FROM sightings WHERE observer_id = $1", old) == 0


@pytest.mark.asyncio
async def test_matching_is_case_and_whitespace_insensitive(migrated_db):
    old = await _passcode_observer(migrated_db, "  Akash@Dognosis.TECH ")
    await _sighting(migrated_db, old)
    new = await get_or_create_observer_by_email(migrated_db, email="akash@dognosis.tech")
    assert await absorb_passcode_observers(
        migrated_db, target=new, email="akash@dognosis.tech") == 1


@pytest.mark.asyncio
async def test_leaves_unrelated_passcode_observers_alone(migrated_db):
    mine = await _passcode_observer(migrated_db, "akash@dognosis.tech")
    theirs = await _passcode_observer(migrated_db, "priya@dognosis.tech")
    plain = await _passcode_observer(migrated_db, "Priya")
    await _sighting(migrated_db, theirs)
    await _sighting(migrated_db, plain)
    await _sighting(migrated_db, mine)
    new = await get_or_create_observer_by_email(migrated_db, email="akash@dognosis.tech")

    assert await absorb_passcode_observers(
        migrated_db, target=new, email="akash@dognosis.tech") == 1
    for other in (theirs, plain):
        assert await migrated_db.fetchval(
            "SELECT count(*) FROM sightings WHERE observer_id = $1", other) == 1
        assert await migrated_db.fetchval(
            "SELECT deleted_at FROM observers WHERE id = $1", other) is None


@pytest.mark.asyncio
async def test_never_absorbs_another_email_observer(migrated_db):
    """Only unverified passcode rows are absorbable. A verified identity is
    never swallowed by another, whatever its display_name happens to say."""
    other = await get_or_create_observer_by_email(
        migrated_db, email="akash@dognosis.tech", display_name="akash@dognosis.tech")
    await _sighting(migrated_db, other)
    target = await get_or_create_observer_by_email(migrated_db, email="someone@dognosis.tech")
    await migrated_db.execute(
        "UPDATE observers SET display_name = 'someone@dognosis.tech' WHERE id = $1", other)

    assert await absorb_passcode_observers(
        migrated_db, target=target, email="someone@dognosis.tech") == 0
    assert await migrated_db.fetchval(
        "SELECT count(*) FROM sightings WHERE observer_id = $1", other) == 1


@pytest.mark.asyncio
async def test_absorbed_observer_is_retired_and_absorb_is_idempotent(migrated_db):
    old = await _passcode_observer(migrated_db, "akash@dognosis.tech")
    await _sighting(migrated_db, old)
    new = await get_or_create_observer_by_email(migrated_db, email="akash@dognosis.tech")

    assert await absorb_passcode_observers(migrated_db, target=new, email="akash@dognosis.tech") == 1
    assert await migrated_db.fetchval(
        "SELECT deleted_at FROM observers WHERE id = $1", old) is not None
    # second run finds nothing -- a retired observer is not re-absorbed
    assert await absorb_passcode_observers(migrated_db, target=new, email="akash@dognosis.tech") == 0


@pytest.mark.asyncio
async def test_moves_every_observer_reference_not_just_sightings(migrated_db):
    """All six FK columns must move, or the retired observer orphans rows."""
    from app.ids import uuid7
    old = await _passcode_observer(migrated_db, "akash@dognosis.tech")
    sid = await _sighting(migrated_db, old)
    ind, prop, conf = uuid7(), uuid7(), uuid7()
    await migrated_db.execute(
        "INSERT INTO individuals (id, named_by, created_by_observer) VALUES ($1,$2,$2)", ind, old)
    await migrated_db.execute(
        # candidate_individual_id is required by ck_match_proposals_has_target:
        # a proposal must name something a human can act on.
        "INSERT INTO match_proposals (id, sighting_id, candidate_individual_id, resolved_by) "
        "VALUES ($1,$2,$3,$4)",
        prop, sid, ind, old)
    await migrated_db.execute(
        "INSERT INTO confirmations (id, sighting_id, observer_id, verdict) "
        "VALUES ($1,$2,$3,'same')", conf, sid, old)

    new = await get_or_create_observer_by_email(migrated_db, email="akash@dognosis.tech")
    await absorb_passcode_observers(migrated_db, target=new, email="akash@dognosis.tech")

    assert await migrated_db.fetchval("SELECT named_by FROM individuals WHERE id=$1", ind) == new
    assert await migrated_db.fetchval("SELECT created_by_observer FROM individuals WHERE id=$1", ind) == new
    assert await migrated_db.fetchval("SELECT resolved_by FROM match_proposals WHERE id=$1", prop) == new
    assert await migrated_db.fetchval("SELECT observer_id FROM confirmations WHERE id=$1", conf) == new
