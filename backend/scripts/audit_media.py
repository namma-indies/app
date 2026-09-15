"""Read-only, private media review evidence; never a deletion authorisation.

Run from backend (no services are contacted by --help):
    uv run python scripts/audit_media.py --output /private/path/media-audit --max-photos 5 --delay 1
Repeat with --resume and the same configuration. Omit --max-photos for a full
pass. Every attempted photo counts against the limit, including cached photos
whose bytes are revalidated. New/failed photos are prioritised. A partial pass
is explicitly NOT a currently validated full audit. Remove PAUSE to continue;
create STOP or send SIGINT/SIGTERM to checkpoint after the current photo.
Create control files with mode 0600 (for example, use `umask 077` before touch).
Exit codes: 0 = fully revalidated successful pass; 2 = partial/stopped/errors;
1 = setup/checkpoint failure. A failed photo is never a no-animal candidate.

Use SELECT-only database credentials and GetObject-only object credentials
where available. Configuration is read from the existing app environment; no
configuration, database, bucket, original, or live model setting is modified.
Output includes sensitive media and identifiers: keep the entire directory
private, outside this repository. No source originals or vectors are exported.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import fcntl
import html
import io
import json
import math
import os
from pathlib import Path
import signal
import stat
import sys
import types
import uuid
import warnings

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

FORMAT_VERSION = 1
SUCCESS = {"animal_detected", "no_animal_candidate"}


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha(path: Path) -> str:
    refuse_symlinks(path)
    h = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def refuse_symlinks(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("symlinks are not permitted")


class PrivateOutput:
    """Private directory with no-follow, dirfd-relative atomic file access."""

    def __init__(self, path: Path, repo: Path = REPO):
        if not path.is_absolute():
            raise ValueError("--output must be absolute")
        refuse_symlinks(path)
        path = path.resolve()
        if path == repo.resolve() or repo.resolve() in path.parents:
            raise ValueError("--output must be outside the repository")
        # Parent creation is intentionally not recursive: do not change an
        # operator's directory hierarchy or silently follow an unsafe ancestor.
        if not path.parent.is_dir():
            raise ValueError("output parent must already exist")
        path.mkdir(mode=0o700, exist_ok=True)
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            self.close()
            raise ValueError("output directory must be owned by you with mode 0700")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for name in os.listdir(self.fd):
                self.check(name)
        except BaseException:
            self.close()
            raise

    def close(self):
        os.close(self.fd)

    def check(self, name):
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("invalid output filename")
        try:
            info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("output files must be private regular files (0600), not links")
        return True

    def read_json(self, name):
        if not self.check(name):
            return None
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as source:
            return json.load(source)

    def write(self, name, data: bytes):
        self.check(name)
        temporary = f".tmp-{uuid.uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            self.check(name)
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass


def normalise(row):
    return {key: (value.isoformat() if isinstance(value, datetime)
                  else str(value) if isinstance(value, uuid.UUID) else value)
            for key, value in dict(row).items()}


async def snapshot(conn):
    """One repeatable snapshot; vector text exists only transiently for hashing."""
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        sightings = [normalise(r) for r in await conn.fetch("""
            SELECT id, individual_id, captured_at, created_at, updated_at,
                   review_status, match_status, clip_s3_key, dog_confidence
            FROM sightings ORDER BY id
        """)]
        photos = [normalise(r) for r in await conn.fetch("""
            SELECT id, sighting_id, s3_key, width, height, phash, created_at
            FROM photos ORDER BY id
        """)]
        by_photo = {p["id"]: p for p in photos}
        for photo in photos:
            photo["embedding_digests"] = []
            photo["embedding_rows"] = []
        async for row in conn.cursor("""
            SELECT photo_id, model, dim, vec::text AS legacy_text,
                   vec_miew::text AS miew_text
            FROM embeddings ORDER BY photo_id, model
        """, prefetch=32):
            by_photo[str(row["photo_id"])]["embedding_rows"].append({
                "model": row["model"], "dim": row["dim"],
                "has_legacy_vector": row["legacy_text"] is not None,
                "has_miew_vector": row["miew_text"] is not None})
            for column, field in (("vec", "legacy_text"), ("vec_miew", "miew_text")):
                if row[field] is not None:
                    by_photo[str(row["photo_id"])]["embedding_digests"].append({
                        "model": row["model"], "column": column, "dim": row["dim"],
                        "text_sha256": digest([row["model"], column, row[field]]),
                    })
        proposals = [normalise(r) for r in await conn.fetch("""
            SELECT id, sighting_id, candidate_sighting_id, candidate_individual_id,
                   status, score FROM match_proposals ORDER BY id
        """)]
        confirmations = [normalise(r) for r in await conn.fetch("""
            SELECT sighting_id, individual_id, proposal_id, count(*) AS count
            FROM confirmations GROUP BY sighting_id, individual_id, proposal_id
            ORDER BY sighting_id, individual_id, proposal_id
        """)]
        clinical = [normalise(r) for r in await conn.fetch("""
            SELECT sighting_id, individual_id, count(*) AS count
            FROM clinical_records GROUP BY sighting_id, individual_id
            ORDER BY sighting_id, individual_id
        """)]
    by_sighting = {s["id"]: s for s in sightings}
    proposal_by_id = {p["id"]: p for p in proposals}
    counts = defaultdict(Counter)
    individual_counts = defaultdict(Counter)
    for proposal in proposals:
        counts[proposal["sighting_id"]]["proposals_outgoing"] += 1
        counts[proposal["candidate_sighting_id"]]["proposals_incoming"] += 1
        individual_counts[proposal["candidate_individual_id"]]["proposals_to_individual"] += 1
    for row in confirmations:
        counts[row["sighting_id"]]["confirmations_direct"] += row["count"]
        individual_counts[row["individual_id"]]["confirmations_to_individual"] += row["count"]
        proposal = proposal_by_id.get(row["proposal_id"])
        if proposal:
            for sid in {proposal["sighting_id"], proposal["candidate_sighting_id"]} - {None}:
                counts[sid]["confirmations_via_proposals"] += row["count"]
    for row in clinical:
        counts[row["sighting_id"]]["clinical_direct"] += row["count"]
        individual_counts[row["individual_id"]]["clinical_to_individual"] += row["count"]
    siblings = defaultdict(list)
    for photo in photos:
        siblings[photo["sighting_id"]].append(photo["id"])
    for sighting in sightings:
        sid, iid = sighting["id"], sighting["individual_id"]
        sighting["photo_ids"] = siblings[sid]
        sighting["dependencies"] = {
            key: counts[sid][key] for key in (
                "proposals_outgoing", "proposals_incoming", "confirmations_direct",
                "confirmations_via_proposals", "clinical_direct")}
        sighting["dependencies"].update({key: individual_counts[iid][key] if iid else 0
            for key in ("proposals_to_individual", "confirmations_to_individual",
                        "clinical_to_individual")})
    # Existing proposal evidence only: no O(N²) vector scan or new embedding.
    pairs = []
    for proposal in proposals:
        left = by_sighting.get(proposal["sighting_id"], {}).get("individual_id")
        right = (proposal["candidate_individual_id"] or
                 by_sighting.get(proposal["candidate_sighting_id"], {}).get("individual_id"))
        if left and right and left != right:
            pairs.append({"individual_ids": sorted([left, right]),
                          "proposal_id": proposal["id"], "score": proposal["score"],
                          "status": proposal["status"], "classification": "review_candidate"})
    return {"photos": photos, "sightings": sightings,
            "identified_pair_proposal_evidence": pairs,
            "dependency_counts_overlap": True}


async def fetch_photo(client, bucket, key, max_bytes):
    response = await client.get_object(Bucket=bucket, Key=key)
    async with response["Body"] as body:
        declared = response.get("ContentLength")
        if declared is not None and declared > max_bytes:
            raise ValueError("object exceeds byte limit")
        data = bytearray()
        while True:
            chunk = await body.read(min(64 * 1024, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                raise ValueError("object exceeds byte limit")
        if declared is not None and declared != len(data):
            raise ValueError("incomplete object response")
    raw = bytes(data)
    return raw, {"byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                 "etag": response.get("ETag"), "version_id": response.get("VersionId"),
                 "last_modified": str(response.get("LastModified", ""))}


class AuditDetector:
    def __init__(self, model_path, threshold, threads, expected_sha):
        import onnxruntime as ort
        from app import analyse as module
        from app.detect import load_upright

        refuse_symlinks(model_path)
        weights = model_path.read_bytes()
        if hashlib.sha256(weights).hexdigest() != expected_sha:
            raise ValueError("model changed while opening audit")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        session = ort.InferenceSession(weights, sess_options=options,
                                       providers=["CPUExecutionProvider"])
        self.decode = load_upright
        # A private function namespace reuses the real analysis recipe without
        # monkeypatching its module globals or the server's singleton/session.
        namespace = dict(module.analyse.__globals__)
        namespace.update(_get_session=lambda: session, REID_CONF_THRESHOLD=threshold)
        self.namespace = namespace
        self.analyse = types.FunctionType(module.analyse.__code__, namespace)

    def detect(self, raw, image):
        self.namespace["load_upright"] = lambda _: image
        return self.analyse(raw)


def error_result(stage, exc, **evidence):
    # SDK exception strings can carry signed URLs, hostnames or credentials.
    return {"status": f"{stage}_error", "error_type": type(exc).__name__, **evidence}


def thumbnail_name(photo_id):
    return "photo-" + hashlib.sha256(photo_id.encode()).hexdigest() + ".jpg"


async def inspect_photo(photo, client, bucket, detector, output, max_bytes, max_pixels, previous=None):
    try:
        raw, source = await fetch_photo(client, bucket, photo["s3_key"], max_bytes)
    except Exception as exc:
        return error_result("fetch", exc)
    if (previous and previous.get("status") in SUCCESS
            and previous.get("source") == source
            and output.check(thumbnail_name(photo["id"]))):
        return {**previous, "source": source}
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as header:
                if header.width * header.height > max_pixels:
                    raise ValueError("image exceeds pixel limit")
            image = detector.decode(raw)
    except Exception as exc:
        return error_result("decode", exc, source=source)
    try:
        thumb = image.copy()
        thumb.thumbnail((512, 512))
        # A fresh RGB image discards EXIF/ICC/XMP and any original metadata.
        clean = Image.new("RGB", thumb.size)
        clean.paste(thumb)
        buffer = io.BytesIO()
        clean.save(buffer, format="JPEG", quality=85)
        output.write(thumbnail_name(photo["id"]), buffer.getvalue())
        try:
            analysis = detector.detect(raw, image)
            confidence = [float(analysis.dog_confidence), float(analysis.cat_confidence)]
            if not all(math.isfinite(c) and 0 <= c <= 1 for c in confidence):
                raise ValueError("invalid detector confidence")
            return {"status": "animal_detected" if analysis.has_animal else "no_animal_candidate",
                    "source": source, "dog_confidence": confidence[0],
                    "cat_confidence": confidence[1], "box": analysis.box,
                    "thumbnail": thumbnail_name(photo["id"])}
        except Exception as exc:
            return error_result("detection", exc, source=source,
                                thumbnail=thumbnail_name(photo["id"]))
    finally:
        image.close()


def evidence_groups(inventory, results):
    groups = {kind: defaultdict(list) for kind in ("exact_bytes", "size_vector_candidate", "phash_candidate")}
    for photo in inventory["photos"]:
        result = results.get(photo["id"], {})
        if not result.get("revalidated_this_run"):
            continue
        source = result.get("source")
        if source:
            groups["exact_bytes"][source["sha256"]].append(photo["id"])
            for embedding in photo["embedding_digests"]:
                key = digest([source["byte_length"], embedding])
                groups["size_vector_candidate"][key].append(photo["id"])
        if photo["phash"]:
            groups["phash_candidate"][photo["phash"]].append(photo["id"])
    return {kind: [{"evidence_key": key, "photo_ids": ids,
                    "classification": "exact_byte_equality" if kind == "exact_bytes" else "review_candidate"}
                   for key, ids in sorted(buckets.items()) if len(ids) > 1]
            for kind, buckets in groups.items()}


def report(state):
    inventory, results = state["inventory"], state["results"]
    statuses = Counter(r["status"] for r in results.values())
    current = sum(bool(r.get("revalidated_this_run")) for r in results.values())
    no_animal = []
    for sighting in inventory["sightings"]:
        ids = sighting["photo_ids"]
        if ids and all(results.get(pid, {}).get("status") == "no_animal_candidate"
                       and results[pid].get("revalidated_this_run") for pid in ids):
            no_animal.append(sighting["id"])
    return {"deletion_approved": False, "purpose": "human review evidence only",
            "inventory_photo_count": len(inventory["photos"]), "status_counts": dict(statuses),
            "photos_revalidated_this_run": current,
            "full_current_pass": current == len(inventory["photos"]) and all(
                r["status"] in SUCCESS for r in results.values()),
            "no_animal_sighting_candidates": no_animal,
            "duplicate_evidence": evidence_groups(inventory, results),
            "limitations": ["No detector result authorises deletion; false negatives need human review.",
                "No-animal means every stored photo cleared processing but no dog/cat box passed the threshold.",
                "Clips are inventoried by key only; their unsampled frames are not analysed.",
                "Partial runs retain older evidence, explicitly marked not revalidated this run.",
                "S3 reads and the database snapshot are not a cross-system atomic snapshot.",
                "Identified-pair diagnostics use existing proposals, not a fresh Dogs similarity ranking.",
                "pHash grouping uses equal stored hashes only, not near-hash distance."]}


def contact_sheet(state):
    esc = lambda value: html.escape(str(value), quote=True)
    summary = state["summary"]
    parts = ["<!doctype html><html><head><meta charset='utf-8'>",
             "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; img-src 'self'; base-uri 'none'; form-action 'none'\">",
             "<title>Private media audit</title></head><body><h1>Private review evidence</h1>",
             "<p>Deletion is NOT approved. No-animal and inferred duplicate signals require human review.</p>",
             "<pre>" + esc(json.dumps(summary, indent=2)) + "</pre>"]
    photos = {p["id"]: p for p in state["inventory"]["photos"]}
    for sighting in state["inventory"]["sightings"]:
        parts.append("<section><h2>Sighting " + esc(sighting["id"]) + "</h2>")
        if sighting["id"] in summary["no_animal_sighting_candidates"]:
            parts.append("<strong>No-animal review candidate: inspect ALL siblings below.</strong>")
        parts.append("<pre>" + esc(json.dumps(sighting, indent=2)) + "</pre>")
        for pid in sighting["photo_ids"]:
            result = state["results"].get(pid, {"status": "not_processed"})
            parts.append("<figure><figcaption>" + esc(pid) + " — " + esc(result["status"]) + "</figcaption>")
            if result.get("thumbnail") == thumbnail_name(pid):
                parts.append("<img alt='Private review thumbnail' src='" + thumbnail_name(pid) + "'>")
            parts.append("<pre>" + esc(json.dumps({"photo": photos[pid], "result": result}, indent=2)) + "</pre></figure>")
        parts.append("</section>")
    parts.append("</body></html>")
    return "".join(parts).encode()


def save(output, state):
    state["summary"] = report(state)
    output.write("checkpoint.json", canonical(state))
    output.write("audit.json", canonical(state))
    output.write("contact-sheet.html", contact_sheet(state))


def start_state(output, manifest, inventory, resume):
    prior = output.read_json("checkpoint.json")
    if prior is not None and not resume:
        raise ValueError("checkpoint exists; use --resume or a new output directory")
    if resume and prior is None:
        raise ValueError("no checkpoint to resume")
    if prior is not None:
        if prior.get("manifest") != manifest or prior.get("inventory") != inventory:
            raise ValueError("inventory, model or immutable configuration changed; use a new output directory")
        if set(prior["results"]) - {p["id"] for p in inventory["photos"]}:
            raise ValueError("checkpoint contains unknown photos")
        state = prior
    else:
        state = {"manifest": manifest, "inventory": inventory, "results": {}, "run_number": 0}
    state["run_number"] += 1
    for result in state["results"].values():
        result["revalidated_this_run"] = False
    return state


class Controls:
    def __init__(self, pause, stop):
        self.pause, self.stop = pause, stop
        self.interrupted = False

    def requested(self, path):
        refuse_symlinks(path)
        return path.exists()

    async def ready(self):
        while not self.interrupted and not self.requested(self.stop):
            if not self.requested(self.pause):
                return True
            await asyncio.sleep(0.25)
        return False

    async def delay(self, seconds):
        until = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < until:
            if self.interrupted or self.requested(self.stop):
                break
            await asyncio.sleep(min(0.25, max(0, until - asyncio.get_running_loop().time())))


async def process(state, output, client, bucket, detector, controls, args):
    photos = sorted(state["inventory"]["photos"], key=lambda p: (
        state["results"].get(p["id"], {}).get("status") in SUCCESS,
        state["results"].get(p["id"], {}).get("checked_run", 0), p["id"]))
    save(output, state)
    attempted = 0
    for photo in photos:
        if args.max_photos is not None and attempted >= args.max_photos:
            break
        if not await controls.ready():
            break
        result = await inspect_photo(photo, client, bucket, detector, output,
                                     args.max_bytes, args.max_pixels, state["results"].get(photo["id"]))
        result.update(revalidated_this_run="source" in result, checked_run=state["run_number"],
                      checked_at=datetime.now(timezone.utc).isoformat())
        state["results"][photo["id"]] = result
        attempted += 1
        save(output, state)
        await controls.delay(args.delay)
    state["last_run_attempts"] = attempted
    state["stopped"] = controls.interrupted or controls.requested(controls.stop)
    save(output, state)
    return state["summary"]


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--model", type=Path, default=BACKEND / "app/ml/yolo26x.onnx")
    p.add_argument("--threshold", type=float, default=0.10)
    p.add_argument("--threads", type=int, default=1)
    p.add_argument("--max-bytes", type=int, default=25 * 1024 * 1024)
    p.add_argument("--max-pixels", type=int, default=40_000_000)
    p.add_argument("--max-photos", type=int)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--statement-timeout-ms", type=int, default=30_000)
    p.add_argument("--pause-file", type=Path)
    p.add_argument("--stop-file", type=Path)
    return p


async def run(args):
    import asyncpg
    import onnxruntime
    import PIL
    import numpy
    from botocore.config import Config
    from app.config import settings
    from app.storage.s3 import storage_from_settings

    if (not 0 < args.threshold <= 1 or not math.isfinite(args.delay) or args.delay < 0
            or not 1 <= args.threads <= 4 or args.max_bytes < 1 or args.max_pixels < 1
            or args.statement_timeout_ms < 1 or (args.max_photos is not None and args.max_photos < 1)):
        raise ValueError("invalid limits, thread count, delay or threshold")
    output = PrivateOutput(args.output)
    try:
        model_sha = file_sha(args.model)
        manifest = {"format_version": FORMAT_VERSION, "model_sha256": model_sha,
                    "threshold": args.threshold, "threads": args.threads,
                    "delay": args.delay, "statement_timeout_ms": args.statement_timeout_ms,
                    "max_bytes": args.max_bytes, "max_pixels": args.max_pixels,
                    "onnxruntime": onnxruntime.__version__, "pillow": PIL.__version__,
                    "numpy": numpy.__version__, "source_scope_sha256": digest([
                        settings.database_url, settings.s3_endpoint, settings.s3_bucket]),
                    "recipe_sha256": {name: file_sha(BACKEND / name) for name in (
                        "scripts/audit_media.py", "app/analyse.py", "app/detect.py", "app/detect_reid.py")}}
        conn = await asyncpg.connect(settings.database_url, timeout=15,
            server_settings={"default_transaction_read_only": "on",
                             "statement_timeout": str(args.statement_timeout_ms)})
        try:
            inventory = await snapshot(conn)
        finally:
            await conn.close()
        manifest["inventory_sha256"] = digest(inventory)
        state = start_state(output, manifest, inventory, args.resume)
        controls = Controls(args.pause_file or output.path / "PAUSE", args.stop_file or output.path / "STOP")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, setattr, controls, "interrupted", True)
        try:
            save(output, state)
            if controls.requested(controls.stop):
                return state["summary"]
            detector = AuditDetector(args.model, args.threshold, args.threads, model_sha)
            storage = storage_from_settings()
            # Only GetObject is used. Never call bucket provisioning or the
            # storage convenience methods that read an unbounded response.
            async with storage._session.client("s3", endpoint_url=storage.endpoint,
                aws_access_key_id=storage.access_key, aws_secret_access_key=storage.secret_key,
                region_name=storage.region, config=Config(connect_timeout=10, read_timeout=30,
                    retries={"max_attempts": 2}, max_pool_connections=1)) as client:
                return await process(state, output, client, storage.bucket, detector, controls, args)
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
    finally:
        output.close()


def main():
    args = parser().parse_args()
    try:
        summary = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Interrupted; rerun with --resume if a checkpoint exists.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Do not print database URLs or SDK exception payloads to a terminal log.
        print(f"Audit stopped ({type(exc).__name__}); no remote data was modified. "
              "Check private output permissions, limits, credentials and immutable resume inputs.", file=sys.stderr)
        return 1
    print(json.dumps({key: value for key, value in summary.items()
                      if key not in {"duplicate_evidence", "no_animal_sighting_candidates"}}, indent=2))
    return 0 if summary["full_current_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
