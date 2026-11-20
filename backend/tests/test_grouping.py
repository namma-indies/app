"""Multi-animal grouping with confidence (Akash, 2026-11-19).

A confident grouping publishes on its own; an unsure one is pre-filled and
asked. Everything here is synthetic: boxes and MiewID-like vectors are built
by hand, so no weights, GPU, or footage are needed. What these cannot show is
how real street clips score -- see docs/specs/2026-11-19-multi-animal-grouping.md.
"""
import os
import subprocess
from uuid import UUID, uuid4

import numpy as np
import pytest
from pydantic import ValidationError

from app import video
from app.capture_contracts import MULTI_PIPELINE_VERSION, MultiCompletion, SourceFrame
from app.detect_reid import AnimalDetection, animal_detections
from app.scene import SceneSignals
from app.tracking import (GroupingPolicy, SamplingCoverage, associate_frames, keep_frames_with_animals,
                          make_frame, species_vote)

W = H = 640


def look(animal, view=0, noise=0.3, shared=None):
    """A unit vector: one direction per animal plus per-view noise.

    `shared` blends in a common direction, for look-alikes (cosine ~0.8)."""
    v = np.zeros(2152)
    v[animal] = 1.0
    if shared is not None:
        v[shared] = 2.0
    v[500 + view] = noise
    return v / np.linalg.norm(v)


def box(x, y=200, size=80):
    return (float(x), float(y), float(x + size), float(y + size))


def det(i, species, x, conf=0.8, y=200, size=80, dog=None, cat=None):
    raw = box(x, y, size)
    return AnimalDetection(i, species, conf, raw, tuple(int(v) for v in raw), dog, cat)


def frame(t, animals, photos=False):
    """animals: (species, x, look vector or None, extra detection kwargs)."""
    evidence = []
    for i, (species, x, vec, *rest) in enumerate(animals):
        evidence.append((det(i, species, x, **(rest[0] if rest else {})), vec,
                         None if vec is not None else "embedding_failed"))
    return make_frame(f"{'photo' if photos else 'video'}:{t}", t, None if photos else t * 0.5,
                      W, H, .8, 0, evidence)


def clip(*frames):
    n = len(frames)
    return associate_frames(tuple(frames), SamplingCoverage("video", n, n * 10, n * 0.5, 0, (n - 1) * 0.5,
                                                            48, 3600, True, "nominal_fps"))


def burst(*frames):
    n = len(frames)
    return associate_frames(tuple(frames), SamplingCoverage("photos", n, n, None, None, None, 12, 12, True))


def groups(result):
    return sorted(t.instance_ids for t in result.tracks)


# --- association and confidence -------------------------------------------

def test_two_dogs_crossing_with_different_looks_publish_confidently():
    frames = [frame(t, [("dog", 100 + 60 * t, look(0, t)), ("dog", 400 - 60 * t, look(1, t))]) for t in range(6)]
    result = clip(*frames)
    assert groups(result) == [tuple(f"video:{t}:0" for t in range(6)), tuple(f"video:{t}:1" for t in range(6))]
    assert not result.requires_review
    assert all(t.confident and t.confidence >= 0.65 for t in result.tracks)


def test_two_look_alike_dogs_crossing_are_asked_with_best_guess_filled_in():
    frames = [frame(t, [("dog", 100 + 60 * t, look(0, t, shared=9)), ("dog", 400 - 60 * t, look(1, t, shared=9))])
              for t in range(6)]
    result = clip(*frames)
    assert len(result.tracks) == 2 and result.requires_review
    assert any("close_alternative" in t.reasons for t in result.tracks)


def test_dog_leaving_and_returning_is_still_one_dog():
    a, b = 0, 1
    frames = [frame(0, [("dog", 100, look(a, 0))]), frame(1, [("dog", 110, look(a, 1))]),
              frame(2, [("dog", 500, look(b, 2))]), frame(3, [("dog", 510, look(b, 3))]),
              frame(4, []), frame(5, [("dog", 120, look(a, 5))])]
    result = clip(*frames)
    assert groups(result) == [("video:0:0", "video:1:0", "video:5:0"), ("video:2:0", "video:3:0")]
    assert not result.requires_review


