import numpy as np
import pytest
from PIL import Image

from app.embed import EMBED_DIM, MODEL_NAME, ModelUnavailable, embed_crop, preprocess
from app import embed as embed_mod


def _crop(size=(316, 315)) -> Image.Image:
    """Non-symmetric, non-square content: a flat fill would make the
    aspect-ratio assertions below pass for the wrong reason."""
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 255 // w, y * 255 // h, (x + y) % 256)
    return img


def test_preprocess_shape_and_normalisation():
    out = preprocess(_crop())
    assert out.shape == (1, 3, 440, 440)
    assert out.dtype == np.float32
    # ImageNet-normalised pixels land roughly in [-2.2, 2.7]; raw 0-1 or 0-255
    # input would sit far outside that and is the failure this catches.
    assert -3.0 < float(out.min()) and float(out.max()) < 3.5


def test_preprocess_preserves_aspect_ratio():
    """Resize must scale the SHORTER side to 440 and centre-crop, not squash
    both sides to 440. Squashing distorts the subject and shifts every
    embedding away from the distribution the thresholds were measured on."""
    tall = _crop((300, 900))
    wide = _crop((900, 300))
    # A vertical strip of the tall image and a horizontal strip of the wide one
    # describe the same geometry after correct handling: both keep their
    # shorter side at 440, so neither is distorted.
    for crop in (tall, wide):
        out = preprocess(crop)
        assert out.shape == (1, 3, 440, 440)

    # Squashing would make these two images' outputs mirror-related; correct
    # centre-cropping keeps them independent.
    assert not np.allclose(preprocess(tall)[0], preprocess(wide)[0].transpose(0, 2, 1))


def test_preprocess_upsamples_small_crops():
    """A distant dog yields a crop smaller than 440. It must be upsampled, not
    padded or rejected -- 70% of our benchmark crops were under 440px."""
    out = preprocess(_crop((120, 90)))
    assert out.shape == (1, 3, 440, 440)


def test_model_name_and_dim_match_the_schema():
    """These two constants are the contract with migration 0005: the column is
    vector(2152) and the CHECK constraint keys off this exact model string."""
    assert MODEL_NAME == "miewid-msv3"
    assert EMBED_DIM == 2152


@pytest.mark.skipif(
    not embed_mod._MODEL_PATH.exists(),
    reason="MiewID weights absent (206 MB, gitignored; see scripts/export_miewid_onnx.py)",
)
def test_embed_crop_is_unit_norm_and_correct_dim():
    vec = embed_crop(_crop())
    assert vec.shape == (EMBED_DIM,)
    assert vec.dtype == np.float32
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-5


@pytest.mark.skipif(
    not embed_mod._MODEL_PATH.exists(), reason="MiewID weights absent"
)
def test_same_image_embeds_identically_and_differs_from_another():
    """Sanity floor for re-ID: an image matches itself exactly, and an
    unrelated image does not. If these ever converge, matching is broken in a
    way that still returns plausible-looking numbers."""
    a, b = _crop((316, 315)), _crop((400, 260))
    va1, va2, vb = embed_crop(a), embed_crop(a), embed_crop(b)
    assert float(va1 @ va2) > 0.9999
    assert float(va1 @ vb) < 0.999


def test_missing_model_raises_a_named_error(monkeypatch, tmp_path):
    """Absent weights must fail loudly with a pointer to the fix, not with a
    bare onnxruntime error two layers down."""
    monkeypatch.setattr(embed_mod, "_session", None)
    monkeypatch.setattr(embed_mod, "_MODEL_PATH", tmp_path / "nope.onnx")
    with pytest.raises(ModelUnavailable, match="export_miewid_onnx"):
        embed_mod._get_session()
