"""Read-only re-ID baseline: freeze a private snapshot, then export metrics.

Only Aswin runs snapshot in the production environment, with a read-only role.
The implementation and CI use synthetic local data. No media or inference.
See docs/reid-evaluation.md for denominators, limitations and holdout rules.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.reid_eval.core import evaluate  # noqa: E402
from app.reid_eval.schema import validate_metrics, validate_snapshot  # noqa: E402


def write_private(path, data, validator):
    validator(data)
    encoded = json.dumps(data, sort_keys=True, indent=2, allow_nan=False) + "\n"
    # Frozen outputs: no clobbering, no following an existing symlink, mode 0600.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(encoded)


async def snapshot(args):
    import asyncpg
    from app.reid_eval.snapshot import take_snapshot

    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        raise ValueError("set the requested DSN environment variable to a read-only database role")
    conn = await asyncpg.connect(dsn)
    try:
        data = await take_snapshot(conn, limit=args.limit, seed=args.seed, folds=args.folds,
            phash_distance=args.phash_distance, holdout_fraction=args.holdout_fraction, sleep=args.sleep)
    finally:
        await conn.close()
    write_private(args.out, data, validate_snapshot)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot", help="read-only inventory and frozen scores; stays in the source environment")
    snap.add_argument("--out", required=True, type=Path)
    snap.add_argument("--dsn-env", default="REID_EVAL_DATABASE_URL", help="name of an explicitly supplied read-only DSN variable")
    snap.add_argument("--limit", type=int, default=0, help="earliest N encounters for a smoke test; 0 means all")
    snap.add_argument("--sleep", type=float, default=0, help="seconds to pause per query phase")
    snap.add_argument("--phash-distance", type=int, default=4, help="conservative near-copy leakage grouping, not an identity label")
    snap.add_argument("--seed", type=int, default=189)
    snap.add_argument("--folds", type=int, default=5)
    snap.add_argument("--holdout-fraction", type=float, default=.3)
    score = commands.add_parser("score", help="pure offline scoring; writes aggregate-only metrics")
    score.add_argument("--snap", required=True, type=Path)
    score.add_argument("--out", required=True, type=Path)
    score.add_argument("--split", choices=("development", "holdout"), default="development")
    score.add_argument("--decision-id", type=UUID, help="recorded decision UUID; required to open the holdout")
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            asyncio.run(snapshot(args))
        else:
            with args.snap.open(encoding="utf-8") as source:
                data = json.load(source)
            report = evaluate(data, split=args.split,
                              decision_id=str(args.decision_id) if args.decision_id else None)
            write_private(args.out, report, validate_metrics)
    except Exception as exc:
        # Never echo a DSN, database error, user value or row into an export/log.
        print(f"reid_eval failed ({type(exc).__name__}); check inputs and the private protocol", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
