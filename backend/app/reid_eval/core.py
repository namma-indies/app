"""Known-positive retrieval, policy loss accounting, and leakage-safe splits.

Unlabelled is unknown. Distinct identity IDs, vicinity and copy families never
create negative labels. Existing verdicts describe a selected population, not
unbiased city-wide recall. Current eligibility/history gaps make replay
counterfactual; the report says that even when the arithmetic is exact.
"""

import math
import random
from collections import Counter, defaultdict

from .schema import CAVEATS, STAGES, validate_metrics, validate_snapshot


class Groups:
    def __init__(self, ids):
        self.parent = {sid: sid for sid in ids}

    def find(self, sid):
        if self.parent[sid] != sid:
            self.parent[sid] = self.find(self.parent[sid])
        return self.parent[sid]

    def join(self, a, b):
        a, b = self.find(a), self.find(b)
        self.parent[max(a, b)] = min(a, b)


def near_hash_pairs(hashes, distance):
    """Index pairs (i, j) whose perceptual hashes differ in at most `distance`
    bits -- exactly those, without comparing every pair.

    Pigeonhole banding: split each hash into distance+1 bands; two hashes within
    `distance` bits agree on at least one band, so only hashes sharing a band
    value are compared. `hashes` is a list of (value, bit_length).
    """
    buckets = defaultdict(list)
    for index, (value, bits) in enumerate(hashes):
        bands = distance + 1
        for band in range(bands):
            low, high = band * bits // bands, (band + 1) * bits // bands
            buckets[bits, band, (value >> low) & ((1 << (high - low)) - 1)].append(index)
    found = set()
    for members in buckets.values():
        for x, i in enumerate(members):
            for j in members[x + 1:]:
                if (i, j) not in found and (hashes[i][0] ^ hashes[j][0]).bit_count() <= distance:
                    found.add((i, j))
    return found


def copy_families(observations, photos, phash_distance=4):
    """Conservative leakage groups, not animal identities. No hashes leave here."""
    groups = Groups(row["id"] for row in observations)
    captures, hashes, perceptual = {}, {}, []
    for row in observations:
        if row["capture_id"]:
            previous = captures.setdefault(row["capture_id"], row["id"])
            groups.join(previous, row["id"])
    for photo in photos:
        sid = str(photo["sighting_id"])
        if sid not in groups.parent:
            continue
        for sha, phash in ((photo["content_sha256"], photo["phash"]),
                           (photo.get("source_sha256"), photo.get("source_phash"))):
            if sha:
                previous = hashes.setdefault(sha, sid)
                groups.join(previous, sid)
            if phash:
                perceptual.append((int(phash, 16), 4 * len(phash), sid))
    for i, j in near_hash_pairs([(value, bits) for value, bits, _ in perceptual], phash_distance):
        groups.join(perceptual[i][2], perceptual[j][2])
    return {sid: groups.find(sid) for sid in groups.parent}


def label_graph(data):
    groups = Groups(row["id"] for row in data["observations"])
    verdicts = defaultdict(set)
    for label in data["labels"]:
        pair = tuple(sorted((label["a"], label["b"])))
        verdicts[pair].add(label["verdict"])
        if label["verdict"] == "same":
            groups.join(*pair)
    roots = {sid: groups.find(sid) for sid in groups.parent}
    bad = {roots[a] for (a, b), values in verdicts.items()
           if "different" in values and roots[a] == roots[b]}
    conflicts = {sid for sid, root in roots.items() if root in bad}
    conflict_pairs = sum("different" in values and roots[a] == roots[b]
                         for (a, b), values in verdicts.items())
    return roots, conflicts, conflict_pairs, verdicts


