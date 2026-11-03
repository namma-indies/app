"""Positive schemas for private snapshots and exportable metrics.

No permissive metadata dictionary or free text. A new field needs an explicit
schema decision; checking for a few forbidden words would miss nested leaks.
"""

import math
from uuid import UUID

SPECIES = {"dog", "cat", "unknown"}
MEDIA = {"photo", "burst", "video"}
PIPELINES = {"legacy", "media_v1", "capture_v2", "unknown"}
GEO = {"located", "none"}
STAGES = {"unscored", "detection", "embedding", "gallery_vector", "eligibility",
          "species", "capture", "vicinity", "ann", "cap", "threshold", "queue", "proposed"}
VIDEO_RECIPES = {"not_video", "legacy_frames", "media_v1_frames", "capture_v2_frames", "unknown_frames"}
SLICES = (SPECIES - {"unknown"}) | MEDIA | (PIPELINES - {"unknown"}) | (VIDEO_RECIPES - {"not_video"}) | {
    "unknown_species", "unknown_pipeline", "no_gps", "has_gps", "same_observer", "cross_observer", "unknown_observer"}


def keys(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError("fields outside the allowlist or required fields missing")


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("expected a finite number")


def uuid(value, *, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str):
        raise ValueError("expected a UUID")
    try:
        UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("expected a UUID") from exc


def enum(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError("value outside enum allowlist")


def boolean(value):
    if type(value) is not bool:
        raise ValueError("expected a boolean")


def score(value):
    if value is not None:
        number(value)
        if not -1.000001 <= value <= 1.000001:
            raise ValueError("cosine outside range")


def validate_snapshot(data):
    keys(data, {"meta", "observations", "pairs", "labels"})
    meta = data["meta"]
    keys(meta, {"schema", "snapshot_id", "recipe_id", "as_of_ms", "threshold", "cap",
                "ann_shortlist", "phash_distance", "holdout_fraction", "seed", "folds",
                "versioning", "replay", "orphan_labels", "truncated"})
    for field in ("snapshot_id", "recipe_id"):
        uuid(meta[field])
    for field in ("schema", "as_of_ms", "threshold", "cap", "ann_shortlist", "phash_distance",
                  "holdout_fraction", "seed", "folds", "orphan_labels"):
        number(meta[field])
    if meta["schema"] != 1 or not 0 < meta["holdout_fraction"] < 1 or meta["cap"] < 1 or meta["folds"] < 2:
        raise ValueError("unsupported snapshot settings")
    enum(meta["versioning"], {"versioned", "baseline_unversioned"})
    enum(meta["replay"], {"counterfactual_current_state"})
    boolean(meta["truncated"])
    ids, order = set(), {}
    for row in data["observations"]:
        keys(row, {"id", "capture_id", "created_ms", "captured_day", "available_ms", "verified",
                   "embedded", "eligible", "review", "species", "media", "pipeline", "geo",
                   "copy_family", "detection", "probe", "mean_state", "video_recipe"})
        uuid(row["id"])
        uuid(row["capture_id"], nullable=True)
        uuid(row["copy_family"])
        if row["id"] in ids:
            raise ValueError("duplicate observation")
        ids.add(row["id"])
        for field in ("created_ms", "captured_day", "available_ms"):
            number(row[field])
        order[row["id"]] = (row["created_ms"], row["id"])
        for field in ("verified", "embedded", "eligible"):
            boolean(row[field])
        for field, choices in (("review", {"valid", "pending", "rejected"}), ("species", SPECIES),
                               ("media", MEDIA), ("pipeline", PIPELINES), ("geo", GEO),
                               ("video_recipe", VIDEO_RECIPES),
                               ("detection", {"animal", "no_animal", "unscored"}),
                               ("probe", {"mean", "frames", "none"}),
                               ("mean_state", {"active", "untagged", "inactive", "absent"})):
            enum(row[field], choices)
    seen = set()
    for row in data["pairs"]:
        keys(row, {"query", "candidate", "model_score", "policy_score", "ann_score",
                   "within_reach", "reverse_within_reach", "species_ok", "capture_ok", "observer_relation"})
        for field in ("query", "candidate"):
            uuid(row[field])
            if row[field] not in ids:
                raise ValueError("unknown pair endpoint")
        pair = row["query"], row["candidate"]
        if pair[0] == pair[1] or pair in seen:
            raise ValueError("duplicate or self pair")
        seen.add(pair)
        for field in ("model_score", "policy_score", "ann_score"):
            score(row[field])
        for field in ("within_reach", "reverse_within_reach", "species_ok", "capture_ok"):
            boolean(row[field])
        enum(row["observer_relation"], {"same", "different", "unknown"})
    expected = {(q, c) for q in ids for c in ids if order[c] < order[q]}
    if seen != expected:
        raise ValueError("snapshot must contain the complete earlier-gallery pair matrix")
    for row in data["labels"]:
        keys(row, {"a", "b", "verdict", "source"})
        for field in ("a", "b"):
            uuid(row[field])
            if row[field] not in ids:
                raise ValueError("unknown label endpoint")
        enum(row["verdict"], {"same", "different"})
        enum(row["source"], {"owner", "stranger", "moderator", "unknown"})


def validate_metrics(data):
    keys(data, {"schema", "snapshot_id", "recipe_id", "status", "split", "replay", "label_scope",
                "inventory", "populations", "model", "policy", "funnel", "slices", "failures",
                "split_inventory"}, {"decision_id"})
    if data["schema"] != 1:
        raise ValueError("unsupported metrics schema")
    uuid(data["snapshot_id"])
    uuid(data["recipe_id"])
    if "decision_id" in data:
        uuid(data["decision_id"])
    enum(data["status"], {"inconclusive", "observational"})
    enum(data["split"], {"development", "holdout"})
    enum(data["replay"], {"counterfactual_current_state"})
    enum(data["label_scope"], {"existing_verdicts"})
    fields = {
        "inventory": {"encounters", "verified", "embedded", "unembedded_verified", "conflicted_encounters",
                      "conflicting_pairs", "orphan_labels", "copy_families", "unscored", "detector_misses",
                      "embedding_missing", "untagged_means", "inactive_means", "no_gps", "asymmetric_no_gps_pairs"},
        "populations": {"verified_encounters", "embedded_verified_encounters", "repeat_encounters", "model_repeat_encounters", "repeat_components"},
        "model": {"top1", "top5", "mean_ap", "unknown_before_first_true"},
        "policy": {"coverage_at_5", "exact_coverage_at_5", "ann_losses", "prompts", "labelled_same",
                   "labelled_different", "unknown_prompts", "precision_on_labelled_prompts", "wrong_per_correct",
                   "reviewer_timing_available"},
        "split_inventory": {"development", "holdout", "quarantined", "components", "folds", "cutoff_ms"},
    }
    ratio_fields = {"hits", "total", "groups", "status"}
    ratios = {("model", key) for key in ("top1", "top5", "mean_ap")} | {
        ("policy", key) for key in ("coverage_at_5", "exact_coverage_at_5", "precision_on_labelled_prompts", "wrong_per_correct")}
    for section, allowed in fields.items():
        keys(data[section], allowed)
        for field, value in data[section].items():
            if (section, field) in ratios:
                keys(value, ratio_fields, {"value", "ci_low", "ci_high"})
                enum(value["status"], {"estimated", "undefined"})
                for key, item in value.items():
                    if key != "status":
                        number(item)
            else:
                number(value)
    keys(data["funnel"], STAGES)
    for value in data["funnel"].values():
        number(value)
    if set(data["slices"]) - SLICES:
        raise ValueError("unknown slice")
    for row in data["slices"].values():
        keys(row, {"verified", "embedded_verified", "repeats", "covered", "model_repeats", "top1", "top5"})
        for value in row.values():
            number(value)
    number(data["failures"])
