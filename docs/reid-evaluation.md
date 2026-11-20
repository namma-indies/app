# Re-ID evaluation v0 (#227, #189)

This is engineering measurement tooling, not a validated benchmark or a new
matching policy. It fetches no media, runs no models and writes no database
rows. Existing verdicts are selected by the current queue. Results from them
do not establish population recall, calibrated probabilities or safe merges.

The primary planned outcome is repeat-encounter coverage at five. Reviewer
effort remains audited correct links per minute; v0 explicitly reports that
decision timing is unavailable. The trusted label set and timed adjudication
are separate work. This tool does not change or take ownership of PR #85.

## Running it

Aswin runs `snapshot` in the source environment using an explicitly supplied
`REID_EVAL_DATABASE_URL` for a read-only database role. The command also enforces
a repeatable-read, read-only transaction. It has no default connection to the
application database. Use a private output directory and `nice` plus `--sleep`
to keep the serial CPU work out of the application's way.

**Run it against a streaming replica or a restored backup, not the primary.**
The snapshot holds one long repeatable-read transaction and runs a vector walk
per verified query; on the primary that pins old row versions and competes
with uploads. The command refuses any server that is not in recovery unless
`--allow-primary` is given. A restored backup is a primary as far as Postgres
can tell, so it needs the flag too; pointing it at production's primary should
be a deliberate, off-hours decision. Every statement runs under
`statement_timeout` (`REID_EVAL_STATEMENT_TIMEOUT_MS`, default 120 s, or
`--statement-timeout-ms`), so a bad plan fails the run instead of dragging on.

```sh
uv run python scripts/reid_eval.py snapshot --out SNAPSHOT.json --sleep 0.05
uv run python scripts/reid_eval.py score --snap SNAPSHOT.json --out METRICS.json
```

The private snapshot stays in that environment. Only the aggregate metrics
file leaves it. Neither belongs in this public repository. Outputs have mode
0600 and refuse overwrite. `--limit` is a smoke test over the earliest records,
not a sample; its report is always inconclusive.

No production run was performed by the implementation worker. Tests use
fixture Postgres, synthetic vectors and fabricated labels, without media.

## Populations and truth

The inventory includes every sighting, even without a vector. Pairs are
built only for verified queries, the only ones ever scored: the inventory
needs counts, not an all-against-all matrix. The verified
population is deliberately conservative: an explicit positive moderator
animal override, or involvement in a human `same` verdict. A negative animal
override wins. A contributor publishing a detector-generated group is not
automatically a verified animal label. This misses real animals without an
explicit ruling; the inventory reports the unverified population through the
difference in counts rather than assuming detector outputs are ground truth.

Ranking truth is the graph of explicit human `same` confirmations. Explicit
`different` confirmations remain negatives. A contradictory negative inside a
positive component quarantines that component and increments conflict counts.
Proposal statuses are never labels: a competing proposal closed as rejected
by a `same` verdict supplies no negative. Orphaned confirmations are counted
when their former proposal no longer identifies both endpoints.

Distinct identity IDs, distant cells and unreviewed co-visible animals never
become negatives. Copy families are leakage groups, never identity labels.
Unknown prompts stay unknown, including when computing precision or effort.

The report separates all verified query encounters from the embedded subset.
Repeat encounters need an earlier verified positive at a different capture,
on a different captured day in Asia/Kolkata, outside the same copy family.
No-vector encounters remain in the policy denominator. Model top-1/top-5 and
mean AP require a usable query and a known positive with an available vector.
They describe retrieval of known positives under incomplete labels.

## Replay and funnel

Model mode uses max cosine over per-frame pairs, without vicinity, species,
candidate cap or threshold; the base gallery eligibility still applies. Policy
mode uses the resolver's cached mean when usable, otherwise its per-frame
probe. Model top-1/top-5/mAP drop junk candidates, as is standard in re-ID:
a copy of the query, or the same animal in a non-independent encounter (same
capture or same day). They are neither hits nor misses for the model. The
policy replay keeps them, because a reviewer would be shown them. I-2 tags are used automatically once that code/schema is present;
code/schema disagreement fails closed. Until then, baseline means are marked
untagged and recipe provenance is explicitly unversioned.

