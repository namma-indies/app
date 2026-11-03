# Database backups and restore drills

Media bytes cannot reconstruct sightings, identities, confirmations or vectors.
The existing `backup-db.timer` runs nightly at **01:00 Asia/Kolkata** through
`remote-backup-db.sh`; **Back up the database** is the manual workflow for the
same path. It creates `pg_dump -Fc`, verifies its table of contents, then uploads
to `backups/`. Detailed health now checks the newest archive's size/header and
36-hour freshness. A successful restore still needs the drill below.

Real hosts, directories, bucket names, credentials, inventories and drill
evidence belong in private ops. Use placeholders here.

## Enable the nightly timer

Aswin installs the existing service/timer through the private operator procedure.
The units assume `/home/ubuntu/app`; adapt the private installation copy to
`<app-checkout>` and the deploy user. Copy units into `/etc/systemd/system/`, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now backup-db.timer
systemctl list-timers backup-db.timer
sudo systemctl start backup-db.service
journalctl -u backup-db.service -n 40 --no-pager
```

Verify successful execution and a fresh backup in detailed health. Preserve the
existing workflow's `DEPLOY_SSH_KEY`, `DEPLOY_SSH_TARGET` and forced-command
`backup-db` setup. The drill uses a separate read-only identity. Take a backup
before a risky migration.

## Retention

Proposed launch default: expire current `backups/` objects after **30 days**.
Merge this single rule with the existing lifecycle configuration:

```json
{
  "ID": "database-backups-30d",
  "Filter": {"Prefix": "backups/"},
  "Status": "Enabled",
  "Expiration": {"Days": 30}
}
```

The [media deletion](../docs/ops/media-deletion.md) runbook's global noncurrent
version retention is **14 days**. With versioning, current expiry creates a
delete marker and the backup can remain recoverable for the additional window:
plan for roughly **44 days plus lifecycle processing lag**, not byte erasure on
day 30. Verify both rules privately. Keep `erasures/` at least as long as **any**
backup, replica or noncurrent version can be restored; leave its expiry disabled
until that bound is established. Monthly/yearly archives require extending the
ledger and agreeing the privacy window too.

The script does not prune. Keep backup DeleteObject/DeleteObjectVersion away
from API/drill credentials even if guarded media deletion is granted. Record
independent backup copy policy/effective retention privately alongside
[media protection](../docs/ops/media-backups.md). Confirm #183 wording first.

## Restore drill — Aswin runs this

**Database restore drill** is manual and default-off. It downloads the latest
matching backup, refuses a missing/stale/future-dated or damaged newest archive,
and restores into a generated disposable Postgres 16 + PostGIS + pgvector.
The container has no published port, external network, application, deployment
database credentials, host data mount or persistent database volume. The target
is always generated; no database URL/existing destination is accepted.

Checks cover a custom-format archive with core TABLE DATA entries (schema-only
is refused), full `pg_restore --exit-on-error --single-transaction`, extensions,
one known Alembic revision, readable core tables, minimum sightings/vectors,
vector dimensions, validated foreign keys and photo ownership. An intentionally
unvalidated CHECK constraint is distinguished from a failed foreign-key restore.
The generated container and downloaded file are discarded on success/failure.

Only fixed pass/fail classifications reach logs: no rows, counts, dump contents,
keys or raw database errors, and no artifacts. The hosted runner processes a
plaintext temporary backup and is discarded afterwards. **Approve that processing
privately before enabling.** If backups must stay inside the existing hosting
boundary, change `runs-on` in the private workflow copy to a dedicated ephemeral
self-hosted runner with Docker/AWS CLI/Python, restricted to the trusted workflow.
Use neither the production host nor a runner shared with untrusted PR jobs.

Create GitHub environment `backup-restore`, restricted to the default branch,
with operator approval appropriate for backup access. Set environment secrets:

| Secret | Value |
|---|---|
| `RESTORE_BACKUP_BUCKET` | `<backup-source-bucket>` |
| `RESTORE_BACKUP_ACCESS_KEY_ID` | Dedicated read-only backup identity |
| `RESTORE_BACKUP_SECRET_ACCESS_KEY` | Its secret |
| `RESTORE_BACKUP_ENDPOINT` | Optional custom HTTPS S3 origin; empty uses regional AWS endpoint |

Set `RESTORE_BACKUP_REGION` (environment/repository variable, default
`ap-south-1`). IAM needs ListBucket with prefix `backups/` and GetObject on
`arn:aws:s3:::<backup-source-bucket>/backups/*`, plus Decrypt for the specific KMS
key if applicable. No database credential or write/delete grant is needed.
Record actual source, encryption, policy and secret locations privately.

Set **repository variable** `RESTORE_DRILL_ENABLED=true`, then dispatch
**Database restore drill** on the default branch. Default minimum sightings and
MiewID embeddings are each 1; choose useful private expected lower bounds.
Zero is reserved for a deliberately empty test corpus. Freshness defaults to
36 hours. Keep exact counts and evidence in private ops.

Run before launch, weekly for the first two weeks, and after schema, backup,
image or restore changes. CI exercises this same script on synthetic data with
a photo, identity, human verdict and MiewID vector; it never downloads deployment
backups. **No agent has run the workflow against production.**

For a local synthetic rehearsal (Docker and Python 3.12+):

```sh
docker build -t indiedex-restore-db ./docker/db
python backend/scripts/restore_db_drill.py --archive /path/to/synthetic.dump
```

For the real latest-backup drill, Aswin supplies the separate AWS identity,
region and `RESTORE_BACKUP_BUCKET` privately, with the AWS CLI installed, then
runs `python backend/scripts/restore_db_drill.py`. It never reads app `.env`.
Default temporary data capacity is 2 GiB tmpfs with a 3 GiB memory limit; adjust
`--data-mib` on a suitably sized isolated runner for a larger corpus.
`--max-age-hours` permits an explicit historical rehearsal, not a silent waiver
of freshness in scheduled health checks.

## Actual disaster recovery

The drill never promotes its database. The private operator recovery procedure
must stop writes, choose a validated backup, restore into a new isolated DB,
compare counts, replay the **current** erasure ledger from the backup time and
complete purges **before** reconnecting traffic. Use `replay_erasures.py` and
[decision U2](../docs/ops/media-deletion.md#decision-u2-the-erasure-ledger-backup-restores).
Configure `ERASURE_LEDGER_ENABLED=true` before deletions for this guarantee.

Account requests still in grace are not yet erased/on the ledger: reconcile
post-snapshot requests/notices as in the deletion runbook, plus later moderation
and blocking decisions privately. Verify media source/replica erasures. The
operator authorises promotion and traffic. Keep #187 open until real rehearsal,
activation and people work are accepted.
