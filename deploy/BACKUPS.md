# Database backups

## What is and is not at risk

Photos are in S3, and the app's IAM user has List, Get and Put but **no
DeleteObject** — a compromised app cannot erase the photo corpus.

The database is not like that. Sightings, individuals, embeddings and
`confirmations` live in one Docker volume on one Lightsail box, and the Seoul
rollback box was deleted on 2026-08-16. There is no second copy.

Losing it is worse than it sounds. A photo without its sighting is an anonymous
picture of a dog: no animal, no observer, no place, no date. And
`confirmations` is not just history, it is the labelled set the matching
thresholds are supposed to be fitted against — the scarcest data in the system,
because every row is a human decision someone made once.

## What runs

`deploy/backup-db.timer` fires `deploy/remote-backup-db.sh` nightly at 01:00
IST. Each run:

1. `pg_dump -Fc` inside the `db` container.
2. `pg_restore --list` on the result, **before** it is treated as a backup. A
   dump cut off mid-write is still a file.
3. Uploads from the `app` container, which already holds the S3 credentials, so
   nothing new has to be distributed.
4. Prints the whole inventory. The failure that matters is not a run that
   errors, it is a run that quietly stopped happening months ago.

`scripts/backup_db.py` refuses anything that does not begin with `PGDMP`, which
is how a `pg_dump: error:` message landing in the redirect gets caught instead
of stored as a backup.

Trigger one by hand from Actions → "Back up the database", or on the box:

```sh
bash ~/app/deploy/remote-backup-db.sh
```

Take one deliberately before any risky migration.

## Installing the timer

Once per box, as the deploy user:

```sh
sudo cp ~/app/deploy/backup-db.service ~/app/deploy/backup-db.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now backup-db.timer
systemctl list-timers backup-db.timer
```

Both units hardcode `/home/ubuntu`. Adjust if the deploy user differs.

Confirm it actually worked rather than assuming:

```sh
sudo systemctl start backup-db.service
journalctl -u backup-db.service -n 40 --no-pager
```

## Retention

**Not handled by the script, deliberately.** The app's IAM has no
`DeleteObject`, which is exactly why a compromised app cannot erase the photos.
Giving this job the permission to prune would hand that back.

Expire old backups with an S3 lifecycle rule scoped to the `backups/` prefix.
It is server-side, needs no credential on the box, and cannot be turned against
`sightings/`. Something like 30 daily plus 12 monthly is plenty for a pilot.

## Restoring

The half that only ever runs on the worst day, so read it before you need it.

**Find and fetch a backup**, from any box that can reach the bucket:

```sh
cd ~/app
sudo docker compose -f docker-compose.prod.yml exec -T app \
  sh -c 'cd /app/backend && uv run python scripts/backup_db.py --list' < /dev/null

sudo docker compose -f docker-compose.prod.yml exec -T app \
  sh -c 'cd /app/backend && uv run python scripts/backup_db.py \
         --fetch indiedex-20260904T193005Z.dump --to /tmp/restore.dump' < /dev/null
```

`--fetch` re-runs the same header check on the way down, because a bucket can
hand back a zero-length object for a key that exists.

**Restore into the database:**

```sh
sudo docker compose -f docker-compose.prod.yml cp \
  $(sudo docker compose -f docker-compose.prod.yml ps -q app):/tmp/restore.dump /tmp/restore.dump
sudo docker compose -f docker-compose.prod.yml cp /tmp/restore.dump db:/tmp/restore.dump

# Stop the app first. Restoring under live writes gives you a database that is
# neither the backup nor what was there before.
sudo docker compose -f docker-compose.prod.yml stop app

sudo docker compose -f docker-compose.prod.yml exec -T db \
  pg_restore --clean --if-exists --no-owner -U postgres -d indiedex \
  /tmp/restore.dump < /dev/null

sudo docker compose -f docker-compose.prod.yml start app
curl -s https://app.nammaindies.org/health
```

`--clean --if-exists` drops and recreates each object, so this overwrites the
current database. On a **fresh** box, let the entrypoint run `alembic upgrade
head` first so the extensions exist, then restore over it.

Expect noise about the `postgis` and `vector` extensions already existing. That
is fine. What is not fine is an error mentioning a *table*.

**Then check the numbers, not the exit code:**

```sh
sudo docker compose -f docker-compose.prod.yml exec -T db psql -U postgres -d indiedex \
  -c 'select count(*) from sightings' \
  -c 'select count(*) from individuals' \
  -c 'select count(*) from confirmations' \
  -c 'select count(*) from embeddings where vec_miew is not null' < /dev/null
```

An embedding count of zero after a restore that reported success means the
vectors did not come across, and re-ID will be silently inert.

## Test the restore

A backup that has never been restored is a hypothesis. Once, on the staging box:
fetch the newest production dump, restore it there, and compare those four
counts against production. Do it again after any change to this file.
