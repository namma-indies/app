"""One detection pass must say exactly what two passes said.

`analyse()` replaces `animal_confidence()` + `best_animal_box()` with a single
forward pass. That is a performance change and must not be a behaviour change:
the box decides what gets cropped and embedded, and the confidence is stored on
the sighting as a review signal. If either moves, every downstream number moves
with it -- including the calibrated thresholds in config.py.

So these compare the new path against the old one on real photographs and
require equality, not closeness.
"""

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

FIXTURES = Path(__file__).parent / "fixtures" / "dogs"
PHOTOS = sorted(FIXTURES.glob("*.jpg"))

pytestmark = pytest.mark.skipif(
    not PHOTOS, reason="no dog fixtures; see tests/fixtures/dogs/README.md"
)


def _models_present() -> bool:
    from app.detect_reid import _MODEL_PATH as YOLO
    from app.embed import _MODEL_PATH as MIEW

    return YOLO.exists() and MIEW.exists()


needs_models = pytest.mark.skipif(
    not _models_present(), reason="ONNX weights absent; run scripts/export_*_onnx.py"
)


@needs_models
@pytest.mark.parametrize("path", PHOTOS, ids=lambda p: p.stem)
def test_confidences_match_the_old_two_pass_path(path):
    from app.analyse import analyse
    from app.detect_reid import animal_confidence

    raw = path.read_bytes()
    old_dog, old_cat = animal_confidence(raw)
    got = analyse(raw)

    assert got.dog_confidence == old_dog
    assert got.cat_confidence == old_cat


@needs_models
@pytest.mark.parametrize("path", PHOTOS, ids=lambda p: p.stem)
def test_box_matches_the_old_two_pass_path(path):
    from app.analyse import analyse
    from app.detect_reid import best_animal_box

    raw = path.read_bytes()
    old = best_animal_box(raw)
    got = analyse(raw)

    if old is None:
        assert got.box is None
    else:
        # Exact: the crop is these pixels, and a one-pixel shift changes the
        # embedding it produces.
        assert got.box == old[0]


@needs_models
@pytest.mark.parametrize("path", PHOTOS, ids=lambda p: p.stem)
def test_embedding_is_bit_identical_to_embed_photo(path):
    """The vectors land in the same pgvector column as every existing row, so
    'very close' is not good enough -- the corpus was built with the old path
    and the thresholds are fitted to its distribution."""
    from app.analyse import analyse, embed_analysis
    from app.embed import embed_photo

    raw = path.read_bytes()
    old = embed_photo(raw)
    got = embed_analysis(analyse(raw))

    if old is None:
        assert got is None
        return
    # Array equality is the real check: same bytes, not merely same direction.
    np.testing.assert_array_equal(got, old[0])
    # The dot product is a readable restatement of it, and 1e-6 is the honest
    # tolerance -- summing 2152 float32 squares of a unit vector accumulates to
    # about 1.0000001, so a tighter bound tests float32 rather than the code.
    assert float(np.dot(got, old[0])) == pytest.approx(1.0, abs=1e-6)


@needs_models
def test_one_forward_pass_not_two():
    """The whole point. Counts calls into the ONNX session rather than timing,
    so it cannot go green on a fast machine."""
    from app import analyse as analyse_mod
    from app.detect_reid import _get_session

    sess = _get_session()
    calls = {"n": 0}
    real_run = sess.run

    def counting_run(*a, **kw):
        calls["n"] += 1
        return real_run(*a, **kw)

    sess.run = counting_run  # type: ignore[method-assign]
    try:
        analyse_mod.analyse(PHOTOS[0].read_bytes())
    finally:
        sess.run = real_run  # type: ignore[method-assign]

    assert calls["n"] == 1, f"expected one detection pass, made {calls['n']}"


@needs_models
def test_decodes_the_image_once_and_hands_it_back():
    """`Analysis.image` exists so the caller can crop without a second JPEG
    decode. embed_photo used to decode twice: once itself, once inside
    best_animal_box."""
    from app.analyse import analyse

    got = analyse(PHOTOS[0].read_bytes())
    assert isinstance(got.image, Image.Image)
    assert got.image.mode == "RGB"


@needs_models
def test_no_animal_yields_no_box_but_still_a_confidence():
    """A flat frame has no animal. The confidence is still a number -- callers
    store it as a label, and 'scored, saw nothing' must stay distinguishable
    from 'never scored', which is NULL."""
    from app.analyse import analyse, embed_analysis

    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (30, 120, 60)).save(buf, "JPEG")
    got = analyse(buf.getvalue())

    assert got.box is None
    assert got.has_animal is False
    assert isinstance(got.dog_confidence, float)
    assert embed_analysis(got) is None


# --- the capture path uses it once per photo ---------------------------------


@needs_models
@pytest.mark.asyncio
async def test_one_upload_makes_one_detection_pass_per_photo(authed_client):
    """The end the refactor exists for. Two background tasks meant two yolo26x
    passes per photo; a 12-frame clip was spending ~10 s of CPU on a 2-core box
    re-detecting animals it had already found."""
    from app.detect_reid import _get_session

    sess = _get_session()
    calls = {"n": 0}
    real_run = sess.run

    def counting_run(*a, **kw):
        calls["n"] += 1
        return real_run(*a, **kw)

    client, _ = authed_client
    sess.run = counting_run  # type: ignore[method-assign]
    try:
        r = await client.post(
            "/sighting",
            files={"photos": ("d.jpg", PHOTOS[0].read_bytes(), "image/jpeg")},
            data={"geo_source": "none", "captured_at": "2026-09-08T10:00:00Z"},
        )
        assert r.status_code == 201
    finally:
        sess.run = real_run  # type: ignore[method-assign]

    # Exactly one, for one photo. It was two.
    assert calls["n"] == 1, f"one photo caused {calls['n']} detection passes"
