"""End-to-end re-ID on real photographs.

Everything else in the suite proves the plumbing: a sighting saves, a vector is
stored, a query returns rows. None of it proves the pipeline can actually tell
two dogs apart, because the vectors elsewhere are synthetic. This is the test
that would catch a silently broken embedder -- a changed preprocessing
convention, a swapped model, a lost L2 normalisation -- none of which break any
other test in this repo.

It needs photographs of real dogs with known identities, which are not
committed here. Populate `tests/fixtures/dogs/` with:

    <name>_0.jpg, <name>_1.jpg     two photos of the same dog
    <other>_0.jpg, <other>_1.jpg   two photos of a different dog

Two individuals is the minimum; more is better, and photos from different days
are worth far more than two frames of one moment. Street sightings captured
through the app itself are ideal -- they are the distribution this actually runs
on, unlike anything from a controlled setting.

Skips when the directory is empty or the ONNX weights are absent, so it never
blocks CI. That means a green CI run does NOT include this check; it runs on a
machine with weights and fixtures.

Current fixtures are three street dogs photographed on a phone, EXIF stripped
(every one carried GPS). Two of the three are beagle-types, which is the point:
the check is not "light dog versus dark dog". Measured:

    lemon_0   vs lemon_1       0.6109   same dog
    lemon_1   vs tricolour_0   0.4028   different, and the hardest pair
    brindle_0 vs anything      ~0.147   different

    margin +0.2082
"""

import itertools
from pathlib import Path

import pytest

from app import detect_reid, embed as embed_mod

FIXTURES = Path(__file__).parent / "fixtures" / "dogs"


def _pairs():
    """(name, path) for every fixture, grouped by the leading identity token."""
    if not FIXTURES.is_dir():
        return []
    out = []
    for p in sorted(FIXTURES.glob("*.jpg")) + sorted(FIXTURES.glob("*.webp")):
        name = p.stem.rsplit("_", 1)[0]
        out.append((name, p))
    return out


_FIX = _pairs()
_NAMES = {n for n, _ in _FIX}
_HAS_MODELS = detect_reid._MODEL_PATH.exists() and embed_mod._MODEL_PATH.exists()

pytestmark = pytest.mark.skipif(
    not _HAS_MODELS or len(_NAMES) < 2,
    reason=(
        "needs ONNX weights and >= 2 identities in tests/fixtures/dogs/ "
        f"(found {len(_NAMES)} identities, weights={_HAS_MODELS})"
    ),
)


@pytest.fixture(scope="module")
def vectors():
    """Embed every fixture through the real capture path: detect, crop, embed."""
    from app.embed import embed_photo

    out = {}
    for name, path in _FIX:
        found = embed_photo(path.read_bytes())
        assert found is not None, f"no animal detected in {path.name}"
        out[path.stem] = (name, found[0])
    return out


def test_every_fixture_yields_a_unit_vector(vectors):
    """A vector that is not unit-length means normalisation was lost, which
    silently turns every cosine score into nonsense while nothing raises."""
    import numpy as np

    for key, (_, v) in vectors.items():
        assert v.shape == (embed_mod.EMBED_DIM,), key
        assert np.isfinite(v).all(), key
        assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-3, f"{key} not L2-normalised"


def test_same_dog_scores_above_different_dogs(vectors):
    """The one property the whole feature rests on.

    Deliberately a comparison, not a threshold: absolute scores move with the
    encoding and the photo distribution (see the sweep in AGENTS.md), so pinning
    a number here would make this test fail for reasons that are not bugs. The
    ordering is what must hold.
    """
    same, diff = [], []
    for a, b in itertools.combinations(sorted(vectors), 2):
        (na, va), (nb, vb) = vectors[a], vectors[b]
        (same if na == nb else diff).append((float(va @ vb), a, b))

    assert same, "no same-dog pair in fixtures -- name them <dog>_0, <dog>_1"
    assert diff, "no different-dog pair in fixtures -- need >= 2 identities"

    worst_same = min(same)
    best_diff = max(diff)
    assert worst_same[0] > best_diff[0], (
        f"worst same-dog pair {worst_same[1]}/{worst_same[2]}={worst_same[0]:.4f} "
        f"did not beat best different-dog pair "
        f"{best_diff[1]}/{best_diff[2]}={best_diff[0]:.4f}"
    )


def test_reencoding_a_photo_barely_moves_its_vector(vectors):
    """WebP q90 is what the server stores, so the vector must survive it.

    Measured across a 47,671-frame sweep, q90 costs ~0.2 accuracy points. If
    this ever drops sharply, the preprocessing changed -- which is exactly the
    failure that leaves every stored threshold quietly mis-calibrated.
    """
    import io

    from PIL import Image

    from app.embed import embed_photo

    name, path = _FIX[0]
    buf = io.BytesIO()
    Image.open(path).convert("RGB").save(buf, "WEBP", quality=90, method=4)
    found = embed_photo(buf.getvalue())
    assert found is not None, "animal lost after a WebP q90 round-trip"

    original = vectors[path.stem][1]
    assert float(original @ found[0]) > 0.95, "WebP q90 moved the vector too far"