def split_plan(data, roots=None, conflicts=None):
    """Time holdout; a copy family spanning its boundary is quarantined.

    Earlier gallery context can be read by a later holdout query; gallery rows
    are not training examples. No holdout query is scored by default.
    """
    if roots is None or conflicts is None:
        roots, conflicts, _, _ = label_graph(data)
    rows, meta = data["observations"], data["meta"]
    times = sorted(row["created_ms"] for row in rows)
    at = max(0, len(times) - math.ceil(len(times) * meta["holdout_fraction"]))
    cutoff = times[at] if times else 0
    families = defaultdict(list)
    for row in rows:
        families[row["copy_family"]].append(row)
    plan = {}
    for family in families.values():
        crossing = min(r["created_ms"] for r in family) < cutoff <= max(r["created_ms"] for r in family)
        for row in family:
            plan[row["id"]] = ("quarantined" if crossing or row["id"] in conflicts else
                                "holdout" if row["created_ms"] >= cutoff else "development")
    assignments = {sid: {"split": part} for sid, part in plan.items()}
    counts = Counter(plan.values())
    return assignments, {"development": counts["development"], "holdout": counts["holdout"],
                         "quarantined": counts["quarantined"], "cutoff_ms": cutoff}


def ranking_metrics(ranked, relevant):
    hits, precision_sum = 0, 0.0
    for index, sid in enumerate(ranked, 1):
        if sid in relevant:
            hits += 1
            precision_sum += hits / index
    return (int(bool(ranked) and ranked[0] in relevant),
            int(bool(set(ranked[:5]) & relevant)),
            precision_sum / len(relevant) if relevant else 0.0)


def ratio(rows, seed=0):
    """Rows are (independence group, numerator, denominator); cluster bootstrap."""
    hits, total = sum(r[1] for r in rows), sum(r[2] for r in rows)
    groups = defaultdict(lambda: [0, 0])
    for group, numerator, denominator in rows:
        if denominator or numerator:
            groups[group][0] += numerator
            groups[group][1] += denominator
    result = {"hits": hits, "total": total, "groups": len(groups), "status": "estimated" if total else "undefined"}
    if not total:
        return result
    result["value"] = hits / total
    values = list(groups.values())
    if len(values) >= 2 and all(v[1] > 0 for v in values):
        rng, samples = random.Random(seed), []
        for _ in range(500):
            selected = rng.choices(values, k=len(values))
            denominator = sum(v[1] for v in selected)
            if denominator:
                samples.append(sum(v[0] for v in selected) / denominator)
        if samples:
            samples.sort()
            result.update(ci_low=samples[int((len(samples) - 1) * .025)],
                          ci_high=samples[int((len(samples) - 1) * .975)])
    return result


def independent(q, c):
    return (q["capture_id"] is not None and c["capture_id"] is not None
            and q["capture_id"] != c["capture_id"] and q["captured_day"] != c["captured_day"]
            and q["copy_family"] != c["copy_family"])


def junk(q, c, same_identity):
    return c["copy_family"] == q["copy_family"] or (same_identity and not independent(q, c))


def ranked(pairs, observations, score_key, *, policy=False):
    kept = [p for p in pairs if p[score_key] is not None and observations[p["candidate"]]["eligible"]]
    if policy:
        kept = [p for p in kept if p["species_ok"] and p["capture_ok"] and p["within_reach"]]
    return sorted(kept, key=lambda p: (-p[score_key], p["candidate"]))


def loss_stage(q, c, pair, ann_ranks, meta):
    if not q["embedded"]:
        return {"unscored": "unscored", "no_animal": "detection", "animal": "embedding"}[q["detection"]]
    if pair["model_score"] is None:
        return "gallery_vector"
    if not q["eligible"] or not c["eligible"]:
        return "eligibility"
    if not pair["species_ok"]:
        return "species"
    if not pair["capture_ok"]:
        return "capture"
    if not pair["within_reach"]:
        return "vicinity"
    if pair["ann_score"] is None:
        return "ann"
    if ann_ranks[pair["candidate"]] > meta["cap"]:
        return "cap"
    if pair["ann_score"] < meta["threshold"]:
        return "threshold"
    if q["review"] != "valid" or c["review"] != "valid":
        return "queue"
    return "proposed"


