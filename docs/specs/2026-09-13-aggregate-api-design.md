# Aggregate API, the area layer, and basic rate limiting — design

_Author: Claude, 2026-09-13. Product calls (ward-first-with-PIN-code-fallback,
area definitions modular rather than hardcoded, public counts kept separate from
naming weight, cohort-gated first, rate limiting now rather than later) made by
Akash; this doc covers the technical shape._

Answers the first tier of issue #58, the coarsening unit from #5, and takes the
one naming decision with a closing window from #4. Deliberately does **not**
answer #56 (standing), #55 (what a name is), or #58's tiers 2–4.

## Goal

Make IndieDex able to answer, over an API:

- How many people have logged how many sightings of how many dogs.
- How many of each are in a given area.
- How that has moved, month by month, and when an area was last surveyed.

Without ever answering "where is this particular dog, right now."

## Scope

**In:**

1. An **area layer** where "area" is a pluggable kind, not a hardcoded unit.
2. Three read-only **aggregate endpoints**, cohort-gated.
3. **Basic rate limiting**, covering the auth surface as well as the new one.
4. The append-only **`individual_names`** table — migration only, no behaviour.

**Out, and why:**

| Not building | Why |
|---|---|
| API tokens, scoping, revocation | #58 tiers 2–4. Nobody outside the cohort is being given access yet; tokens are a real subsystem, not a flag. |
| `public_read` DB-role enforcement | Tier 1 returns derived counts, never rows. A DB role buys nothing here and costs a second enforcement point to keep in sync. Revisit at tier 3, where real rows leave the building. |
| Opt-in public observer profiles | Needs a privacy policy that matches (#53 says the current one is already stale). No closing window — the column is free to add later. |
| Standing derivation, precedence, vouching, name-response scoring | #4, #55 and #56 all recommend deferring. Agreed. |
| `observers.public_profile_opt_in` | Speculative column with no reader. Unlike `individual_names`, adding it later costs nothing. |
| Lockouts, per-address cooldowns, captcha | "Basic" rate limiting was the ask. Auth hardening is its own issue. |
| A `/v1` prefix | Matches the existing `/map`, `/dex`, `/dogs`. Versioning is worth introducing when third parties get tokens, not before. |

## Part 1 — the area layer

`areas` has existed since `0001` (`id`, `name`, `geog geography(MultiPolygon,4326)`),
is **empty**, and no application code has ever referenced it. It has no spatial
index.

### Area is a kind

The unit is data, not code. Wards, PIN codes, neighbourhood outlines and
hand-drawn pilot polygons are all rows differing only by `kind`. Every query
takes a `kind`; the default lives in config.

This is what makes the ward/PIN-code question reversible. If BBMP ward data
turns out to be unusable, falling back is one loader run and one config line —
not a schema change and not new query code. Both can also coexist: the same
sighting is counted once per kind, so a ward view and a PIN-code view are two
readings of one corpus rather than two pipelines.

### Migration `0008_areas_kind`

```sql
ALTER TABLE areas ADD COLUMN kind text NOT NULL DEFAULT 'unknown';
ALTER TABLE areas ALTER COLUMN kind DROP DEFAULT;
ALTER TABLE areas ADD COLUMN ext_code text;
CREATE UNIQUE INDEX ux_areas_kind_ext_code ON areas (kind, ext_code)
    WHERE ext_code IS NOT NULL;
CREATE INDEX ix_areas_kind ON areas (kind);
CREATE INDEX ix_areas_geog ON areas USING GIST (geog);
```

The add-default-then-drop-default dance is deliberate: `areas` is believed empty
on prod, but a `NOT NULL` column added without a default aborts the boot if that
belief is wrong, and `entrypoint.sh` is `set -e` — a failed migration takes the
site down rather than leaving it un-updated (see `docs/WORKFLOW.md`). This
form is correct either way.

`ext_code` is the source system's own identifier (ward number, PIN code). It is
what makes a re-load an update rather than a duplicate, and what lets someone
else's dataset join to ours.

### The loader

`backend/scripts/load_areas.py`, generic over kind:

```
uv run python scripts/load_areas.py \
    --geojson bbmp-wards.geojson \
    --kind bbmp_ward \
    --name-field KGISWardName \
    --code-field KGISWardNo \
    [--prune] [--dry-run]
```

- Idempotent: upserts on `(kind, ext_code)`, so re-running after a boundary
  revision updates in place.
- Coerces `Polygon` to `MultiPolygon`; rejects anything else rather than
  silently storing a geometry the queries can't use.
- Asserts SRID 4326.
- **Warns on overlapping polygons within a kind** — it means counts will be
  ambiguous. It warns rather than fails, because real administrative data has
  slivers, and the read path is built to be correct anyway (below).
- `--prune` removes rows of that kind absent from the file; off by default, so
  a partial file can't silently delete the map.

The GeoJSON itself is not committed. It is public data, but full-resolution
ward polygons are megabytes of payload in a repo whose value is the code; the
source URL and the exact loader invocation are documented in the private ops
repo alongside the other operational facts.

## Part 2 — the aggregate endpoints

```
GET /stats?kind=…               city-wide totals + monthly series
GET /stats/areas?kind=…         per-area rows
GET /stats/areas/{id}           one area, with its own monthly series
```

All three require a signed-in observer — the same `require_observer` dependency
as `/map` and `/dex`. Opening them to the public is a dependency swap, and is
gated on rate limiting existing (it now will), the #53 privacy policy update,
and having looked at real numbers first.

### What is a countable sighting

One predicate, defined once in `backend/app/aggregates.py`:

```
s.review_status <> 'rejected'
```

`/map` and `/dogs` each inline this today and `/dex` omits it (issue #54). They
are repointed at the shared constant so the three can't drift — the bug where
one surface forgets is already in the tracker, and three copies is how it
happened.

This predicate is only as honest as the data behind it. Issue #54's cleanup of
the indoor test sightings is assigned and in progress; **no number from these
endpoints should be quoted publicly until it has landed.** The code is correct
either way, which is why this does not block the build.

### Three numbers, never one called "dogs"

`sightings.match_status` defaults to `'unmatched'` and `individuals` only began
filling on 2026-08-29. So:

| Field | Meaning |
|---|---|
| `sightings` | Countable sightings. Overcounts dogs — one dog seen ten times is ten. |
| `confirmed_individuals` | `COUNT(DISTINCT individual_id)` where non-null. Undercounts dogs — every unmatched sighting is invisible to it. |
| `observers` | Distinct observers with at least one countable sighting. |

The true dog count is somewhere between the second and the first, and the API
does not pretend to know where. Returning one number labelled "dogs" would be a
guess wearing a fact's clothes.

### Attributing a sighting to an area

Computed on read, via a lateral join per sighting:

```sql
LEFT JOIN LATERAL (
    SELECT a.id, a.name, a.ext_code
    FROM areas a
    WHERE a.kind = $1 AND ST_Covers(a.geog, s.geog)
    ORDER BY a.id
    LIMIT 1
) a ON s.geog IS NOT NULL
```

`LIMIT 1` is load-bearing, not defensive: if two polygons of a kind overlap, a
plain join counts that sighting twice and the per-area rows silently exceed the
city total. This makes double-counting structurally impossible regardless of
data quality, at the cost of attributing a sliver arbitrarily-but-stably
(`ORDER BY a.id`).

Computed on read rather than stored: no backfill, and no invalidation problem
when boundaries are revised — which they will be. If it ever gets slow the seam
is a cached `sightings.area_id` filled by the existing `jobs` table. Not built.

### Suppression, and the three numbers that make totals reconcile

An area is returned only if it clears **both** thresholds for its kind. Below
either, the area is **omitted entirely** — not returned with nulls and a
`suppressed: true` flag, because that flag still discloses "at least one dog is
here", which is the presence fact the whole exercise protects.

Thresholds are **per kind**, in config:

```python
area_min_sightings: dict[str, int] = {"bbmp_ward": 5, "pin_code": 20}
area_min_observers:  dict[str, int] = {"bbmp_ward": 2, "pin_code": 3}
# Applied to any kind not named above.
area_min_sightings_default: int = 5
area_min_observers_default: int = 2
```

Per-kind rather than global because the threshold protects a privacy property
and that property depends on cell size. A Bangalore PIN code and a BBMP ward
differ by roughly an order of magnitude in area, so one number cannot be right
for both — and under the fallback-to-PIN-codes plan, a global threshold would
silently become the wrong number with no code change to notice it.

Because areas drop out, per-area rows will not sum to the city total. Two
further things break that arithmetic, and neither is suppression:

- A sighting with `geog IS NULL` — `geo_source` explicitly allows `'none'`.
- A sighting falling inside no polygon of that kind — outside the loaded set.

So every response carries three reconciling numbers: the city total,
`areas_suppressed`, and `unattributed_sightings`. Without them the first person
to add up the columns in front of a partner concludes that dogs went missing.

### Two things suppression does *not* cover, stated deliberately

**City-wide totals are never suppressed.** "12 people, 340 sightings, 18 dogs"
names no place, so there is no cell to be small. If the whole corpus is one
sighting, that fact is already visible to anyone who can sign in.

**An area-month cell is smaller than the area total, and is floored
separately.** An area can clear the threshold on its lifetime numbers while a
single month inside it holds one sighting — which is a finer disclosure than
the area row it sits under. Monthly rows within an area are therefore omitted
below the same `area_min_sightings` floor. The area's own totals still include
them, so the series deliberately does not sum to the area total, for the same
reason and with the same honesty as the reconciling counts above.

### Time is monthly, and that is the only grain there is

`date_trunc('month', captured_at)`. No daily or weekly buckets, and
`last_active_month` is coarsened to a month too.

This is #5's "delay" dial, enforced by the absence of an API that can ask a
finer question rather than by a rule someone has to remember. A ward that was
"last surveyed in September" cannot be read as "he was there twenty minutes
ago".

### Response shapes

```jsonc
// GET /stats?kind=bbmp_ward
{
  "kind": "bbmp_ward",
  "totals": { "observers": 12, "sightings": 340, "confirmed_individuals": 18 },
  "months": [
    { "month": "2026-08", "observers": 9, "sightings": 128, "confirmed_individuals": 11 }
  ],
  "areas_reported": 7,
  "areas_suppressed": 11,
  "unattributed_sightings": 23
}

// GET /stats/areas?kind=bbmp_ward
{
  "kind": "bbmp_ward",
  "areas": [
    { "id": "…", "name": "Domlur", "ext_code": "112",
      "sightings": 84, "confirmed_individuals": 6, "observers": 4,
      "last_active_month": "2026-09" }
  ],
  "areas_suppressed": 11,
  "unattributed_sightings": 23
}

// GET /stats/areas/{id}
// `months` omits any month below the area_min_sightings floor for this kind,
// so the series does not sum to the area total. See suppression, above.
{ "area": { "…": "…" }, "months": [ /* … */ ] }
```

A suppressed area requested by id returns **404**, identical to an id that does
not exist. A distinct 403 would turn the endpoint into an oracle for exactly the
fact suppression exists to withhold.

An unknown `kind`, or a kind with no rows loaded, returns an empty `areas` list
with `unattributed_sightings` equal to the countable total — not a 404. "No
polygons loaded" is a legitimate state of the system, and it reads honestly:
every sighting is unattributed because nothing has been defined to attribute it
to.

## Part 3 — basic rate limiting

There is **no throttling anywhere in the application today.**

### Identity first, IP only as a fallback

The key is `observer_id` when the request is authenticated, and the client IP
only when it is not. Every endpoint in Part 2 is authenticated, so the new
surface never consults a client-controllable header at all.

### It covers the auth surface too

A limiter on `/stats` alone would protect the endpoint that needs it least.

| Surface | Limit | Key | Why |
|---|---|---|---|
| `/stats*` | 60 / min | observer | Generous; exists so a runaway client can't spin the DB. |
| Request an email login link | 5 / 15 min | IP **and** email | SES has production sending access (50k/day). Unthrottled, this is both a cost exposure and a way to mail-bomb a third party. |
| `/join` passcode | 10 / 15 min | IP | A shared passcode with unlimited attempts is a shared passcode with no passcode. |

Two keys on the email endpoint rather than one is the single borderline call
against "basic": IP alone lets a rotating attacker hammer one address and lets
one NAT block a whole office. It is two fixed-window counters, not a lockout
system.

### Mechanics

`backend/app/ratelimit.py` — fixed-window counters in process memory, applied as
a **per-route dependency** rather than global middleware, so each surface can
carry its own limit. Over-limit returns `429` with `Retry-After`.

In-process, no Redis. The container runs a single `uvicorn` with no `--workers`,
so in-memory state is genuinely global and the counts are exact. Two honest
consequences, recorded here so neither is a later surprise:

- **Limits reset on deploy.** Acceptable for abuse-dampening; not acceptable if
  this ever becomes a quota.
- **Adding workers silently multiplies every limit** by the worker count. If
  `--workers` is ever added, this file is the thing to revisit.

The counter store is **bounded** — a fixed-window map keyed by client IP with no
eviction is itself the denial-of-service. Expired windows are swept on write and
the map is capped, evicting oldest-first at the cap.

### Getting the client IP right

Caddy is the only thing in front of the app (`deploy/Caddyfile`), and the app
container is reachable only through it on the compose network.

Rather than depend on Caddy's default append-vs-replace behaviour for
`X-Forwarded-For` — where getting it backwards is not a bug but a **bypass**,
since one attacker-supplied header makes the limiter key on a value they
control — the Caddyfile sets a single-valued header from the real peer:

```
{$APP_DOMAIN} {
    reverse_proxy app:8000 {
        header_up X-Real-IP {http.request.remote.host}
    }
}
```

`header_up` overwrites any client-supplied value. The app reads `X-Real-IP` when
`trust_proxy_header` is set, and `request.client.host` otherwise (local dev,
tests). **This must be verified empirically against the deployed box** — a
`curl -H "X-Forwarded-For: 1.2.3.4" -H "X-Real-IP: 1.2.3.4"` with the resolved
key logged — before the limiter is relied on for anything.

## Part 4 — `individual_names` (migration only)

In scope solely because #4 and #55 both identify it as the one decision with a
closing window: free while no names exist, expensive afterwards. **Nothing reads
or writes this table in this change.**

### Migration `0009_individual_names`

```sql
CREATE TABLE individual_names (
    id uuid PRIMARY KEY,
    individual_id uuid NOT NULL REFERENCES individuals(id),
    name text NOT NULL,
    proposed_by uuid REFERENCES observers(id),
    status text NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed','active','superseded','rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_by uuid REFERENCES observers(id),
    resolved_at timestamptz
);
CREATE INDEX ix_individual_names_individual_id ON individual_names (individual_id);
CREATE UNIQUE INDEX ux_individual_names_active
    ON individual_names (individual_id) WHERE status = 'active';
```

- **Append-only by convention, enforced where it counts.** The partial unique
  index is the real invariant: at most one `active` name per individual, which
  is precisely what `individuals.name` caches. The database now refuses the
  inconsistent state rather than trusting application code to avoid it.
- **`proposed_by` is nullable**, answering #4's open question: a name can arrive
  from a model suggestion or a WhatsApp intake with no account behind it. A
  `NOT NULL` here would force a fake observer row for every such name, which is
  worse than an honest null.
- `individuals.name` / `named_by` / `named_at` are **left in place and
  documented as a cache** of the winning row. Same pattern as
  `sightings.match_status` caching the confirmation log: reads stay cheap, the
  log stays authoritative. No code change, because no code writes names yet.

## Testing

Postgres-backed, via the existing `migrated_db` / `authed_client` fixtures.

Area fixtures are **two small synthetic polygons inserted by the test**, not
real ward data — the suite must not depend on a GeoJSON that isn't in the repo.

| Case | Asserts |
|---|---|
| Suppression boundary | An area at exactly the threshold appears; one sighting below, it vanishes and `areas_suppressed` increments. |
| Observer threshold | An area with plenty of sightings but one observer is suppressed. |
| Unattributed | `geog IS NULL` and outside-all-polygons both land in `unattributed_sightings`, neither in an area row. |
| Rejected excluded | A `review_status='rejected'` sighting counts nowhere. |
| Overlap | Two overlapping polygons of one kind: the sighting is counted once. |
| Kind isolation | Polygons of a second kind don't appear under the first, and totals are unchanged. |
| Month bucketing | Sightings in two months produce two rows; nothing finer than a month is ever emitted. |
| Thin month | A month below the floor is dropped from an area's series while still counting in its totals. |
| City totals unsuppressed | A one-sighting corpus still returns real city-wide numbers. |
| Suppressed by id | 404, indistinguishable from a nonexistent id. |
| No polygons loaded | Empty `areas`, all sightings unattributed, 200 not 404. |
| Auth | All three endpoints 401 unauthenticated. |
| Limiter | 429 after the limit with `Retry-After`; two observers have independent budgets; the store stays bounded under many distinct keys. |

## Open risks

1. **Ward data availability is the one thing that can't be settled from the
   repo.** BBMP was subject to a governance restructuring (the Greater Bengaluru
   legislation) that may mean "the BBMP ward list" is a live question rather
   than a lookup. The loader is indifferent to which vintage is loaded, and the
   `kind` design means a wrong choice is reversible — but the boundary set must
   be dated and its provenance recorded at load time, not inferred later. If
   what is available is stale or unusable, PIN codes are the fallback and the
   change is a config line.
2. **Numbers are only as clean as #54.** Assigned, in progress, not blocking the
   build. Blocking any public quotation of a figure.
3. **Suppression thresholds are a starting guess.** Cohort-gating exists so we
   can see what they hide against real data before a stranger sees a number.
4. **Rate limits reset on deploy and multiply with workers.** Both acceptable
   now, both wrong if this ever becomes a quota or scales out.
