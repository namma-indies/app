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

A clip is matched as a whole, on `captures.clip_sha256` (migration 0018): its
frames are sampled, not chosen, and carry no hash of their own. Clip and photo
hashes are kept apart -- a clip never links to a photo.

WHEN IT STEPS ASIDE
-------------------
A rule that is never wrong has to know when it is not sure. It falls back to an
ordinary proposal, and a person, when:

* **the photo shows more than one animal.** The same frame of two dogs says
  nothing about which dog either sighting is. Judged on the photo, not the
  sighting: if any copy of those bytes was found to hold two instances, every
  copy is ambiguous, including a /sighting copy whose pipeline only ever
  looked at the biggest box. For a clip, the test is whether any copy of it
  published more than one animal.
* **the sightings already belong to different dogs.** Folding two identities
  together is a merge of individuals, which nothing here is entitled to do --
  and a human verdict may be what put either one there.
* **`observations_compatible` refuses** -- two animals a capture separated, or
  a dog and a cat. Same check the human path makes.
* either side is rejected by a moderator, or ruled to hold no animal. A
  takedown that still seeds identities is a decision that did not stick.

A PHOTO LOGGED TWICE COUNTS ONCE
-------------------------------
Linking says the two are one dog; it would still say the dog was seen twice.
So a copy that adds no photo of its own -- every photo it holds is also in an
earlier sighting -- is marked `duplicate_of` that earlier sighting and drops
out of /map, /dogs and /stats (`countable_sighting()`). It stays in its
contributor's /dex. "Earlier" is submission order (`created_at`), so live it is
almost always the new upload that is the copy. A sighting with any photo the
other lacks is a second observation and keeps counting.

Known names carry over: a contributor's typed name is proposed for the dog,
exactly as after a human verdict, and stays a proposal.

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
CLIP_METHOD = "identical_clip"

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

# A sighting's photos as hashes, plus whether it has any uploaded photo with
# no hash -- one that predates the backfill. Such a sighting may hold a photo
# nothing else does, so it is never judged a copy. Crops are excluded from that
# test: a /capture sighting holds only crops, and is judged by its sources.
_HASH_SETS = """
    SELECT s.id, s.created_at, s.duplicate_of,
           ARRAY(
               SELECT p.content_sha256 FROM photos p
               WHERE p.sighting_id = s.id AND p.content_sha256 IS NOT NULL
               UNION
               SELECT src.content_sha256 FROM animal_instances a
               JOIN photos src ON src.id = a.source_photo_id
               WHERE a.sighting_id = s.id AND src.content_sha256 IS NOT NULL
               UNION
               -- Prefixed so a clip's hash can never stand in for a photo's.
               SELECT 'clip:' || c.clip_sha256 WHERE c.clip_sha256 IS NOT NULL
           ) AS hashes,
           -- A hashed clip covers the frames sampled from it.
           c.clip_sha256 IS NULL AND EXISTS (
               SELECT 1 FROM photos p
               WHERE p.sighting_id = s.id AND p.content_sha256 IS NULL
                 AND NOT EXISTS (SELECT 1 FROM animal_instances a
                                 WHERE a.evidence_photo_id = p.id)
           ) AS unhashed
    FROM sightings s LEFT JOIN captures c ON c.id = s.capture_id
    WHERE s.id = ANY($1::uuid[])
    ORDER BY s.created_at, s.id
"""

# Every sighting published from a capture whose clip has this hash. `animals`
# is how many sightings that capture published: a clip that held two dogs says
# both copies are the same pair, not which dog is which.
_CLIP_EVIDENCE = """
    SELECT s.id AS sighting_id, c.clip_sha256 AS content_sha256,
           (SELECT count(*) FROM sightings x WHERE x.capture_id = c.id) AS animals
    FROM captures c JOIN sightings s ON s.capture_id = c.id
    WHERE c.clip_sha256 = $1
"""

# Whether any copy of the clip was found to hold two animals in one frame, or
# published more than one. Read from the captures, not through their
# sightings: a copy still held for review has none, and is exactly the copy
# that knows the clip holds two dogs. A /sighting copy only ever looked at the
# biggest animal, so on its own it cannot tell -- the same accepted risk as a
# /sighting photo with no /capture copy.
_CLIP_AMBIGUOUS = """
    SELECT EXISTS (
        SELECT 1 FROM captures c WHERE c.clip_sha256 = $1 AND (
            (SELECT count(*) FROM sightings x WHERE x.capture_id = c.id) > 1
            OR EXISTS (SELECT 1 FROM animal_instances a WHERE a.capture_id = c.id
                       GROUP BY a.source_photo_id HAVING count(*) > 1))
    )
"""

