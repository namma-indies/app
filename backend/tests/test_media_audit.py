"""Synthetic unit tests only: no database, object storage, or model weights."""

import io
import json
from pathlib import Path
import stat
from types import SimpleNamespace
import uuid

from PIL import Image
import pytest

from scripts import audit_media as audit


@pytest.fixture
def output(tmp_path):
    # macOS /var is itself a symlink; the CLI deliberately requires its real path.
    private = audit.PrivateOutput(tmp_path.resolve() / "audit")
    yield private
    private.close()


def photo(pid="p1", sid="s1"):
    return {"id": pid, "sighting_id": sid, "s3_key": f"{pid}.webp", "phash": "aabb",
            "embedding_digests": [{"model": "m1", "column": "vec_miew", "dim": 2,
                                    "text_sha256": "digest"}]}


def inventory():
    return {"photos": [photo(), photo("p2")], "sightings": [
        {"id": "s1", "photo_ids": ["p1", "p2"], "clip_s3_key": "clip.mp4",
         "dependencies": {}, "individual_id": None}]}


def image_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (16, 12), "red").save(buffer, format="PNG")
    return buffer.getvalue()


class Body:
    def __init__(self, raw):
        self.raw = raw
        self.requests = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def read(self, size):
        assert 0 < size <= 64 * 1024
        self.requests.append(size)
        result, self.raw = self.raw[:size], self.raw[size:]
        return result


class Client:
    def __init__(self, raw=None, declared=True):
        self.raw = raw if raw is not None else image_bytes()
        self.declared = declared
        self.etag, self.version = '"v1"', "1"
        self.calls = []

    async def get_object(self, **kwargs):
        self.calls.append(kwargs)
        self.body = Body(self.raw)
        result = {"Body": self.body, "ETag": self.etag, "VersionId": self.version}
        if self.declared:
            result["ContentLength"] = len(self.raw)
        return result


class Detector:
    def __init__(self, animal=False, fail=False):
        self.calls, self.animal, self.fail = 0, animal, fail

    def decode(self, raw):
        return Image.open(io.BytesIO(raw)).convert("RGB")

    def detect(self, raw, image):
        self.calls += 1
        if self.fail:
            raise RuntimeError("sensitive server details")
        return SimpleNamespace(has_animal=self.animal, dog_confidence=0.03,
                               cat_confidence=0.01, box=None)


def args(**updates):
    return SimpleNamespace(**({"max_photos": None, "max_bytes": 1024 * 1024,
                              "max_pixels": 10000, "delay": 0} | updates))


def controls(output):
    return audit.Controls(output.path / "PAUSE", output.path / "STOP")


def test_private_output_modes_and_atomic_checkpoint(output):
    output.write("checkpoint.json", b'{"first":true}')
    output.write("checkpoint.json", b'{"second":true}')
    assert output.read_json("checkpoint.json") == {"second": True}
    assert stat.S_IMODE(output.path.stat().st_mode) == 0o700
    assert stat.S_IMODE((output.path / "checkpoint.json").stat().st_mode) == 0o600
    assert not list(output.path.glob(".tmp-*"))


def test_output_refuses_repo_and_relative_paths(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        audit.PrivateOutput(Path("relative"))
    repo = tmp_path.resolve()
    with pytest.raises(ValueError, match="outside"):
        audit.PrivateOutput(repo / "inside", repo=repo)


def test_output_refuses_symlink_ancestors_and_targets(output, tmp_path):
    link = tmp_path.resolve() / "link"
    link.symlink_to(output.path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        audit.PrivateOutput(link / "child")
    secret = tmp_path / "secret"
    secret.write_text("untouched")
    (output.path / "audit.json").symlink_to(secret)
    with pytest.raises(ValueError, match="private regular"):
        output.write("audit.json", b"bad")
    assert secret.read_text() == "untouched"


def test_refuses_shared_directory_and_concurrent_owner(output, tmp_path):
    shared = tmp_path.resolve() / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="0700"):
        audit.PrivateOutput(shared)
    with pytest.raises(BlockingIOError):
        audit.PrivateOutput(output.path)


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False])
async def test_stream_cap_closes_body(declared):
    client = Client(b"x" * 100, declared=declared)
    with pytest.raises(ValueError, match="byte limit"):
        await audit.fetch_photo(client, "bucket", "key", 16)
    assert client.body.closed
    assert sum(client.body.requests) <= 17
    assert client.calls == [{"Bucket": "bucket", "Key": "key"}]


@pytest.mark.asyncio
async def test_stream_evidence_and_truncation():
    client = Client(b"hello")
    raw, source = await audit.fetch_photo(client, "bucket", "key", 5)
    assert raw == b"hello"
    assert source["byte_length"] == 5
    assert source["sha256"] == audit.hashlib.sha256(b"hello").hexdigest()
    assert source["etag"] == '"v1"' and source["version_id"] == "1"

    class Truncated(Client):
        async def get_object(self, **kwargs):
            result = await super().get_object(**kwargs)
            result["ContentLength"] = 6
            return result

    with pytest.raises(ValueError, match="incomplete"):
        await audit.fetch_photo(Truncated(b"hello"), "bucket", "key", 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["fetch", "decode", "detection"])
