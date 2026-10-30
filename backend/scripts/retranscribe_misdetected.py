"""Transcribe again the voice notes and clip sound whisper misheard as some other language (#164).

Before the GPU worker kept whisper to English and the Indian languages (#154),
auto-detection could hear English as, say, Malay ("ms") and write the note out
in Malay. This finds every transcript whose language is outside that set (the
worker's own list, `gpu_worker.transcribe.ALLOWED`) and queues its sound to be
transcribed again.

For each one, in one transaction: its transcription job goes back to pending
with a fresh count of attempts, the suggestions still open from it are
removed, and the transcript itself is deleted. The worker then transcribes the
sound again, and the new transcript is read for suggestions like any other.
Details the observer already chose stay: they are kept on the sighting, not
on the transcript.

**Run it only after the DGX worker has been updated with #154**: before
that, whisper would hear the same thing again. `--apply` therefore also needs
`--dgx-updated`, to say so.

**Dry-run is the default**: it lists what it would queue (ids, source,
language, date; never the words) and writes nothing.

Usage, from /app/backend inside the container:

    uv run python scripts/retranscribe_misdetected.py                           # dry run
    uv run python scripts/retranscribe_misdetected.py --apply --dgx-updated
    uv run python scripts/retranscribe_misdetected.py --apply --dgx-updated --limit 20

Needs MEDIA_JOBS_ENABLED and TRANSCRIPTION_ENABLED on, with the worker
running, for the queued jobs to be taken. Safe to repeat: a transcript in an
allowed language is never touched, and one already re-queued has no
transcript left to find.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import effective_dsn  # noqa: E402
from app.transcription_jobs import PIPELINE_VERSION  # noqa: E402
from gpu_worker.transcribe import ALLOWED  # noqa: E402

FOUND = """
    SELECT id, capture_id, source, language, created_at FROM voice_transcripts
    WHERE pipeline_version = $1 AND outcome = 'transcribed' AND NOT (language = ANY($2::text[]))
    ORDER BY created_at, id LIMIT $3
"""


@dataclass
class Report:
    found: list = field(default_factory=list)
    queued: int = 0
    no_job: int = 0


async def _same_sound(conn) -> str:
    """How a transcript finds its job: by capture and source, and by the note
    added later it came from where notes can be added (#156)."""
    has_note = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_name='voice_transcripts' AND column_name='note_id')")
    return " AND j.note_id IS NOT DISTINCT FROM t.note_id" if has_note else ""


async def run(pool: asyncpg.Pool, *, apply: bool, limit: int | None = None) -> Report:
    rep = Report()
    async with pool.acquire() as conn:
        rep.found = [dict(r) for r in await conn.fetch(FOUND, PIPELINE_VERSION, list(ALLOWED), limit)]
        if not apply:
            return rep
        same_sound = await _same_sound(conn)
        for t in rep.found:
            async with conn.transaction():
                job = await conn.fetchval(
                    f"""SELECT j.id FROM transcription_jobs j JOIN voice_transcripts t ON t.id = $1
                        WHERE j.capture_id = t.capture_id AND j.source = t.source
                          AND j.pipeline_version = t.pipeline_version{same_sound}
                        FOR UPDATE OF j""", t["id"])
                if job is None:
                    rep.no_job += 1
                    continue
                await conn.execute(
                    "DELETE FROM detail_suggestions WHERE transcript_id=$1 AND status='open'", t["id"])
                await conn.execute("DELETE FROM voice_transcripts WHERE id=$1", t["id"])
                await conn.execute(
                    """UPDATE transcription_jobs SET status='pending', attempts=0, run_after=now(),
                       lease_token_hash=NULL, lease_owner=NULL, lease_expires_at=NULL, last_error=NULL,
                       updated_at=now() WHERE id=$1""", job)
                rep.queued += 1
    return rep


def describe(rep: Report, *, apply: bool) -> str:
    lines = [("APPLIED" if apply else "DRY RUN -- nothing written"),
             f"transcripts in a language outside {', '.join(ALLOWED)}: {len(rep.found)}"]
    lines += [f"  {t['id']}  capture={t['capture_id']}  {t['source']}  {t['language']}  {t['created_at']:%Y-%m-%d}"
              for t in rep.found]
    if apply:
        lines += [f"queued to transcribe again: {rep.queued}",
                  f"no transcription job to re-queue (left as they are): {rep.no_job}"]
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="re-queue them; without it nothing is written")
    ap.add_argument("--dgx-updated", action="store_true",
                    help="confirm the DGX worker runs the language fix (#154); required with --apply")
    ap.add_argument("--limit", type=int, default=None, help="at most this many, oldest first")
    args = ap.parse_args()
    if args.apply and not args.dgx_updated:
        ap.error("--apply re-transcribes with whatever worker is running: update the DGX first, "
                 "then pass --dgx-updated as well")
    pool = await asyncpg.create_pool(effective_dsn(), min_size=1, max_size=2)
    try:
        rep = await run(pool, apply=args.apply, limit=args.limit)
    finally:
        await pool.close()
    print(describe(rep, apply=args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