def evaluate(data, *, split="development", decision_id=None):
    validate_snapshot(data)
    if split not in ("development", "holdout") or (split == "holdout" and decision_id is None):
        raise ValueError("holdout scoring requires a recorded decision UUID")
    roots, conflicts, conflict_pairs, verdicts = label_graph(data)
    plan, split_counts = split_plan(data, roots, conflicts)
    observations = {row["id"]: row for row in data["observations"]}
    pairs = defaultdict(list)
    for pair in data["pairs"]:
        pairs[pair["query"]].append(pair)
    selected = [r for r in observations.values() if plan[r["id"]]["split"] == split and r["verified"]]
    funnel, slices = Counter({stage: 0 for stage in STAGES}), defaultdict(Counter)
    coverage, exact_coverage, top1, top5, ap, precision, exchange = [], [], [], [], [], [], []
    failures, unknown_before, prompts, positives, negatives, unknown, ann_losses = [], 0, 0, 0, 0, 0, 0
    meta = data["meta"]
    for query in selected:
        sid, group = query["id"], roots[query["id"]]
        query_pairs = [p for p in pairs[sid] if p["candidate"] not in conflicts]
        relevant = {p["candidate"] for p in query_pairs
                    if roots[p["candidate"]] == group and observations[p["candidate"]]["verified"]
                    and independent(query, observations[p["candidate"]])}
        # Standard re-ID junk: a copy of the query, or the same animal in a
        # non-independent encounter (same capture or day), is neither a hit
        # nor a miss for the model. Kept in the policy replay, which is about
        # what a reviewer would actually be shown.
        model = [p for p in ranked(query_pairs, observations, "model_score")
                 if not junk(query, observations[p["candidate"]], roots[p["candidate"]] == group)]
        ann = ranked(query_pairs, observations, "ann_score", policy=True)
        exact = ranked(query_pairs, observations, "policy_score", policy=True)
        ann_ranks = {p["candidate"]: rank for rank, p in enumerate(ann, 1)}
        def offered(rows, field):
            capped = rows[:int(meta["cap"])]
            return [
                p for p in capped
                if query["eligible"] and query["review"] == "valid"
                and observations[p["candidate"]]["review"] == "valid" and p[field] >= meta["threshold"]
            ]
        proposed, exact_proposed = offered(ann, "ann_score"), offered(exact, "policy_score")
        tp = fp = 0
        for pair in proposed:
            labels = verdicts.get(tuple(sorted((sid, pair["candidate"]))), set())
            if roots[pair["candidate"]] == group:
                tp += 1
            elif labels == {"different"}:
                fp += 1
            else:
                unknown += 1
        prompts += len(proposed)
        positives += tp
        negatives += fp
        precision.append((group, tp, tp + fp))
        exchange.append((group, fp, tp))
        covered = bool({p["candidate"] for p in proposed[:5]} & relevant)
        exact_covered = bool({p["candidate"] for p in exact_proposed[:5]} & relevant)
        metrics = (0, 0, 0.0)
        embedded_relevant = relevant & {p["candidate"] for p in model}
        if relevant:
            coverage.append((group, int(covered), 1))
            exact_coverage.append((group, int(exact_covered), 1))
            if not covered:
                failures.append(sid)
            ann_losses += int(exact_covered and not covered)
            for pair in query_pairs:
                if pair["candidate"] in relevant:
                    funnel[loss_stage(query, observations[pair["candidate"]], pair, ann_ranks, meta)] += 1
        if query["embedded"] and embedded_relevant:
            order = [p["candidate"] for p in model]
            metrics = ranking_metrics(order, embedded_relevant)
            top1.append((group, metrics[0], 1))
            top5.append((group, metrics[1], 1))
            ap.append((group, metrics[2], 1))
            for candidate in order:
                if candidate in embedded_relevant:
                    break
                if tuple(sorted((sid, candidate))) not in verdicts:
                    unknown_before += 1
        names = {query["species"] if query["species"] != "unknown" else "unknown_species",
                 query["media"], query["pipeline"] if query["pipeline"] != "unknown" else "unknown_pipeline",
                 "no_gps" if query["geo"] == "none" else "has_gps"}
        if query["video_recipe"] != "not_video":
            names.add(query["video_recipe"])
        names.update({"same": "same_observer", "different": "cross_observer", "unknown": "unknown_observer"}[p["observer_relation"]]
                     for p in query_pairs if p["candidate"] in relevant)
        for name in names:
            slices[name].update(verified=1, embedded_verified=int(query["embedded"]), repeats=int(bool(relevant)),
                                covered=int(bool(relevant) and covered), model_repeats=int(bool(embedded_relevant)),
                                top1=metrics[0], top5=metrics[1])
    all_rows = list(observations.values())
    inventory = {
        "encounters": len(all_rows), "verified": sum(r["verified"] for r in all_rows),
        "embedded": sum(r["embedded"] for r in all_rows),
        "unembedded_verified": sum(r["verified"] and not r["embedded"] for r in all_rows),
        "conflicted_encounters": len(conflicts), "conflicting_pairs": conflict_pairs,
        "orphan_labels": meta["orphan_labels"], "copy_families": len({r["copy_family"] for r in all_rows}),
        "unscored": sum(r["verified"] and not r["embedded"] and r["detection"] == "unscored" for r in all_rows),
        "detector_misses": sum(r["verified"] and not r["embedded"] and r["detection"] == "no_animal" for r in all_rows),
        "embedding_missing": sum(r["verified"] and not r["embedded"] and r["detection"] == "animal" for r in all_rows),
        "untagged_means": sum(r["mean_state"] == "untagged" for r in all_rows),
        "inactive_means": sum(r["mean_state"] == "inactive" for r in all_rows),
        "no_gps": sum(r["geo"] == "none" for r in all_rows),
        "asymmetric_no_gps_pairs": sum(p["within_reach"] != p["reverse_within_reach"] for p in data["pairs"]),
    }
    result = {
        "schema": 1, "snapshot_id": meta["snapshot_id"], "recipe_id": meta["recipe_id"],
        "policy_id": meta["policy_id"],
        "protocol": {key: meta[key] for key in ("threshold", "cap", "ann_shortlist", "phash_distance", "holdout_fraction", "seed", "ann_replay", "versioning")},
        # Always true of v0: vector availability is `embeddings.created_at`,
        # which a re-embed upsert resets, so a re-embedded gallery looks newer
        # than it was and drops out of earlier queries.
        "caveats": sorted(CAVEATS),
        "status": "inconclusive" if len(coverage) < 20 or len({r[0] for r in coverage}) < 2 or meta["truncated"] else "observational",
        "split": split, "replay": meta["replay"], "label_scope": "existing_verdicts",
        "inventory": inventory,
        "populations": {"verified_encounters": len(selected), "embedded_verified_encounters": sum(r["embedded"] for r in selected),
                        "repeat_encounters": len(coverage), "model_repeat_encounters": len(top1),
                        "repeat_components": len({r[0] for r in coverage})},
        "model": {"top1": ratio(top1, meta["seed"]), "top5": ratio(top5, meta["seed"]),
                  "mean_ap": ratio(ap, meta["seed"]), "unknown_before_first_true": unknown_before},
        "policy": {"coverage_at_5": ratio(coverage, meta["seed"]), "exact_coverage_at_5": ratio(exact_coverage, meta["seed"]),
                   "ann_losses": ann_losses, "prompts": prompts, "labelled_same": positives, "labelled_different": negatives,
                   "unknown_prompts": unknown, "precision_on_labelled_prompts": ratio(precision, meta["seed"]),
                   "wrong_per_correct": ratio(exchange, meta["seed"]), "reviewer_timing_available": 0},
        "funnel": dict(funnel), "slices": {name: dict(counts) for name, counts in slices.items()},
        "failures": len(failures), "split_inventory": split_counts,
    }
    if decision_id is not None:
        result["decision_id"] = decision_id
    validate_metrics(result)
    return result
