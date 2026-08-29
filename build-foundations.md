# build foundations (for Claude Code)

Working name. Company GitHub, AWS/Lightsail hosting. Read this before scaffolding anything.

---

## 0. What we're building

A crowdsourced system that photographs street dogs (via WhatsApp), estimates the **population** of dogs in an area over time, and — the hard part — re-identifies **individual** dogs across sightings so a photo history can become a longitudinal health record.

Two consumers of one pipeline, at two confidence thresholds:
- **Population layer** — mark-resight estimate with honest confidence intervals. Runs on loose/aggregate match signal; errors wash out on average.
- **Individual layer** — a confirmed dog identity + history. Requires high-confidence match, ideally human-confirmed by a feeder or a clinic.

**The core technical problem is open-set individual re-identification with a strong geo/time prior.** Everything else is deliberately boring plumbing so effort concentrates there.

**Species.** Dogs are the focus, but the pipeline is species-agnostic. Every `sighting` and `individual` carries a `species` tag (`dog` | `cat` | `other`), set at intake. Street cats are a first-class-but-secondary population — re-ID candidate retrieval and population estimates both run *per species* (never match across species). On the public surface cats stay a quiet easter egg, not a headline.

## 1. Keep this in mind (the heart)

Every sighting starts anonymous and points at an **individual slot that may stay empty for months** until the model earns confidence or a feeder fills it. That empty-slot-waiting-to-be-named is the emotional and architectural center of the system. Design so a "sighting" can *always* be linked to an "individual" later, even if it begins as a nameless street photo. Recognition-as-love (a feeder confirming "that's Kaju") is also recognition-as-label (a training pair). The two are the same event in the data model.

Public surface = aggregate only. Never expose an individual dog's precise recent location publicly. Code open; data not.

## 2. Architecture principles

- **Single Lightsail VM, single Postgres, Python end-to-end** for the pilot. Resist microservices, Kubernetes, Elasticsearch, Redis. You do not need them.
- Postgres does triple duty: relational + **PostGIS** (spatial) + **pgvector** (embeddings). The re-ID candidate query is geo-prefilter → vector-rank in one SQL statement.
- Embedding compute is the only heavy step and **does not need to be real-time** — async background worker, CPU is fine at pilot volume. No GPU on day one.
- Photos in object storage (S3 / Lightsail buckets); store the key in Postgres, never blobs in the DB.
- `open source (code) != open data`. Two separate release decisions, designed in from the start.

## 3. Stack