async def test_failures_are_not_no_animal(output, stage):
    client = Client(b"invalid" if stage == "decode" else None)
    detector = Detector(fail=stage == "detection")
    limit = 1 if stage == "fetch" else 1024 * 1024
    result = await audit.inspect_photo(photo(), client, "b", detector, output, limit, 10000)
    assert result["status"] == stage + "_error"
    assert "sensitive" not in json.dumps(result)


@pytest.mark.asyncio
async def test_pixel_limit_and_private_thumbnail(output):
    client = Client()
    result = await audit.inspect_photo(photo(), client, "b", Detector(), output, 10000, 1)
    assert result["status"] == "decode_error"
    result = await audit.inspect_photo(photo(), client, "b", Detector(), output, 10000, 10000)
    assert result["status"] == "no_animal_candidate"
    path = output.path / result["thumbnail"]
    with Image.open(path) as image:
        assert not image.getexif()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_resume_revalidates_bytes_metadata_and_retries_errors(output):
    client, detector = Client(), Detector()
    first = await audit.inspect_photo(photo(), client, "b", detector, output, 10000, 10000)
    second = await audit.inspect_photo(photo(), client, "b", detector, output, 10000, 10000, first)
    assert first == second and detector.calls == 1 and len(client.calls) == 2
    client.etag = '"v2"'
    third = await audit.inspect_photo(photo(), client, "b", detector, output, 10000, 10000, second)
    assert detector.calls == 2 and third["source"] != second["source"]
    # Same ETag is not accepted as proof: hash the streamed bytes again.
    client.raw = image_bytes() + b"trailing metadata"
    fourth = await audit.inspect_photo(photo(), client, "b", detector, output, 10000, 10000, third)
    assert detector.calls == 3 and fourth["source"]["sha256"] != third["source"]["sha256"]
    failed = {"status": "detection_error", "source": fourth["source"]}
    await audit.inspect_photo(photo(), client, "b", detector, output, 10000, 10000, failed)
    assert detector.calls == 4


def test_resume_rejects_manifest_or_inventory_changes(output):
    inv = inventory()
    manifest = {"model_sha256": "abc", "threshold": 0.1, "inventory_sha256": audit.digest(inv)}
    state = audit.start_state(output, manifest, inv, False)
    audit.save(output, state)
    with pytest.raises(ValueError, match="checkpoint exists"):
        audit.start_state(output, manifest, inv, False)
    for changed in ({**manifest, "threshold": 0.2}, {**manifest, "model_sha256": "def"}):
        with pytest.raises(ValueError, match="changed"):
            audit.start_state(output, changed, inv, True)
    with pytest.raises(ValueError, match="changed"):
        audit.start_state(output, manifest, {**inv, "photos": []}, True)


@pytest.mark.asyncio
async def test_smoke_batches_prioritise_remaining_and_never_claim_full_validation(output):
    inv = inventory()
    state = audit.start_state(output, {}, inv, False)
    first = await audit.process(state, output, Client(), "b", Detector(), controls(output), args(max_photos=1))
    assert first["status_counts"] == {"no_animal_candidate": 1}
    assert not first["full_current_pass"] and not first["no_animal_sighting_candidates"]
    resumed = audit.start_state(output, {}, inv, True)
    second = await audit.process(resumed, output, Client(), "b", Detector(), controls(output), args(max_photos=1))
    assert second["status_counts"] == {"no_animal_candidate": 2}
    assert second["photos_revalidated_this_run"] == 1
    assert not second["full_current_pass"]
    final = await audit.process(audit.start_state(output, {}, inv, True), output, Client(),
                                "b", Detector(), controls(output), args())
    assert final["full_current_pass"] and final["no_animal_sighting_candidates"] == ["s1"]
    assert final["deletion_approved"] is False


@pytest.mark.asyncio
async def test_stop_and_interrupt_do_not_fetch(output):
    for mode in ("file", "signal"):
        ctl = controls(output)
        if mode == "file":
            output.write("STOP", b"")
        else:
            ctl.interrupted = True
        client = Client()
        state = {"inventory": inventory(), "results": {}, "run_number": 1}
        await audit.process(state, output, client, "b", Detector(), ctl, args())
        assert not client.calls and state["stopped"]
        if mode == "file":
            (output.path / "STOP").unlink()


@pytest.mark.asyncio
async def test_pause_waits_until_removed(output, monkeypatch):
    output.write("PAUSE", b"")
    waited = []

    async def release(seconds):
        waited.append(seconds)
        (output.path / "PAUSE").unlink()

    monkeypatch.setattr(audit.asyncio, "sleep", release)
    assert await controls(output).ready()
    assert waited == [0.25]


