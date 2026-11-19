"""API-side vector provenance and the history of accepted wire recipes.

This release adds tags without changing preprocessing or either wire version.
Keep historical registrations: a different recipe must use NEW versions for
both pipelines. The source-identity test pins the preprocessing shared with the
worker, without changing any of the modules it hashes.

This is not worker attestation. Weight/source digests in completion payloads
still need a separate worker update before a second recipe is deployed.

BEFORE BUMPING ACTIVE_RECIPE: CAPTURE PHOTOS HAVE NO RE-EMBED PATH YET
--------------------------------------------------------------------------
Every read filters on the active recipe, so a bump makes every vector made
under the old one invisible to matching until it is re-made. Two gaps stand
in the way, and a bump must close both first:

* `scripts/backfill_embeddings.py` only re-embeds /sighting photos
  (`p.capture_id IS NULL`). A /capture (v2) vector is of a *crop* the worker
  cut from a source frame by its own detection, so the API cannot re-make it
  from the stored photo the way the backfill does; that needs a worker job
  that re-runs crop + embed for existing captures and writes rows with the
  new recipe. Until it exists, every /capture sighting drops out of matching
  on a bump.
* `captures.publish_groups` upserts the sighting mean (`vec_miew`) on
  conflict, so a capture re-published under the new recipe overwrites the
  cached mean; that is the intended path for such a job, but nothing calls
  it for old captures today.

Not fixed here because it is a worker feature, not a tag: it needs a job
type, a lease and a completion contract (#226).
"""

from dataclasses import dataclass

ACTIVE_RECIPE = "stored-webp90-yolo640-miew440-imagenet-l2-mean-v1"


@dataclass(frozen=True)
class Recipe:
    name: str
    legacy_pipeline: str
    multi_pipeline: str
    preprocessing: str


RECIPES = (
    Recipe(
        name="stored-webp90-yolo640-miew440-imagenet-l2-mean-v1",
        legacy_pipeline="stored-webp-q90-yolo26x-miewid-msv3-v1",
        multi_pipeline="stored-webp-q90-yolo26x-miewid-msv3-multi-v2",
        # Re-pinned when the launch stack met #211: GpuInference now also
        # reports scene signals for upload screening. Detection boxes, crops
        # and vectors are untouched, so the recipe and both versions stand.
        preprocessing="stored-webp90-yolo640-miew440-imagenet-l2-v1:"
                      "e662cfd229d503a7893180d76f218b3afd4435e5386460edaed405c99afff2d6",
    ),
)


def validate_recipe_versions(recipe, legacy_pipeline, multi_pipeline, preprocessing, *, recipes=RECIPES):
    """Reject an unregistered recipe, source drift, or reuse of either version."""
    active = next((r for r in recipes if r.name == recipe), None)
    if active is None:
        raise ValueError("register the recipe and bump both pipeline versions")
    if (active.legacy_pipeline, active.multi_pipeline, active.preprocessing) != (
            legacy_pipeline, multi_pipeline, preprocessing):
        raise ValueError("recipe, source and both pipeline versions must agree")
    for previous in recipes:
        if previous.name != recipe and (
                previous.legacy_pipeline == legacy_pipeline or previous.multi_pipeline == multi_pipeline):
            raise ValueError("a recipe change must bump both pipeline versions")
