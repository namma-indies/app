"""API-side vector provenance and the history of accepted wire recipes.

This release adds tags without changing preprocessing or either wire version.
Keep historical registrations: a different recipe must use NEW versions for
both pipelines. The source-identity test pins the preprocessing shared with the
worker, without changing any of the modules it hashes.

This is not worker attestation. Weight/source digests in completion payloads
still need a separate worker update before a second recipe is deployed.
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
        preprocessing="stored-webp90-yolo640-miew440-imagenet-l2-v1:"
                      "64d2300c1227f3b082ffbd5be48ece1016a9efe1fc0050e48522e40720100e45",
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
