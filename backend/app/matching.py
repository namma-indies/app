"""Candidate search: given a new photo's embedding, find sightings nearby that
might be the same animal.

Two stages, in this order for a reason:

1. **Vicinity filter** (PostGIS). Street dogs hold small territories, so a
   candidate 40 km away is almost never the same animal, and searching globally
   makes look-alike collisions grow with the whole database instead of with the
   neighbourhood.
2. **Vector search** (pgvector HNSW), then an exact re-rank. The index is built
   on a `halfvec` cast because HNSW refuses >2000 dimensions and MiewID is
   2152, so the ANN stage is fp16. Half precision is fine for *finding*
   candidates and not fine for *deciding* between them, so the shortlist is
   re-scored against the full-precision column before anything is returned.

This module deliberately stops at a ranked list. It does not decide "same dog"
-- that needs thresholds calibrated on real uploads (see THRESHOLD NOTE below),
and the decision belongs to the caller.

THRESHOLD NOTE
--------------
Offline, MiewID-msv3 separates known from unknown individuals at open-set
AUC 0.961 over 1,336 identities, which is what makes a two-threshold scheme
viable at all. The *specific* cosine cut-offs are not portable: they depend on
the photo distribution, and re-encoding shifts the score distribution even when
ranking barely moves (d' fell 4.58 -> 3.34 across a JPEG quality sweep while
top-1 held). So the thresholds live in settings, start unset, and should be
fitted to the first few hundred human verdicts in `confirmations`.
"""

from dataclasses import dataclass
from uuid import UUID

import asyncpg
import numpy as np

from app.embed import EMBED_DIM, MODEL_NAME

# How many neighbours the ANN stage pulls before exact re-ranking. Larger costs
# little (the exact pass is a dot product over a shortlist) and protects
# against fp16 mis-ranking near the top.
ANN_SHORTLIST = 50


@dataclass
class Candidate:
    sighting_id: UUID
    photo_id: UUID
    individual_id: UUID | None
    similarity: float  # cosine in [-1, 1]; exact, full precision
    distance_m: float | None


def _to_pgvector(vec: np.ndarray) -> str:
    """pgvector's text input format. asyncpg has no native vector codec, so we
    pass a literal and let Postgres parse it."""
    if vec.shape != (EMBED_DIM,):
        raise ValueError(f"expected ({EMBED_DIM},), got {vec.shape}")
    return "[" + ",".join(f"{float(v):.7g}" for v in vec) + "]"