def test_a_cat_mislabelled_as_dog_in_one_frame_stays_one_cat():
    frames = [frame(t, [("dog", 100 + 10 * t, look(0, t), {"conf": .9}),
                        ("dog" if t == 2 else "cat", 400 + 10 * t, look(1, t), {"conf": .5 if t == 2 else .9})])
              for t in range(5)]
    result = clip(*frames)
    dog, cat = sorted(result.tracks, key=lambda t: t.species == "cat")
    assert dog.species == "dog" and cat.species == "cat"
    assert "video:2:1" in cat.instance_ids and len(cat.instance_ids) == 5
    assert not cat.species_uncertain and not result.requires_review


def test_two_boxes_in_one_frame_are_never_one_animal_even_when_identical():
    same = look(0)
    result = clip(frame(0, [("dog", 100, same), ("dog", 300, same)]), frame(1, [("dog", 105, same)]))
    assert all(len({i.split(":")[1] for i in t.instance_ids}) == len(t.instance_ids) for t in result.tracks)
    assert len(result.tracks) == 2


def test_same_frame_competition_for_one_animal_asks():
    # Frame 0: one dog. Frame 1: two equally plausible ones near where it was.
    same = look(0)
    result = clip(frame(0, [("dog", 200, same)]), frame(1, [("dog", 190, same), ("dog", 210, same)]))
    assert result.requires_review
    assert {"close_alternative", "maybe_seen_before"} & {r for t in result.tracks for r in t.reasons}


def test_photo_burst_uses_looks_only_and_separates_different_dogs():
    result = burst(frame(0, [("dog", 100, look(0, 0))], photos=True),
                   frame(1, [("dog", 500, look(1, 1))], photos=True))
    assert len(result.tracks) == 2 and not result.requires_review
    assert all(a.motion_score is None for a in result.associations)


def test_photo_burst_of_one_dog_from_two_angles_is_one_dog():
    result = burst(frame(0, [("dog", 100, look(0, 0))], photos=True),
                   frame(1, [("dog", 500, look(0, 1))], photos=True))
    (track,) = result.tracks
    assert track.confident and len(track.instance_ids) == 2


def test_a_middling_resemblance_is_asked_not_guessed():
    # Cosine ~0.45: neither clearly the same nor clearly another animal.
    a = look(0, 0)
    b = 0.45 * a + np.sqrt(1 - 0.45 ** 2) * look(1, 1, noise=0)
    result = burst(frame(0, [("dog", 100, a)], photos=True), frame(1, [("dog", 100, b / np.linalg.norm(b))],
                                                                     photos=True))
    assert result.requires_review


def test_representative_is_the_best_view_not_the_first():
    frames = [frame(0, [("dog", 100, look(0, 0), {"conf": .5, "size": 40})]),
              frame(1, [("dog", 110, look(0, 1), {"conf": .9, "size": 120})]),
              frame(2, [("dog", 120, look(0, 2), {"conf": .9, "size": 60})])]
    (track,) = clip(*frames).tracks
    assert track.representative_instance_id == "video:1:0"


def test_stricter_policy_turns_the_same_clip_into_a_question():
    frames = [frame(t, [("dog", 100 + 5 * t, look(0, t, noise=0.8))]) for t in range(3)]
    assert not clip(*frames).requires_review
    strict = GroupingPolicy(confident_min=0.99)
    n = len(frames)
    result = associate_frames(tuple(frames), SamplingCoverage("video", n, n, 1.5, 0, 1.0, 48, 3600, True,
                                                              "nominal_fps"), policy=strict)
    assert result.requires_review


# --- species vote ------------------------------------------------------------

