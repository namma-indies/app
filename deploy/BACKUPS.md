# Database backups and restore drills

Media bytes cannot reconstruct sightings, identities, confirmations or vectors.
The existing `backup-db.timer` runs nightly at **01:00 Asia/Kolkata** through
`remote-backup-db.sh`; **Back up the database** is the manual workflow for the
same path. It creates `pg_dump -Fc`, verifies its table of contents, then uploads
to `backups/`. Immediately before the dump it records row counts of the key
tables and the Alembic head (`deploy/backup-manifest.sql`); after the upload it
writes them beside the dump as `backups/indiedex-<stamp>.json` (the manifest).
After the upload is read back it writes a tiny marker,
`backups/latest.json` (dump key, size, `created_at`, `pgdmp_verified`). Detailed
health reads **only that marker** for validity and 36-hour freshness; it never
lists or reads database dumps. A successful restore still needs the drill below.

### Narrowing the API's storage permissions

Because health reads only the marker, the application identity no longer needs
to read database dumps at all. In the private policy, grant it on the backup
prefix only:

- `s3:PutObject` on `arn:aws:s3:::<bucket>/backups/*` (the backup job runs with
  the application identity and writes dumps, manifests and the marker);
- `s3:ListBucket` on `arn:aws:s3:::<bucket>` with an `s3:prefix` condition of
  `backups/` (the job's post-upload read-back and `--list` inventory);
- `s3:GetObject` on `arn:aws:s3:::<bucket>/backups/latest.json` **only**.

Remove any broader GetObject on `backups/*`. Downloading a dump to restore uses
a separate, operator-held read identity (as the drill does), not the API's.

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
vector dimensions, validated foreign keys and photo ownership. It also
downloads the dump's **manifest** and requires the restored schema head to
match it exactly and each key table's restored row count to be within
`max(--count-slack, --count-tolerance × expected)` of the recorded count
(defaults 10 rows / 1%; counts are taken seconds before the dump, so a busy
moment can differ slightly). A latest backup without a manifest fails
(`manifest_missing`) unless `--allow-missing-manifest` is given for a backup
taken before manifests existed. An intentionally
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
**Database restore drill** on the default branch. The same variable also turns
on the workflow's optional **weekly schedule** (Monday ~03:53 Asia/Kolkata,
after the nightly backup); with it unset neither dispatch nor schedule runs.

To let detailed health see the last successful drill, set repository variable
`RESTORE_DRILL_RECORD_PASS=true` and grant the drill identity `s3:PutObject` on
`arn:aws:s3:::<backup-source-bucket>/monitoring/restore-drill.json` only. On a
pass the drill writes that marker (pass time, backup time, whether a manifest
was compared; no counts). Grant the API `s3:GetObject` on the same single key.
`/health/detailed` reports it as `restore_drill`; set
`HEALTH_RESTORE_DRILL_REQUIRED=true` once the drill is running so a missing or
older-than-`HEALTH_RESTORE_DRILL_STALE_S` (default 8 days) pass degrades health.
Until then it shows `disabled` with the age. Default minimum sightings and
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
python backend/scripts/restore_db_drill.py --archive /path/to/synthetic.dump \
  --manifest /path/to/synthetic-counts.json   # optional: backup-manifest.sql output
```

For the real latest-backup drill, Aswin supplies the separate AWS identity,
region and `RESTORE_BACKUP_BUCKET` privately, with the AWS CLI installed, then
runs `python backend/scripts/restore_db_drill.py`. It never reads app `.env`.
Default temporary data capacity is 2 GiB tmpfs with a 3 GiB memory limit; adjust
`--data-mib` on a suitably sized isolated runner for a larger corpus.
`--max-age-hours` permits an explicit historical rehearsal, not a silent waiver
of freshness in scheduled health checks.

## Restoring — step by step

The half that only ever runs on the worst day, so read it before you need it.
Everything in `<angle brackets>` is a placeholder; the real values live in
private ops. Run on the app host from `<app-checkout>`, with
`C="sudo docker compose -f docker-compose.prod.yml"`. The drill above is the
rehearsal of steps 1–3; steps 4–7 only happen in a real recovery, with the
operator's authorisation.

**1. Choose and fetch a backup** with the separate operator read identity
(`<restore-read-profile>`: List on `backups/`, Get on `backups/*`). The API's
own identity can no longer read dumps.

```sh
aws --profile <restore-read-profile> s3api list-objects-v2 \
  --bucket <backup-bucket> --prefix backups/indiedex- --query 'Contents[].[Key,Size]' --output text
aws --profile <restore-read-profile> s3 cp \
  s3://<backup-bucket>/backups/indiedex-<YYYYMMDDTHHMMSSZ>.dump /tmp/restore.dump
aws --profile <restore-read-profile> s3 cp \
  s3://<backup-bucket>/backups/indiedex-<YYYYMMDDTHHMMSSZ>.json /tmp/restore.manifest.json
head -c 5 /tmp/restore.dump; echo   # must print PGDMP
```

Optionally prove it first in isolation:
`python backend/scripts/restore_db_drill.py --archive /tmp/restore.dump --manifest /tmp/restore.manifest.json`.

**2. Restore into a new, separate database** — never over the live one:

```sh
$C cp /tmp/restore.dump db:/tmp/restore.dump
$C exec -T db createdb -U postgres indiedex_restored < /dev/null
$C exec -T db pg_restore --exit-on-error --single-transaction --no-owner \
  -U postgres -d indiedex_restored /tmp/restore.dump < /dev/null
```

Errors mentioning a *table* mean stop. The archive creates the `postgis` and
`vector` extensions itself.

**3. Check the numbers against the manifest, not the exit code:**

```sh
$C exec -T db psql -U postgres -d indiedex_restored -XqAt -v ON_ERROR_STOP=1 \
  -c "$(cat deploy/backup-manifest.sql)" < /dev/null
cat /tmp/restore.manifest.json
```

Schema head must match and counts should be within a few rows. An embedding
count of zero means the vectors did not come across and re-ID would be inert.

**4. Stop writes**, then swap the databases (keeping the old one):

```sh
$C stop app
$C exec -T db psql -U postgres -v ON_ERROR_STOP=1 \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='indiedex' AND pid<>pg_backend_pid()" \
  -c "ALTER DATABASE indiedex RENAME TO indiedex_before_restore_<YYYYMMDD>" \
  -c "ALTER DATABASE indiedex_restored RENAME TO indiedex" < /dev/null
```

**5. Replay the erasure ledger** from the backup's `created_at` (in the
manifest) and complete purges, **before** traffic. Dry-run first, then apply:

```sh
$C run --rm --no-deps --entrypoint "" app sh -c \
  'cd /app/backend && uv run python scripts/replay_erasures.py --since <backup-created-at>'
$C run --rm --no-deps --entrypoint "" app sh -c \
  'cd /app/backend && uv run python scripts/replay_erasures.py --since <backup-created-at> --apply'
```

Then reconcile the items in "Actual disaster recovery" below.

**6. Bring the app back** (the entrypoint applies any newer migrations):

```sh
$C start app
curl -s https://<app-origin>/health
curl -s -H "Authorization: Bearer <health-token>" https://<app-origin>/health/detailed
```

**7. Clean up** once satisfied: `rm -f /tmp/restore.dump /tmp/restore.manifest.json`,
`$C exec -T db rm -f /tmp/restore.dump < /dev/null`, and drop
`indiedex_before_restore_<YYYYMMDD>` only after the operator agrees. Take a
fresh backup and record the event privately.

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
