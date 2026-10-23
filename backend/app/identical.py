"""The same photograph in two sightings is the same dog. Link them, ask no one.

Of 33 pending proposals in production on 2026-10-23, 32 were one photo attached
to two sightings: picked twice from the gallery, or replayed by the offline
queue before #38. Every one is a question with a known answer, and together
they buried the one real match a person needed to look at. So the owner's rule
is that a byte-identical photo merges automatically, across observers too.

Neither sighting is deleted. Identical bytes can still be two events -- the
same picture logged twice is two rows in `/dex`, and migration 0009 refused to
deduplicate on pixels for exactly that reason. What they cannot be is two
dogs. This links identities; it never removes an observation.

WHAT "IDENTICAL" MEANS
----------------------
`photos.content_sha256`, the hash of the stored original (migration 0016 says
why that, and not the upload). A /sighting photo carries it directly. A
/capture sighting holds only per-animal crops, so it is matched through the
*source* photo its animal instances were cut from.

WHEN IT STEPS ASIDE
-------------------
A rule that is never wrong has to know when it is not sure. It falls back to an
ordinary proposal, and a person, when:

* **the photo shows more than one animal.** The same frame of two dogs says
  nothing about which dog either sighting is. Judged on the photo, not the
  sighting: if any copy of those bytes was found to hold two instances, every
  copy is ambiguous, including a /sighting copy whose pipeline only ever
  looked at the biggest box.
* **the sightings already belong to different dogs.** Folding two identities
  together is a merge of individuals, which nothing here is entitled to do --
  and a human verdict may be what put either one there.
* **`observations_compatible` refuses** -- two animals a capture separated, or
  a dog and a cat. Same check the human path makes.
* either side is rejected by a moderator, or ruled to hold no animal. A
  takedown that still seeds identities is a decision that did not stick.

It never un-links anything. A sighting is only ever moved from no identity to
one, so a human-confirmed row is untouched by construction, not by care.

Not a verdict. Nothing is written to `confirmations`, which is the human-labelled
calibration set, and an identity minted here says so (`created_by='model'`,
`created_via='identical_photo'`). `auto_links` records each link and the
sighting whose photo justified it.

Caller holds `lock_matching` inside a transaction, as every resolver does.
"""

from dataclasses import dataclass, field
from uuid import UUID

import asyncpg

from app.aggregates import animal_present

METHOD = "identical_photo"

# Which sighting each hashed photo is evidence for. A /sighting photo belongs to
# its sighting directly. A /capture source belongs to whichever sightings its
# animal instances were published into: its own `sighting_id` is NULL, because
# only the crops are attached. `animals` counts instances on the photo itself,
# so it is 0 for every /sighting photo -- that pipeline never separated animals.
_EVIDENCE = """
    WITH hashed AS (
        SELECT id, sighting_id, content_sha256 FROM photos
        WHERE content_sha256 = ANY($1::text[])
    ),
    owned AS (
        SELECT h.sighting_id, h.id AS photo_id, h.content_sha256
        FROM hashed h WHERE h.sighting_id IS NOT NULL
        UNION
        SELECT a.sighting_id, h.id, h.content_sha256
        FROM hashed h JOIN animal_instances a ON a.source_photo_id = h.id
        WHERE a.sighting_id IS NOT NULL
    )
    SELECT o.sighting_id, o.photo_id, o.content_sha256,
           (SELECT count(*) FROM animal_instances x
            WHERE x.source_photo_id = o.photo_id) AS animals
    FROM owned o
"""

_MY_HASHES = """
    SELECT p.content_sha256 FROM photos p
    WHERE p.sighting_id = $1 AND p.content_sha256 IS NOT NULL
    UNION
    SELECT src.content_sha256 FROM animal_instances a
    JOIN photos src ON src.id = a.source_photo_id
    WHERE a.sighting_id = $1 AND src.content_sha256 IS NOT NULL
"""


@dataclass
class IdenticalOutcome:
    # 'linked', or 'skipped' with a reason: 'multi_animal',
    # 'conflicting_identities', 'incompatible'.
    status: str
    individual_id: UUID | None = None
    reason: str | None = None
    partners: list[UUID] = field(default_factory=list)
    newly_linked: list[UUID] = field(default_factory=list)
    minted: bool = False
    proposals_cleared: int = 0