_MY_CLIP = """
    SELECT c.clip_sha256 FROM sightings s JOIN captures c ON c.id = s.capture_id
    WHERE s.id = $1 AND c.clip_sha256 IS NOT NULL
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
    duplicates_marked: list[UUID] = field(default_factory=list)


async def link_identical(
    conn: asyncpg.Connection, sighting_id: UUID, *, new_uuid
) -> IdenticalOutcome | None:
    """Link `sighting_id` with every sighting holding one of its photos.

    None means the rule has nothing to say -- no hashed photo or clip, or no
    other sighting has one -- and the caller carries on exactly as before. A
    'skipped' outcome means it found a copy and declined, so the pair goes
    to a person.
    """
    hashes = [r["content_sha256"] for r in await conn.fetch(_MY_HASHES, sighting_id)]
    clip = await conn.fetchval(_MY_CLIP, sighting_id)
    if not hashes and clip is None:
        return None
    # Keyed by (method, hash), so a photo and a clip that happened to share a
    # digest are still two different pieces of evidence.
    evidence = [(r["sighting_id"], (METHOD, r["content_sha256"]), r["animals"])
                for r in (await conn.fetch(_EVIDENCE, hashes) if hashes else [])]
    if clip is not None:
        several = await conn.fetchval(_CLIP_AMBIGUOUS, clip)
        evidence += [(r["sighting_id"], (CLIP_METHOD, clip), 2 if several else r["animals"])
                     for r in await conn.fetch(_CLIP_EVIDENCE, clip)]

    ambiguous = {key for _, key, animals in evidence if animals > 1}
    partner_hash: dict[UUID, tuple[str, str]] = {}
    declined = False
    for sid, key, _ in evidence:
        if sid == sighting_id:
            continue
        if key in ambiguous:
            declined = True
            continue
        partner_hash.setdefault(sid, key)
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
        # Nothing to link, but the copy may still need marking and the queue
        # tidying below: a human `same` on a duplicate pair links it without
        # making it count once.
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
            individual_id, partner_hash[partners[0]][0], group,
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
            method, sha = partner_hash.get(s) or partner_hash[partners[0]]
            await conn.execute(
                """
                INSERT INTO auto_links (id, sighting_id, individual_id,
                                        matched_sighting_id, method, content_sha256)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                new_uuid(), s, individual_id, matched, method, sha,
            )

    duplicates = await mark_duplicates(conn, group)
    cleared = await clear_settled_proposals(conn, individual_id)

    # Same call, same table, same `proposed` status as after a human `same`:
    # the name is a contributor's claim about this dog, and the link only
    # settles which dog that is. Approval is still a separate, unbuilt policy.
    # Idempotent per sighting/name/identity, so re-running proposes nothing new.
    from app.routes.match import propose_known_names
    await propose_known_names(conn, individual_id, group)

    return IdenticalOutcome(
        "linked", individual_id=individual_id, partners=partners,
        newly_linked=newly, minted=minted, proposals_cleared=cleared,
        duplicates_marked=duplicates,
    )


async def mark_duplicates(conn: asyncpg.Connection, group: list[UUID]) -> list[UUID]:
    """Mark each sighting that adds no photo of its own as a copy of the
    earliest sighting that already holds all of them. Returns those marked.

    Only ever between sightings already linked to one dog, which is what makes
    hiding the copy safe: the dog keeps every photo, once.
    """
    rows = await conn.fetch(_HASH_SETS, group)
    originals: list = []  # earlier sightings that count, oldest first
    marked: list[UUID] = []
    for r in rows:
        mine = set(r["hashes"])
        if r["duplicate_of"] is not None:
            continue  # already a copy; never an original for anything new
        original = None
        if mine and not r["unhashed"]:
            original = next((o for o in originals if mine <= set(o["hashes"])), None)
        if original is None:
            originals.append(r)
            continue
        await conn.execute(
            "UPDATE sightings SET duplicate_of = $2, updated_at = now() "
            "WHERE id = $1 AND duplicate_of IS NULL",
            r["id"], original["id"],
        )
        # Anything that was a copy of this one is a copy of its original now,
        # so no chain of copies points at a sighting that no longer counts.
        await conn.execute(
            "UPDATE sightings SET duplicate_of = $2 WHERE duplicate_of = $1",
            r["id"], original["id"],
        )
        marked.append(r["id"])
    return marked


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
