"""Invariants on the matching thresholds.

Not a test of what the right number is -- that is a judgement fitted to real
verdicts. These pin the properties that made 0.71 useless in the first place,
so the same shape of mistake fails loudly rather than showing up as a review
queue nobody can explain the emptiness of.
"""

from app.config import settings

# The best cosine ever observed between two sightings of the SAME dog, from the
# Dognosis v2 clips. See the long note in config.py.
BEST_OBSERVED_TRUE_MATCH = 0.5532
# Two different dogs that resemble each other, photographed seven days apart.
LOOKALIKE_CEILING = 0.7122


def test_propose_is_reachable_by_a_real_match():
    """The failure that made re-ID inert. At 0.71 the threshold sat *above* the
    best score any genuine pair had ever achieved, so no true match could reach
    it -- the system was not being conservative, it was switched off, and the
    review queue was empty by construction rather than by luck.
    """
    assert settings.reid_propose_min < BEST_OBSERVED_TRUE_MATCH, (
        f"propose_min {settings.reid_propose_min} is above the best genuine "
        f"match ever measured ({BEST_OBSERVED_TRUE_MATCH}); no true pair can "
        "reach it, so nothing will ever be proposed"
    )


def test_proposing_is_easier_than_auto_merging():
    """Auto-merge must be the stricter gate. Inverted, every candidate that
    engaged a human would already have been merged without one."""
    assert settings.reid_propose_min < settings.reid_auto_merge_min


def test_auto_merge_stays_disabled_until_it_is_calibrated():
    """auto_merge_min > 1.0 is unreachable by cosine, which is how automatic
    merging is held off. Nothing should link two dogs without a human until the
    thresholds are fitted to real verdicts -- a wrong merge fuses two animals
    into one record and has no undo in the app."""
    assert settings.reid_auto_merge_min > 1.0


def test_the_threshold_is_not_pure_noise():
    """Below about 0.3 the ranking stops separating anything: at 0.30 the
    measured precision was 19.2%, four in five prompts wrong. A queue that
    dishonest trains people to dismiss it without looking."""
    assert settings.reid_propose_min >= 0.35
