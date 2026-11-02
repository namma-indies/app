# Deleting media from the object store

For whoever runs the box. Written for #54 / #179: an observer can now delete a
photo (and, with #179, their account). The rows go at once; this page is
about the bytes in the bucket.

`<media-bucket>` is the bucket in `S3_BUCKET`. Real names and keys live in the
private ops repo, not here: this repo is public.

## What the app owes, and where it is recorded

| Deleted | Owed from the bucket | Recorded in |
|---|---|---|
| a whole upload ("Delete photo") | everything under `captures/{capture_id}/` and `sightings/{sighting_id}/` | `media_purges`, `kind='prefix'` |
| one animal of a photo with several ("Remove this dog") | its crop and thumbnail, and its voice notes | `media_purges`, `kind='key'` |
| a voice note added later (#159) | its audio | `media_purges`, `kind='key'` |
| a photo that was posted to X | the post on X | `x_post_purges` |

The record is written in the same transaction as the deletion. The API tries
the store straight after the response, bounded to 30 s; whatever it cannot
delete stays recorded, with the error, until `scripts/purge_media.py` deletes
it. X posts are retried by the daily post run (`scripts/post_daily.py`).

## Decision U1: may the API delete objects itself?

Today the API's credential has List, Get and Put but no `s3:DeleteObject`, so
a compromised or buggy API cannot erase the photo corpus. Both answers work:

### A. Keep it as it is (no delete for the API)

Nothing to change. Every deletion is recorded and waits. Run, inside the
container with a credential that may delete:

```
cd /app/backend
uv run python scripts/purge_media.py            # dry run: how many are owed
uv run python scripts/purge_media.py --apply
```

How quickly deleted photos really leave the bucket is then however often this
is run. The privacy policy (#183) must state that window. Weekly or more often is
suggested.

### B. Let the API delete, under guard rails

Deletion then completes within seconds. Three changes, together:

**1. Grant delete on the media prefixes only, never on `backups/`.**
Added to the API user's policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DeleteOwnedMedia",
      "Effect": "Allow",
      "Action": "s3:DeleteObject",
      "Resource": [
        "arn:aws:s3:::<media-bucket>/sightings/*",
        "arn:aws:s3:::<media-bucket>/captures/*"
      ]
    },
    {
      "Sid": "ListOwnedMedia",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::<media-bucket>",
      "Condition": {"StringLike": {"s3:prefix": ["sightings/*", "captures/*", "erasures/*"]}}
    }
  ]
}
```

If the existing policy already grants `s3:ListBucket` on the whole bucket, the
second statement adds nothing. A prefix purge needs List, because it lists the
upload's folder and deletes each object in it.

**2. Turn on bucket versioning, and expire old versions after 14 days.**
A delete then leaves a recoverable version for two weeks, so a bug that
deletes the wrong thing can be undone. Deleted photos are fully gone from the
bucket 14 days later.

```
aws s3api put-bucket-versioning --bucket <media-bucket> \
  --versioning-configuration Status=Enabled
```

**3. Lifecycle rules** (one configuration; `put-bucket-lifecycle-configuration`
replaces the whole set, so merge with the existing `backups/` expiry rule
first: `aws s3api get-bucket-lifecycle-configuration --bucket <media-bucket>`):

```json
{
  "Rules": [
    {
      "ID": "media-noncurrent-14d",
      "Filter": {"Prefix": ""},
      "Status": "Enabled",
      "NoncurrentVersionExpiration": {"NoncurrentDays": 14},
      "Expiration": {"ExpiredObjectDeleteMarker": true}
    },
    {
      "ID": "media-staging-1d",
      "Filter": {"Prefix": "media-staging/"},
      "Status": "Enabled",
      "Expiration": {"Days": 1}
    }
  ]
}
```

`media-staging/` holds the GPU worker's temporary uploads, keyed by job rather
than by owner. Once a job's row is gone nothing can trace them to an upload, so
expiry is the only way they leave.

With versioning on, `backups/` keeps old versions for 14 days as well. Check
the existing backup expiry still says what the privacy policy says.

To undo a mistaken delete within the window, restore the previous version:
`aws s3api list-object-versions --bucket <media-bucket> --prefix captures/<id>/`
and copy the wanted `VersionId` back over the key.

## Decision U2: the erasure ledger (backup restores)

A database restored from a backup taken before a deletion brings the deleted
rows back. With the ledger on, every deletion also writes an id-only record
(`{"kind", "id", "at"}`) to `erasures/YYYY-MM-DD/` in the media bucket, which a
restore does not touch. The API already has Put; no new permission is needed.

- **Turn on:** `ERASURE_LEDGER_ENABLED=true` in the box `.env`, then restart the API.
  While it is on, a deletion whose ledger write fails is refused, and nothing is deleted.
  The person is asked to try again.
- **After any restore**, before letting traffic back in:

  ```
  cd /app/backend
  uv run python scripts/replay_erasures.py --since <when the backup was taken, ISO 8601 with timezone>
  uv run python scripts/replay_erasures.py --since <same> --apply
  uv run python scripts/purge_media.py --apply      # under option A
  ```

  The replay is safe to repeat.
- **Retention:** keep `erasures/` at least as long as the oldest backup you
  might restore. It holds no personal data, only ids and times.

Without the ledger, a restore must be followed by re-applying deletions by
hand from logs (`erased capture=… reason=…` lines from `app.erasure`). That's
only as good as log retention.