def test_species_vote_weighs_confidence_and_per_crop_scores():
    assert species_vote([det(0, "dog", 0, .9), det(1, "dog", 0, .8), det(2, "cat", 0, .3)])[:2] == ("dog", pytest.approx(1.7 / 2.0))
    species, share, uncertain = species_vote([det(0, "dog", 0, .6, dog=.6, cat=.5)])
    assert species == "dog" and uncertain and share == pytest.approx(.6 / 1.1)
    assert species_vote([det(0, "cat", 0, .9, dog=.1, cat=.9)]) == ("cat", pytest.approx(.9), False)


def test_close_species_vote_makes_the_capture_ask():
    frames = [frame(t, [("dog" if t % 2 else "cat", 100, look(0, t), {"conf": .7})]) for t in range(4)]
    result = clip(*frames)
    (track,) = result.tracks
    assert track.species_uncertain and "species_uncertain" in track.reasons and result.requires_review


def test_detector_reports_both_scores_for_a_box_scored_dog_and_cat():
    dets = np.array([[40, 100, 240, 500, .4, 15], [42, 104, 238, 496, .8, 16], [400, 100, 500, 200, .7, 16]])
    first, second = animal_detections(dets, (640, 640))
    assert (first.species, first.dog_score, first.cat_score) == ("dog", pytest.approx(.8), pytest.approx(.4))
    assert (second.dog_score, second.cat_score) == (pytest.approx(.7), 0.0)


# --- budget ---------------------------------------------------------------------

def test_instance_budget_keeps_each_animals_best_views_instead_of_failing():
    frames = [frame(t, [("dog", 50 + 5 * t, look(0, t), {"conf": .5 + t / 100}),
                        ("dog", 300, look(1, t)), ("cat", 500, look(2, t))]) for t in range(40)]
    result = clip(*frames)
    assert sum(len(t.instance_ids) for t in result.tracks) <= 96
    assert len(result.tracks) == 3 and all(len(t.instance_ids) == 32 for t in result.tracks)
    dog = next(t for t in result.tracks if t.instance_ids[0].endswith(":0"))
    # The most confident views of the first dog survive: the later frames.
    assert dog.instance_ids == tuple(f"video:{t}:0" for t in range(8, 40))
    assert len(result.trimmed_instance_ids) == 120 - 96
    stored = [i.instance_id for f in result.frames for i in f.instances]
    assert set(stored) == {i for t in result.tracks for i in t.instance_ids}


def test_budget_still_fails_when_animals_outnumber_it():
    frames = [frame(0, [("dog", 10 * n, look(n)) for n in range(5)])]
    with pytest.raises(ValueError, match="more animals"):
        associate_frames(tuple(frames), SamplingCoverage("video", 1, 1, 1, 0, 0, 48, 3600, True,
                                                         "nominal_fps"), max_instances=4)


# --- frames ------------------------------------------------------------------------

def test_diverse_selection_drops_near_duplicates_and_keeps_time_order():
    still, other = "0" * 16, "f" * 16
    hashes = [still] * 10 + [other] * 10 + ["0f" * 8] * 5
    assert video.select_diverse(hashes, 12) == [0, 10, 20]


def test_diverse_selection_thins_evenly_to_target_keeping_first_and_last():
    import imagehash
    rng = np.random.default_rng(0)
    hashes = [str(imagehash.ImageHash(rng.integers(0, 2, (8, 8)).astype(bool))) for _ in range(48)]
    picked = video.select_diverse(hashes, 12)
    assert len(picked) == 12 and picked == sorted(picked)
    assert picked[0] == 0 and picked[-1] == 47


def test_only_frames_with_animals_are_kept_and_screening_keeps_what_others_saw():
    person = SceneSignals(person=.9, person_area=.4, indoor=0)
    a = make_frame("video:0", 0, 0.0, W, H, 0, 0, [], person)
    b = frame(1, [("dog", 100, look(0))])
    c = make_frame("video:2", 2, 1.0, W, H, 0, 0, [], SceneSignals(indoor=.7))
    result = keep_frames_with_animals(clip(a, b, c))
    assert [f.source_id for f in result.frames] == ["video:1"]
    assert result.frames[0].scene == SceneSignals(.9, .4, .7)
    empty = keep_frames_with_animals(clip(a, c))
    assert [f.source_id for f in empty.frames] == ["video:0"]


