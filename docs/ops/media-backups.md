# Media backup and retention

Companion to [media deletion](media-deletion.md). Actual identifiers, IAM
identities, bucket configuration and drill evidence belong in private ops.
Configure this before declaring #187 complete.

Version `<media-bucket>` and retain noncurrent versions for **14 days**, then
expire them/delete markers as in the deletion runbook. Keep an independently
administered encrypted `<media-replica-bucket>` for bucket/account failure.
Prefer a separate backup identity/account; the API must not control that copy.
The region/processor choice needs the privacy owner's approval.

Replicate `captures/` and `sightings/`, including originals, thumbnails, clips
and voice notes. Version both buckets, enable delete-marker replication and the
same 14-day noncurrent expiry on the replica. Inventory/copy the existing corpus
too: live replication does not establish historical coverage by itself.
Monitor replication failures and compare a private inventory after seeding.
This code does not provision or claim to verify that infrastructure.

Protect/copy `erasures/` separately for at least the lifetime of **every**
restorable database backup. Keep `media-staging/` on its one-day expiry;
it is temporary worker output. Keep `backups/` under the
[database policy](../../deploy/BACKUPS.md#retention), and the fixed
`monitoring/storage-probe` under noncurrent expiry. Do not apply a whole-bucket
current-object expiry to live media or the erasure ledger.

The API outbox and deletion credential choices remain as in
[decision U1](media-deletion.md#decision-u1-may-the-api-delete-objects-itself).
Record how often option A's privileged purger runs, or enable option B through
the guarded procedure. Erasure applies to every copy.
[S3 replication](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication-what-is-isnot-replicated.html)
does not propagate lifecycle expiry or permanent deletion of a specific version:
the replica needs its own lifecycle and an explicit privileged purge procedure
for exceptional all-version erasure. Test deletion on both copies. A marker
hides current media while old versions remain for the agreed 14-day window,
subject to asynchronous lifecycle processing. Reconcile wording with #183;
avoid Object Lock that prevents the agreed erasure window.

[Lifecycle configuration](https://docs.aws.amazon.com/AmazonS3/latest/userguide/intro-lifecycle-rules.html)
replaces the rule set: merge rather than overwrite existing rules. The deletion
runbook's global 14-day noncurrent rule affects backups too. Keep `erasures/`
until the oldest backup/replica/version is no longer restorable.

Before launch and weekly for the first two weeks, use a **synthetic** upload:
verify original/thumbnail/audio reach the independent copy; delete it; verify
neither copy serves the current object; privately inspect old versions and
both lifecycle/purge procedures. Record date/result/owner privately and repeat
after replication, lifecycle, IAM or deletion changes.

Recover selected versions into an isolated destination. Replay the current
erasure ledger before restoring traffic or copying media back, excluding erased
uploads; a blind copy would resurrect them. Apply this to source and replica.
The automated database drill never publishes media or starts the application.

Proposed targets, **unproven and awaiting agreement** in #201: 24-hour database
recovery point, media-copy lag below 24 hours checked daily, and a four-hour
recovery rehearsal. Measure them before making any user-facing promise.