async def find_candidates(
    conn: asyncpg.Connection,
    vecs: np.ndarray | list[np.ndarray],
    *,
    lat: float | None,
    lng: float | None,
    radius_m: float,
    exclude_sighting_id: UUID | None = None,
    limit: int = 10,
) -> list[Candidate]:
    """Ranked candidate sightings for a query, nearest first.

    `vecs` is every embedding the query sighting has -- one for a photo, several
    for a clip. A candidate scores the **best** match against any query frame,
    because the frames are the same animal from different angles and the
    question is "have we seen this dog", not "have we seen this pose". Measured:
    querying a 6-frame clip with one frame ranked the wrong dog first, while
    max-over-frames put both sightings of the right dog at the top.

    Each vector must be L2-normalised (embed.py guarantees this), which is what
    makes cosine distance and inner product interchangeable here.

    With no location the vicinity filter is skipped rather than returning
    nothing: a sighting with GPS denied is still worth matching, just against a
    wider pool.
    """
    if isinstance(vecs, np.ndarray) and vecs.ndim == 1:
        vecs = [vecs]
    if not len(vecs):
        return []
    qs = [_to_pgvector(v) for v in vecs]
    has_geo = lat is not None and lng is not None

    # The ANN ORDER BY must use the identical cast expression as the index
    # (ix_embeddings_vec_miew_hnsw) or Postgres silently falls back to a
    # sequential scan -- correct results, quietly terrible performance. The
    # LATERAL runs one indexed probe per query frame and unions the results,
    # so recall grows with the number of frames instead of being decided by
    # whichever frame happened to be first.
    # Placeholders are numbered as they are appended rather than fixed, because
    # a parameter that appears in the parameter list but nowhere in the SQL has
    # no inferable type -- Postgres raises IndeterminateDatatypeError. That is
    # exactly what happened to sightings with no GPS, the case this function
    # claims to support: the geo predicates vanished and $3/$4 became untypeable.
    params: list = [qs, MODEL_NAME, exclude_sighting_id]
    p_vecs, p_model, p_excl = "$1", "$2", "$3"
    if has_geo:
        params += [lng, lat, radius_m]
        p_lng, p_lat, p_radius = "$4", "$5", "$6"
        geo_filter = (
            f"AND s.geog IS NOT NULL AND ST_DWithin(s.geog, "
            f"ST_SetSRID(ST_MakePoint({p_lng}, {p_lat}), 4326)::geography, {p_radius})"
        )
        distance = (
            f"ST_Distance(s.geog, ST_SetSRID("
            f"ST_MakePoint({p_lng}, {p_lat}), 4326)::geography)"
        )
    else:
        geo_filter = ""
        distance = "NULL::double precision"

    sql = f"""
        WITH q AS (
            SELECT unnest({p_vecs}::text[])::vector({EMBED_DIM}) AS v
        ),
        shortlist AS (
            SELECT DISTINCT hits.photo_id, hits.vec_miew, hits.sighting_id
            FROM q
            CROSS JOIN LATERAL (
                SELECT e.photo_id, e.vec_miew, p.sighting_id
                FROM embeddings e
                JOIN photos p ON p.id = e.photo_id
                JOIN sightings s ON s.id = p.sighting_id
                WHERE e.model = {p_model}
                  AND e.vec_miew IS NOT NULL
                  AND ({p_excl}::uuid IS NULL OR s.id <> {p_excl}::uuid)
                  {geo_filter}
                ORDER BY e.vec_miew::halfvec({EMBED_DIM}) <=> q.v::halfvec({EMBED_DIM})
                LIMIT {ANN_SHORTLIST}
            ) AS hits
        ),
        scored AS (
            SELECT sl.sighting_id,
                   sl.photo_id,
                   MAX(1 - (sl.vec_miew <=> q.v)) AS similarity
            FROM shortlist sl CROSS JOIN q
            GROUP BY sl.sighting_id, sl.photo_id
        )
        SELECT DISTINCT ON (sc.sighting_id)
               sc.sighting_id,
               sc.photo_id,
               s.individual_id,
               sc.similarity,
               {distance} AS distance_m
        FROM scored sc
        JOIN sightings s ON s.id = sc.sighting_id
        ORDER BY sc.sighting_id, sc.similarity DESC
    """
    # One row per candidate sighting (its best-matching photo); re-sort by score
    # and cut, which DISTINCT ON cannot do in the same pass.
    sql = f"SELECT * FROM ({sql}) ranked ORDER BY similarity DESC LIMIT {int(limit)}"

    if has_geo:
        # An HNSW scan walks ef_search candidates and *then* applies the WHERE
        # clause, so a selective filter can return far fewer rows than LIMIT --
        # or none -- because everything the index surfaced sat outside the
        # radius. Tightening the radius makes that strictly worse, which is
        # exactly the direction this went. Iterative scan keeps walking until
        # enough rows survive the filter. `relaxed_order` is safe here because
        # the shortlist is exact re-ranked and re-sorted afterwards anyway.
        async with conn.transaction():
            await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            rows = await conn.fetch(sql, *params)
    else:
        rows = await conn.fetch(sql, *params)
    return [
        Candidate(
            sighting_id=r["sighting_id"],
            photo_id=r["photo_id"],
            individual_id=r["individual_id"],
            similarity=float(r["similarity"]),
            distance_m=float(r["distance_m"]) if r["distance_m"] is not None else None,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Decision layer
# ---------------------------------------------------------------------------

@dataclass
class MatchOutcome:
    status: str  # 'confirmed' | 'proposed' | 'unmatched'
    individual_id: UUID | None
    candidates: list[Candidate]
    proposal_ids: list[UUID]
    # True when a candidate cleared the bar but this sighting is thin evidence
    # (few frames). Asking for a short clip then beats asking for a yes/no: one
    # photo identifies the right dog 37% of the time, eight frames 83%. The
    # contributor is usually still standing next to the animal when this fires.
    suggest_video: bool = False


async def resolve_sighting(
    conn: asyncpg.Connection,
    sighting_id: UUID,
    *,
    auto_merge_min: float,
    propose_min: float,
    radius_m: float,
    max_candidates: int,
    new_uuid,
    thin_evidence_frames: int = 0,
) -> MatchOutcome:
    """Decide what a freshly embedded sighting is, and persist that decision.

    Three outcomes, per build-foundations.md section 5:

      similarity >= auto_merge_min   link to that individual        ('confirmed')
      similarity >= propose_min      write match_proposals, ask     ('proposed')
      otherwise                      leave alone                    ('unmatched')

    Deliberately does NOT mint a new `individual` for the unmatched case. An
    individual is an identity claim, and creating one per unmatched sighting
    would fill the table with singletons that later have to be merged back --
    the population layer can count sightings without them. Identities are
    created when a human confirms a match, not when the model fails to find one.

    Idempotent: re-running for the same sighting replaces its pending
    proposals, so a re-embed after a model upgrade does not accumulate
    duplicates.
    """
    # Every frame of this sighting, not just the first. A clip contributes six
    # or so; using one of them throws away the evidence the clip was for.
    rows = await conn.fetch(
        """
        SELECT e.vec_miew::text AS vec,
               ST_Y(s.geog::geometry) AS lat,
               ST_X(s.geog::geometry) AS lng
        FROM sightings s
        JOIN photos p ON p.sighting_id = s.id
        JOIN embeddings e ON e.photo_id = p.id AND e.model = $2
        WHERE s.id = $1 AND e.vec_miew IS NOT NULL
        ORDER BY e.created_at
        """,
        sighting_id,
        MODEL_NAME,
    )
    if not rows:
        # No embedding yet (or no animal found). Nothing to decide.
        return MatchOutcome("unmatched", None, [], [])

    row = rows[0]  # lat/lng belong to the sighting, so any row carries them
    vecs = [
        np.array([float(x) for x in r["vec"].strip("[]").split(",")], dtype=np.float32)
        for r in rows
    ]
    cands = await find_candidates(
        conn,
        vecs,
        lat=row["lat"],
        lng=row["lng"],
        radius_m=radius_m,
        exclude_sighting_id=sighting_id,
        limit=max_candidates,
    )

    # A human verdict outranks the model, permanently.
    #
    # Re-running resolution used to overwrite whatever it found. For a sighting
    # someone had already confirmed, where the model no longer proposes anything
    # above the bar, the UPDATEs below set individual_id = NULL and
    # match_status = 'unmatched' -- silently erasing the verdict.
    #
    # Reachable, not theoretical: `backfill_embeddings.py --resolve` calls this
    # over existing sightings, and it is exactly what you run after a model or
    # threshold change -- both of which move scores, which is the case that
    # triggers it. Since auto_merge_min is deliberately unreachable, a human
    # verdict is the ONLY way a sighting becomes 'confirmed', so this destroyed
    # the scarcest data in the system: the labelled pairs the thresholds are
    # meant to be fitted against.
    #
    # `confirmations` keeps the audit trail, so the loss is recoverable in
    # principle -- but nothing reads it back, and the dog quietly loses the
    # sighting in the meantime.
    settled = await conn.fetchrow(
        "SELECT individual_id, match_status FROM sightings WHERE id = $1", sighting_id
    )
    if settled is not None and settled["match_status"] == "confirmed":
        return MatchOutcome("confirmed", settled["individual_id"], [], [])

    # Clear any previous pending proposals for this sighting before rewriting.
    await conn.execute(
        "DELETE FROM match_proposals WHERE sighting_id = $1 AND status = 'pending'",
        sighting_id,
    )

    if not cands:
        await conn.execute(
            # Belt-and-braces against the early return above: an unlink
            # must never touch a human-confirmed row.
            "UPDATE sightings SET match_status='unmatched', individual_id=NULL "
            "WHERE id=$1 AND match_status <> 'confirmed'",
            sighting_id,
        )
        return MatchOutcome("unmatched", None, [], [])

    best = cands[0]

    if best.similarity >= auto_merge_min and best.individual_id is not None:
        await conn.execute(
            "UPDATE sightings SET individual_id=$2, match_status='confirmed' "
            "WHERE id=$1",
            sighting_id,
            best.individual_id,
        )
        await conn.execute(
            "UPDATE individuals SET last_seen_at = GREATEST("
            "  COALESCE(last_seen_at, to_timestamp(0)),"
            "  (SELECT captured_at FROM sightings WHERE id=$2)), updated_at=now() "
            "WHERE id=$1",
            best.individual_id,
            sighting_id,
        )
        return MatchOutcome("confirmed", best.individual_id, cands, [])

    proposable = [c for c in cands if c.similarity >= propose_min]
    if not proposable:
        await conn.execute(
            # Belt-and-braces against the early return above: an unlink
            # must never touch a human-confirmed row.
            "UPDATE sightings SET match_status='unmatched', individual_id=NULL "
            "WHERE id=$1 AND match_status <> 'confirmed'",
            sighting_id,
        )
        return MatchOutcome("unmatched", None, cands, [])

    proposal_ids: list[UUID] = []
    for c in proposable:
        pid = new_uuid()
        await conn.execute(
            """
            INSERT INTO match_proposals
                (id, sighting_id, candidate_individual_id, candidate_sighting_id,
                 score, method, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'pending')
            """,
            pid,
            sighting_id,
            c.individual_id,
            c.sighting_id,
            c.similarity,
            MODEL_NAME,
        )
        proposal_ids.append(pid)

    await conn.execute(
        "UPDATE sightings SET match_status='proposed' WHERE id=$1", sighting_id
    )
    # Thin evidence is judged on the query sighting, not the candidate: it is
    # the contributor in front of us who can still go and film the animal.
    thin = len(vecs) < thin_evidence_frames
    return MatchOutcome("proposed", None, cands, proposal_ids, suggest_video=thin)