# --- policy -------------------------------------------------------------------------

def test_policy_validates_and_ignores_unknown_keys():
    assert GroupingPolicy.from_mapping(None) == GroupingPolicy()
    assert GroupingPolicy.from_mapping({"confident_min": 0.7, "from_the_future": 1}).confident_min == 0.7
    for bad in ({"accept_min": 0.9}, {"cosine_floor": 0.9}, {"species_share_min": 0.4},
                {"motion_speed_scale": 0}, {"margin_min": float("nan")}):
        with pytest.raises(ValueError):
            GroupingPolicy(**bad)


def test_settings_mirror_the_policy_defaults():
    from app.capture_jobs import grouping_policy
    assert grouping_policy() == GroupingPolicy()


# --- wire contract ---------------------------------------------------------------

def completion(tracks=None, **extra):
    source = SourceFrame(photo_id=uuid4(), width=100, height=80, phash="0123456789abcdef",
                         dog_confidence=.9, cat_confidence=.2)
    instances = [dict(index=n, source_photo_id=source.photo_id, track_id=f"t{n}", species="dog",
                      confidence=.9, bbox=(1 + 20 * n, 1, 9 + 20 * n, 9), crop_bbox=(20 * n, 0, 20 * n + 10, 10),
                      width=10, height=10, phash="0123456789abcdef", vector=None, **extra) for n in range(2)]
    return dict(lease_token="x" * 43, pipeline_version=MULTI_PIPELINE_VERSION, model="miewid-msv3",
                frames=[source], instances=instances, needs_review=False,
                **({} if tracks is None else {"tracks": tracks}))


def track(n, **kw):
    return dict(track_id=f"t{n}", species="dog", species_uncertain=False, species_share=1.0,
                confidence=.9, confident=True, representative_index=n, **kw)


def test_old_worker_completion_without_grouping_is_still_accepted():
    body = MultiCompletion.model_validate(completion())
    assert body.tracks is None and body.instances[0].dog_score is None


def test_new_worker_completion_carries_tracks_and_scores():
    body = MultiCompletion.model_validate(completion([track(0), track(1)], dog_score=.9, cat_score=.1))
    assert [t.track_id for t in body.tracks] == ["t0", "t1"]


@pytest.mark.parametrize("tracks", [
    [track(0)],                                   # a track missing
    [track(0), track(1), track(2)],               # a track with no boxes
    [track(0), {**track(1), "representative_index": 0}],  # representative from another animal
    [track(0), {**track(1), "reasons": ["vibes"]}],
    [track(0), {**track(1), "confidence": 1.5}],
    [track(0), {**track(1), "surprise": True}],   # extra fields are forbidden, like the rest
])
def test_inconsistent_tracks_are_rejected(tracks):
    with pytest.raises(ValidationError):
        MultiCompletion.model_validate(completion(tracks))


def test_a_track_may_mix_box_labels_only_when_the_worker_says_what_the_animal_is():
    body = completion()
    body["instances"][1].update(track_id="t0", species="cat", source_photo_id=body["frames"][0].photo_id)
    body["frames"].append(SourceFrame(photo_id=uuid4(), width=100, height=80, phash="0123456789abcdef",
                                      dog_confidence=.9, cat_confidence=.2))
    body["instances"][1]["source_photo_id"] = body["frames"][1].photo_id
    with pytest.raises(ValidationError):
        MultiCompletion.model_validate(body)
    body["tracks"] = [track(0)]
    assert MultiCompletion.model_validate(body).tracks[0].species == "dog"