async def link_identical(
    conn: asyncpg.Connection, sighting_id: UUID, *, new_uuid
) -> IdenticalOutcome | None:
    """Link `sighting_id` with every sighting holding one of its photos.

    None means the rule has nothing to say -- no hashed photo, or no other
    sighting has one -- and the caller carries on exactly as before. A
    'skipped' outcome means it found a copy and declined, so the pair goes
    to a person.
    """
    hashes = [r["content_sha256"] for r in await conn.fetch(_MY_HASHES, sighting_id)]
    if not hashes:
        return None
    evidence = await conn.fetch(_EVIDENCE, hashes)

    ambiguous = {r["content_sha256"] for r in evidence if r["animals"] > 1}
    partner_hash: dict[UUID, str] = {}
    declined = False
    for r in evidence:
        if r["sighting_id"] == sighting_id:
            continue
        if r["content_sha256"] in ambiguous:
            declined = True
            continue
        partner_hash.setdefault(r["sighting_id"], r["content_sha256"])
    if not partner_hash:
        return IdenticalOutcome("skipped", reason="multi_animal") if declined else None

    group_ids = [sighting_id, *partner_hash]
    # Rejected or no-animal rows drop out quietly, exactly as find_candidates
    # drops them: they are not candidates at all, so there is no pair to ask
    # about. If the resolving sighting itself is one, there is nothing to do.
    rows = await conn.fetch(
        f"""
        SELECT s.id, s.individual_id, s.captured_at FROM sightings s
        WHERE s.id = ANY($1::uuid[])
          AND s.review_status <> 'rejected'
          AND {animal_present()}
        """,
        group_ids,
    )
    live = {r["id"]: r for r in rows}
    if sighting_id not in live:
        return None
    partners = [sid for sid in partner_hash if sid in live]
    if not partners:
        return None
    group = [sighting_id, *partners]

    identities = {live[s]["individual_id"] for s in group} - {None}
    if len(identities) > 1:
        return IdenticalOutcome("skipped", reason="conflicting_identities", partners=partners)

    from app.matching import observations_compatible
    if not await observations_compatible(conn, group):
        return IdenticalOutcome("skipped", reason="incompatible", partners=partners)

    newly = [s for s in group if live[s]["individual_id"] is None]
    if not newly:
        # Already one dog -- a human said so, or an earlier pass of this rule.
        # Still worth tidying the queue below, so fall through with no writes
        # to sightings.
        individual_id = next(iter(identities))
        minted = False
    elif identities:
        individual_id = next(iter(identities))
        minted = False
        # Widen the identity's span to cover what joins it. Same shape as the
        # score-based link in matching.py, but in both directions: a re-imported
        # gallery photo carries its original EXIF date, which can predate
        # everything the dog was known by.
        await conn.execute(
            """
            UPDATE individuals SET
                first_seen_at = LEAST(COALESCE(first_seen_at, 'infinity'::timestamptz),
                                      (SELECT MIN(captured_at) FROM sightings WHERE id = ANY($2::uuid[]))),
                last_seen_at = GREATEST(COALESCE(last_seen_at, to_timestamp(0)),
                                        (SELECT MAX(captured_at) FROM sightings WHERE id = ANY($2::uuid[]))),
                updated_at = now()
            WHERE id = $1
            """,
            individual_id, newly,
        )
    else:
        # Neither side has an identity, so this brings one into existence --
        # the bootstrap case POST /proposal handles for a human `same`, minus
        # the human. `created_by_observer` stays NULL: nobody decided this.
        individual_id = new_uuid()
        minted = True
        await conn.execute(
            """
            INSERT INTO individuals (id, created_by, created_by_observer,
                                     created_via, status, first_seen_at,
                                     last_seen_at)
            SELECT $1, 'model', NULL, $2, 'active', MIN(captured_at), MAX(captured_at)
            FROM sightings WHERE id = ANY($3::uuid[])
            """,
            individual_id, METHOD, group,
        )

    if newly:
        # `individual_id IS NULL` in the predicate as well as in `newly`: the
        # guarantee that a linked row -- above all a human-confirmed one -- is
        # never moved should not depend on a list computed a few lines up.
        await conn.execute(
            "UPDATE sightings SET individual_id = $1, match_status = 'confirmed', "
            "updated_at = now() WHERE id = ANY($2::uuid[]) AND individual_id IS NULL",
            individual_id, newly,
        )
        for s in newly:
            # The resolving sighting is justified by its first partner; every
            # partner by the resolving sighting. Enough to find the pair again.
            matched = partners[0] if s == sighting_id else sighting_id
            await conn.execute(
                """
                INSERT INTO auto_links (id, sighting_id, individual_id,
                                        matched_sighting_id, method, content_sha256)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                new_uuid(), s, individual_id, matched, METHOD,
                partner_hash.get(s) or partner_hash[partners[0]],
            )

    cleared = await clear_settled_proposals(conn, individual_id)
    return IdenticalOutcome(
        "linked", individual_id=individual_id, partners=partners,
        newly_linked=newly, minted=minted, proposals_cleared=cleared,
    )


async def clear_settled_proposals(conn: asyncpg.Connection, individual_id: UUID) -> int:
    """Delete pending proposals whose answer is already known.

    A proposal between two sightings of the same individual, or from one of its
    sightings to the individual itself, asks a question the data has answered.
    Deleted rather than resolved: marking it `confirmed` would make it look like
    someone decided it, and pending proposals have no `confirmations` row to
    keep consistent -- the resolver already deletes and rewrites them freely.
    Proposals to anything *outside* the individual are left alone; whether
    another sighting is also this dog is still a real question.
    """
    result = await conn.execute(
        """
        DELETE FROM match_proposals mp
        USING sightings s
        WHERE mp.status = 'pending'
          AND s.id = mp.sighting_id
          AND s.individual_id = $1
          AND (mp.candidate_individual_id = $1
               OR mp.candidate_sighting_id IN
                  (SELECT id FROM sightings WHERE individual_id = $1))
        """,
        individual_id,
    )
    return int(result.split()[-1])