| Concern | Choice | Notes |
|---|---|---|
| Host | Lightsail VM (Ubuntu) | one box for pilot |
| DB | PostgreSQL + PostGIS + pgvector | relational + spatial + vector |
| API / backend | Python + FastAPI (async) | one language; shares model code |
| Jobs | Postgres-backed job table to start; `arq`/Celery later | async embedding + matching |
| Embeddings | MegaDescriptor (via `wildlife-tools`, HuggingFace) and/or DINOv2 | animal re-ID foundation model; WildFusion-style global+local fusion later |
| Object storage | S3 / Lightsail buckets | photo keys in DB |
| Intake | WhatsApp (Meta Cloud API or Gupshup; or reuse Lucky's pattern) | media webhook — fiddliest real-world piece |
| Frontend | Static site + MapLibre/Leaflet | aggregate heatmap, one endpoint |

## 4. Data model (sketch)

```
observers        id, phone_hash, display_name?, trust_tier, home_geog?, created_at
sightings        id, observer_id -> observers,
                 photo_key, captured_at, geog (POINT, PostGIS),
                 species ('dog'|'cat'|'other'),             -- tagged at intake
                 embedding (vector, pgvector),
                 individual_id -> individuals NULL,        -- the empty slot
                 match_status ('unmatched'|'proposed'|'confirmed'),
                 attrs jsonb ( ear_notch?, collar?, notes ),
                 created_at
individuals      id, species ('dog'|'cat'|'other'),
                 name?, first_seen_at, last_seen_at, territory_geog?,
                 created_by ('model'|'feeder'), status, notes
match_proposals  id, sighting_id, candidate_individual_id, score, method,
                 status ('pending'|'confirmed'|'rejected'), resolved_by?, created_at
confirmations    id, sighting_id, individual_id, observer_id,
                 verdict ('same'|'different'), created_at   -- these ARE training labels
clinical_records id, individual_id, visit_date, procedure ('vaccine'|'spay'|'treat'),
                 vet, notes                                  -- Soulmates/Belgaum
```

Key invariant: `sightings.individual_id` is nullable and gets promoted from NULL → set only on high-confidence model match or a `confirmations.verdict = 'same'`. Population estimation never waits on this; individual records do.

## 5. Re-ID pipeline

1. **Ingest** — WhatsApp media webhook → store photo to bucket → create `sighting` (geo, time, observer) → enqueue embed job.
2. **Embed** — background worker runs MegaDescriptor/DINOv2 → write `sightings.embedding`. CPU/async.
3. **Candidate retrieval (the one elegant query)** — geo+time prefilter, then vector rank:

```sql
SELECT s.individual_id, i.name,
       s.embedding <=> :q_embedding AS visual_dist
FROM sightings s
JOIN individuals i ON i.id = s.individual_id
WHERE ST_DWithin(s.geog, :q_geog, 300)              -- geo prior: ~300m
  AND s.captured_at > now() - interval '30 days'     -- time prior
  AND s.species = :q_species                         -- never match across species
  AND s.individual_id IS NOT NULL
ORDER BY visual_dist
LIMIT 10;
```

4. **Decide** by threshold:
   - `visual_dist` very small → auto-link (`match_status='confirmed'`), high bar.
   - middling → `match_status='proposed'`, create `match_proposals`, route to a feeder whose `home_geog` covers the sighting (geo routes the expert automatically).
   - no candidate → new `individual` slot (or leave unmatched; population layer still counts it).
5. **Confirm** — feeder verdict writes `confirmations` → promotes/links the sighting and **becomes a labeled pair** for the next training round.
6. **Population estimate** — separate job consumes resight signal (even loose) → mark-resight estimator with CIs. See §7 caveat.

## 6. Self-supervised label mining (bootstrap without experts)

Cold-start the embedding without waiting for feeders:
- Sightings **≤ ~50m apart within a few hours** → probable **positive** pairs (dogs are territorial, small home ranges). Free hard-positives across light/pose/day.
- Far apart + visually dissimilar → probable **negatives**.
- Mine these as pseudo-labels to fine-tune MegaDescriptor/DINOv2 (ArcFace/triplet). Noisy but abundant; directly attacks cold start. Feeder confirmations then supervise on top.

## 7. First experiment (do this before betting on the street version)

**Measure the individuating information content of street-dog photos, at Soulmates/Belgaum**, where ground truth exists (the clinic knows which dog is which across visits).
- Build a small labeled set from clinic intake photos.
- Score re-ID (rank-1 / rank-5, verification ROC) with: DINOv2 zero-shot, MegaDescriptor zero-shot, MegaDescriptor + geo/time prior.
- Output the number that decides scope: given a candidate pool of ~N nearby dogs, what accuracy can appearance hit? That tells us how far the individual layer can go and how much load the geo prior must carry.

**Sampling-bias correction for the population estimate** is the other real research task: opportunistic crowdsourcing over-samples friendly/photogenic/high-traffic dogs. Capture effort/coverage metadata per area; model detection probability; stratify/weight, and run the estimate per species. Minimum viable signal is "sightings + a defensible model of *where we looked*," not raw sighting counts.

## 8. MVP scope (v1)

- WhatsApp intake → store sighting (photo, geo, time, phone-hash identity).
- Bot first-reply: safety instruction (photograph from distance, do not approach).
- Async embed + candidate retrieval + threshold decision.
- Feeder confirmation flow (even if minimal — a link to confirm/reject a proposed match).
- Public aggregate heatmap (density; later, coverage overlay) — no per-dog pages.
- Population estimate with CIs for pilot wards.
- Belgaum validation harness (§7).

## 9. Non-goals (do NOT build yet)

- No GPU provisioning, no real-time embedding.
- No Elasticsearch, Redis, microservices, or separate API tier.
- No public per-dog profile/location pages.
  > **Status, 2026-08-29:** still held. Per-dog cards *do* exist now (the Dogs
  > tab, `GET /dogs`), but behind `require_observer` over a passcode- and
  > allowlist-gated cohort — they are internal, not public, so this non-goal is
  > intact rather than superseded. Issue #5 proposes relaxing it deliberately
  > (individual profiles public; precise-current-predictable location gated),
  > which would be a decision to record here, not a drift to discover.
- No admin console beyond what's needed to run the pilot.
- No gait/video signal (future).
- No nose-print pipeline for the street (only potentially relevant in-clinic; ~99% accuracy figures are close-range controlled-capture, irrelevant to street photos).

## 10. Guardrails as code

- **Aggregate-only public surface.** Individual-level location data is internal/clinic-facing only, access-controlled.
  > **Status, 2026-08-29:** held, and currently trivially so — there is no
  > unauthenticated *data* surface. Every route that returns a sighting, a
  > photo or an individual sits behind `require_observer`. What is open carries
  > no data: the sign-in doors (`/join`, `/auth/*`), `/health`, and the static
  > `/.well-known/apple-app-site-association` the native apps need.
  > Note that *within* the cohort, `/map` and `/dogs` both show full precision;
  > that is a deliberate trade that holds only while the cohort is vetted, and
  > it is what issue #5's `resolve_precision` exists to replace before anything
  > opens up.
- **Phone-backed identity** (store `phone_hash`, not raw numbers).
- **Privacy**: strip/avoid persisting people's faces, plates, house numbers where feasible; documented position.
- **Estimates carry uncertainty** — API never returns a point population number without its CI.
- **Data policy separate from code license.** Code = MIT/open. Data = restricted; nothing that resolves a vulnerable individual animal's whereabouts goes public.

## 11. References

- Belsare & Vanak 2020, *Sci Rep* 10, doi:10.1038/s41598-020-75828-6 (rebound/denominator).
- Gibson et al. 2022, *Nat Commun* (Goa; WVS App coordination model).
- WildlifeDatasets / MegaDescriptor (WACV 2024) — animal re-ID foundation model, HuggingFace.
- WildFusion 2024 — calibrated fusion of deep-similarity + local matching for zero-shot re-ID.
- iNaturalist (github.com/inaturalist) — community-ID pattern to adapt (MIT, but do NOT fork; borrow the design).