def test_worker_sends_grouping_only_to_an_api_that_accepts_it(tmp_path):
    from app.photos import process_photo
    from gpu_worker.multi import prepare_capture
    from tests.test_multi_inference import raw, worker

    photo = process_photo(raw())
    job = dict(kind="photo", max_frames=48, max_instances=96, sources=[dict(
        photo_id=str(uuid4()), width=photo.width, height=photo.height, phash=photo.phash)])
    old = prepare_capture(worker(), job, str(tmp_path), [photo.original]).completion({})
    assert "tracks" not in old and all("dog_score" not in i for i in old["instances"])
    MultiCompletion(lease_token="t" * 43, pipeline_version=MULTI_PIPELINE_VERSION, model="miewid-msv3", **old)
    job.update(accepts=["grouping"], grouping_policy=GroupingPolicy(confident_min=0.9).as_mapping())
    new = prepare_capture(worker(), job, str(tmp_path), [photo.original]).completion({})
    body = MultiCompletion(lease_token="t" * 43, pipeline_version=MULTI_PIPELINE_VERSION, model="miewid-msv3", **new)
    assert len(body.tracks) == 2 and body.grouping_policy == "motion-appearance-confidence-v3"
    assert all(i.dog_score is not None for i in body.instances)


# --- API ----------------------------------------------------------------------------

def test_settings_tune_the_policy_and_bad_values_fail(monkeypatch):
    from app.capture_jobs import grouping_policy
    from app.config import settings
    monkeypatch.setattr(settings, "grouping_confident_min", 0.8)
    assert grouping_policy().confident_min == 0.8
    with pytest.raises(ValueError):
        monkeypatch.setattr(settings, "grouping_accept_min", 0.95)
        grouping_policy()


async def test_completion_replay_across_the_deploy_still_matches(migrated_db):
    """A completion from an older worker digests the same with or without the
    new optional fields present as None."""
    from app.capture_jobs import complete_capture_job
    from tests.test_multi_captures import seed, tracked
    conn = migrated_db
    body = tracked(("dog",), new_worker=False)
    oid, cid, jid, storage = await seed(conn, body)
    await complete_capture_job(conn, storage, jid, body)
    explicit = MultiCompletion.model_validate({**body.model_dump(), "tracks": None, "grouping_policy": None})
    assert (await complete_capture_job(conn, storage, jid, explicit))["replayed"]


async def test_review_publishes_the_species_the_person_chose(migrated_db):
    from app.routes.capture import get_capture, review_capture
    from app.capture_contracts import AnimalGroup, CaptureReview
    from tests.test_multi_captures import seed, tracked
    conn = migrated_db
    body = tracked(("dog", "dog"), ("dog",), same_look=True)
    oid, cid, jid, storage = await seed(conn, body)
    await complete_capture_job_(conn, storage, jid, body)
    status = await get_capture(cid, oid, conn, storage)
    a, b = status.groups
    merged = sorted(a.instance_ids + b.instance_ids, key=str)
    # Merging boxes from one frame is refused, as before.
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        await review_capture(cid, CaptureReview(revision=status.revision, groups=[
            AnimalGroup(instance_ids=merged)]), oid, conn, storage)
    groups = [AnimalGroup(instance_ids=a.instance_ids, species="cat"), AnimalGroup(instance_ids=b.instance_ids)]
    published = await review_capture(cid, CaptureReview(revision=status.revision, groups=groups), oid, conn, storage)
    assert published.processing_state == "ready"
    rows = await conn.fetch("SELECT species,cover_photo_id FROM sightings WHERE capture_id=$1 ORDER BY species", cid)
    assert [r["species"] for r in rows] == ["cat", "dog"]
    assert all(r["cover_photo_id"] for r in rows)
    assert [g.species for g in published.groups] == ["cat", "dog"]


async def complete_capture_job_(conn, storage, jid, body):
    from app.capture_jobs import complete_capture_job
    return await complete_capture_job(conn, storage, jid, body)


