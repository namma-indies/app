# Re-ID test fixtures

Photographs of real dogs with known identities, used by `test_reid_fixtures.py`
to prove the pipeline can actually tell two animals apart. Empty by default —
the test skips until you populate it.

Naming carries the ground truth:

    rex_0.jpg    rex_1.jpg      two photos of one dog
    kali_0.jpg   kali_1.jpg     two photos of a different dog

Two identities is the minimum. Photos taken on **different days** are worth far
more than two frames of the same moment — two frames a second apart mostly test
that the encoder is deterministic, which is not the interesting question.

Street sightings captured through the app are the best source: they are the
distribution this runs on. Images from a controlled setting (fixed camera,
constant lighting, one room) will pass more easily than reality does, so a green
test on those means less than it appears to.

Whatever goes here is committed, so only add photos you are willing to publish.