def test_groups_are_scoped_and_candidates_only():
    inv = inventory()
    source = {"sha256": "same", "byte_length": 100}
    results = {pid: {"source": source, "revalidated_this_run": True} for pid in ("p1", "p2")}
    groups = audit.evidence_groups(inv, results)
    assert len(groups["exact_bytes"]) == 1
    assert len(groups["size_vector_candidate"]) == 1
    assert groups["size_vector_candidate"][0]["classification"] == "review_candidate"
    inv["photos"][1]["embedding_digests"][0]["model"] = "other"
    assert not audit.evidence_groups(inv, results)["size_vector_candidate"]
    results["p2"]["revalidated_this_run"] = False
    assert not any(audit.evidence_groups(inv, results).values())


def test_contact_sheet_escapes_metadata_and_keeps_all_siblings():
    inv = inventory()
    inv["sightings"][0]["clip_s3_key"] = "<script>alert('x')</script>"
    state = {"inventory": inv, "results": {"p1": {"status": "no_animal_candidate",
             "revalidated_this_run": True}, "p2": {"status": "detection_error"}}}
    state["summary"] = audit.report(state)
    page = audit.contact_sheet(state).decode()
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "default-src 'none'" in page
    assert "p1" in page and "p2" in page
    assert not state["summary"]["no_animal_sighting_candidates"]


@pytest.mark.asyncio
async def test_snapshot_readonly_dependencies_vector_digest_and_no_private_fields():
    s1, s2, i1, p1, proposal_id = (uuid.uuid4() for _ in range(5))

    class Transaction:
        async def __aenter__(self):
            pass

        async def __aexit__(self, *args):
            pass

    class Connection:
        def transaction(self, **kwargs):
            assert kwargs == {"isolation": "repeatable_read", "readonly": True}
            return Transaction()

        async def fetch(self, query):
            assert query.strip().startswith("SELECT")
            assert "geog" not in query and "email" not in query
            if "FROM sightings" in query:
                return [{"id": s1, "individual_id": i1}, {"id": s2, "individual_id": None}]
            if "FROM photos" in query:
                return [{"id": p1, "sighting_id": s1}]
            if "FROM match_proposals" in query:
                return [{"id": proposal_id, "sighting_id": s1, "candidate_sighting_id": s2,
                         "candidate_individual_id": i1, "score": 0.4, "status": "confirmed"}]
            if "FROM confirmations" in query:
                return [{"sighting_id": s1, "individual_id": i1, "proposal_id": proposal_id, "count": 1}]
            if "FROM clinical_records" in query:
                return [{"sighting_id": s2, "individual_id": i1, "count": 2}]
            raise AssertionError(query)

        async def cursor(self, query, **kwargs):
            assert "vec_miew::text" in query
            yield {"photo_id": p1, "model": "m1", "dim": 2,
                   "legacy_text": None, "miew_text": "[0.123,0.456]"}

    inv = await audit.snapshot(Connection())
    serialized = json.dumps(inv)
    assert "[0.123,0.456]" not in serialized
    assert inv["photos"][0]["embedding_digests"][0]["text_sha256"] == audit.digest(
        ["m1", "vec_miew", "[0.123,0.456]"])
    first, second = inv["sightings"]
    assert first["dependencies"]["proposals_outgoing"] == 1
    assert second["dependencies"]["proposals_incoming"] == 1
    assert second["dependencies"]["confirmations_via_proposals"] == 1
    assert first["dependencies"]["clinical_to_individual"] == 2
    assert second["dependencies"]["clinical_direct"] == 2
    assert first["photo_ids"] == [str(p1)]


def test_isolated_detector_reuses_analyse_without_mutating_server(tmp_path, monkeypatch):
    import numpy as np
    import onnxruntime as ort
    from app import analyse

    model = tmp_path.resolve() / "fake.onnx"
    model.write_bytes(b"synthetic")
    original = analyse._get_session
    captured = {}

    class Session:
        def __init__(self, weights, sess_options, providers):
            captured.update(weights=weights, options=sess_options, providers=providers)

        def get_inputs(self):
            return [SimpleNamespace(name="input")]

        def run(self, *args):
            return [np.array([[[0, 0, 100, 100, .5, 16]]], dtype=np.float32)]

    monkeypatch.setattr(ort, "InferenceSession", Session)
    detector = audit.AuditDetector(model, .6, 1, audit.file_sha(model))
    result = detector.detect(image_bytes(), Image.new("RGB", (10, 10)))
    assert not result.has_animal
    assert analyse._get_session is original
    assert analyse.REID_CONF_THRESHOLD == .1
    assert captured["options"].intra_op_num_threads == 1
    assert captured["options"].inter_op_num_threads == 1
    assert captured["providers"] == ["CPUExecutionProvider"]