Gallery uploads precede the query, and gallery vectors must be available by
the query's last recorded vector time. Halfvec ANN replays production's query
shape: a walk of the whole index for the active model and recipe, with the
production cast expression, shortlist size and iterative-scan setting, then
the query's eligibility applied to what the walk returned, in order (located
queries walk up to `REID_EVAL_ANN_WALK` rows, default 20,000). Restricting the
walk to eligible IDs would be planned as a primary-key fetch and an exact
sort, making ANN loss zero by construction. The index holds today's rows, so
this too is counterfactual. Exact
and ANN coverage are both reported. Vicinity and its reverse ordering are
computed only between `coarse_point` cell centres in SQL. Neither coordinates
nor distances enter the snapshot.
Prompt workload, labelled/unknown counts and precision use the full configured
candidate cap. The two coverage-at-five metrics separately use the first five
visible proposals, even when that configured cap is larger.

The positive-pair funnel records the first loss: unscored, detector miss,
embedding missing, missing gallery vector, eligibility, species, capture
separation, vicinity, ANN, cap, threshold, shared-queue visibility, or proposed.
Confirmed, rejected and pending review outcomes are not invented from a
counterfactual ranking. Species, media kind, pipeline, video-recipe path,
location availability and observer relation appear as aggregate slices.

All replay is labelled **counterfactual current state**. Vector creation times
are the availability proxy, and every metrics file carries the caveat
`availability_reset_by_reembed`: a re-embed upsert resets `created_at`, so a
re-embedded gallery frame looks newer than it was and drops out of earlier
queries; commit times, earlier vector versions, historical
eligibility, identity membership and queue contents were not fully recorded.
The source fingerprint identifies current code/configuration, not the weights
that generated historical vectors. V0 cannot reconstruct those missing facts.
Metrics retain the numeric evaluation settings and an opaque policy fingerprint
of matching code/settings; no location-derived distance is exported. Exact
score ties are ordered by candidate UUID for reproducibility.

## Leakage, uncertainty and decisions

Exact content hashes, source-photo hashes, same-capture membership and near
perceptual hashes group copy families before splitting. Near hashes are found
by pigeonhole banding (distance + 1 bands; only hashes sharing a band are
compared), which finds exactly the all-pairs answer without comparing all
pairs. The default near-copy
Hamming distance is four: a conservative engineering choice, not a dog-identity
threshold. Inspect family sizes before a real comparison; scene similarities
can over-group unrelated animals and reduce effective sample size.

The most recent 30% of encounter times are reserved. Development queries are
strictly earlier than the cutoff. A copy family spanning that boundary is
quarantined. Historical gallery context is not a fitted example. (v0 had
cross-validation folds that nothing consumed; they are gone until a fitted
policy needs them.)

Scoring defaults to development only. Holdout scoring requires both
`--split holdout` and `--decision-id` with the UUID of a predeclared decision.
The decision must reserve the holdout, outcomes, slices and at most two
challengers before reading scores. V0 scores the champion only and does not
maintain a decision ledger; the decision record and that experiment budget
remain an operator responsibility.

Rates have a cluster-bootstrap interval when at least two independent human
components are available. Undefined rates are omitted, not printed as zero.
Fewer than 20 repeat encounters produces an inconclusive report. More labels
still produces an observational report, not a validated policy claim. Reliable
comparisons need the independent trusted set; a confidence interval does not
remove selection bias.

CI checks known-answer rankings, both denominators, stage attribution,
conflicts, unknown labels, split invariants, actual snapshot SQL/ANN, asymmetric
no-GPS orderings, no database mutation, and positive output schemas. Snapshots
allow only their declared IDs, scores, flags, times and enum slices. Metrics
allow only aggregate counts/rates, enum values and snapshot/recipe/decision
identifiers, with an opaque policy identifier. Coordinates, distances, observer IDs, media references, vectors,
hashes and free text have no output field. These checks establish mechanics
and output safety; they do not measure street-dog identification accuracy.