async def test_recheck_publishes_a_held_confident_grouping_but_not_an_unsure_one(migrated_db):
    from app.captures import recheck_held
    from app.matching import lock_matching
    from tests.test_multi_captures import seed, tracked
    conn = migrated_db
    for same_look, published in ((False, 2), (True, 0)):
        body = tracked(("dog", "dog"), ("dog",), same_look=same_look).model_copy(update={"needs_review": True})
        oid, cid, jid, storage = await seed(conn, body)
        assert (await complete_capture_job_(conn, storage, jid, body))["outcome"] == "needs_review"
        async with conn.transaction():
            await lock_matching(conn)
            capture = await conn.fetchrow("SELECT * FROM captures WHERE id=$1 FOR UPDATE", cid)
            await recheck_held(conn, capture)
        assert await conn.fetchval("SELECT count(*) FROM sightings WHERE capture_id=$1", cid) == published


async def test_status_lists_boxes_in_time_order_not_track_id_text(migrated_db):
    # Track ids sort as text (video:1005 before video:200); time must win.
    from app.capture_contracts import AnimalResult, MultiURLRequest
    from app.capture_jobs import upload_urls
    from app.routes.capture import get_capture
    from tests.test_multi_captures import seed
    conn = migrated_db
    frames = [SourceFrame(photo_id=uuid4(), index=i, timestamp_ms=t, width=100, height=80,
                          phash="0123456789abcdef", dog_confidence=.9, cat_confidence=0)
              for i, t in enumerate((200, 1005, 30000))]
    instances = [dict(index=i, source_photo_id=f.photo_id, track_id=f"track:video:{t}:0", species="dog",
                      confidence=.9, bbox=(1, 1, 9, 9), crop_bbox=(0, 0, 10, 10), width=10, height=10,
                      phash="0123456789abcdef", vector=None)
                 for i, (f, t) in enumerate(zip(frames, (200, 1005, 30000)))]
    body = MultiCompletion(lease_token="x" * 43, pipeline_version=MULTI_PIPELINE_VERSION, model="miewid-msv3",
                           frames=frames, instances=instances, needs_review=True)
    oid, cid, jid, storage = await seed(conn, body, kind="video")
    reply = await upload_urls(conn, storage, jid, MultiURLRequest(lease_token=body.lease_token, uploads=[
        {"kind": "frame", "index": f.index, "original_bytes": 50, "thumbnail_bytes": 10} for f in frames]))
    ids = {u["index"]: UUID(u["photo_id"]) for u in reply["uploads"]}
    frames = [f.model_copy(update={"photo_id": ids[f.index]}) for f in frames]
    body = body.model_copy(update={"frames": frames, "instances": [
        AnimalResult(**{**i, "source_photo_id": f.photo_id}) for i, f in zip(instances, frames)]})
    await complete_capture_job_(conn, storage, jid, body)
    status = await get_capture(cid, oid, conn, storage)
    assert [i.timestamp_ms for i in status.instances] == [200, 1005, 30000]
    assert [g.instance_ids[0] for g in status.groups] == [i.instance_id for i in status.instances]


# --- migration ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_0049_up_and_down(scratch_db):
    import asyncpg
    from tests.conftest import BACKEND_ROOT
    url, upgrade = scratch_db
    upgrade("0049_multi_animal_grouping")
    conn = await asyncpg.connect(url)
    try:
        assert await conn.fetchval("SELECT to_regclass('capture_tracks') IS NOT NULL")
        assert await conn.fetchval("""SELECT count(*) FROM information_schema.columns
            WHERE table_name='sightings' AND column_name='cover_photo_id'""") == 1
    finally:
        await conn.close()
    env = os.environ.copy()
    env["ALEMBIC_DB_URL"] = url.replace("postgresql://", "postgresql+psycopg://")
    subprocess.run(["uv", "run", "alembic", "downgrade", "0048_moderation_defaults"], cwd=BACKEND_ROOT,
                   env=env, check=True, capture_output=True)
    conn = await asyncpg.connect(url)
    try:
        assert await conn.fetchval("SELECT to_regclass('capture_tracks') IS NULL")
        assert await conn.fetchval("""SELECT count(*) FROM information_schema.columns
            WHERE table_name='sightings' AND column_name='cover_photo_id'""") == 0
    finally:
        await conn.close()


from tests.test_launch_migrations import scratch_db  # noqa: E402,F401  (fixture)
