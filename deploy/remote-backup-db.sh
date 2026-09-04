# Back the database up to object storage. Runs on the box, nightly via
# deploy/backup-db.timer, on demand via the backup-db.yml workflow, or by hand.
#
# Everything the app knows lives in one Docker volume on one box. The photos are
# safe in S3; the rows are not, and the rows are the part that cannot be
# reconstructed -- which animal, seen by whom, where, and every human verdict in
# `confirmations`. The Seoul rollback box was deleted on 2026-08-16, so there is
# no second copy of any of it.
#
# `< /dev/null` on every `exec -T` is not decoration. This file can arrive as
# stdin over SSH, and a command that reads stdin then eats the rest of the
# script -- bash reaches that line, loses everything after it, and exits 0. A
# green run that skipped the upload is exactly the failure a backup must not
# have. See remote-backfill.sh, which documented this first.
set -euo pipefail

COMPOSE="docker-compose.prod.yml"
cd ~/app

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="/tmp/indiedex-$STAMP.dump"
# The host copy is a plaintext dump of every sighting and observer. It exists
# only between the two containers, and it goes away whichever way this exits.
trap 'rm -f "$DUMP"' EXIT

echo "==> dumping"
# Custom format: restores selectively and in parallel, and its table of
# contents can be verified without restoring anything.
sudo docker compose -f "$COMPOSE" exec -T db \
  pg_dump -U postgres -Fc indiedex < /dev/null > "$DUMP"
echo "    $(du -h "$DUMP" | cut -f1)"

echo "==> verifying the archive"
# Before it is treated as a backup, not after. A truncated or empty dump is
# still a file, and a file is what a naive check would find.
sudo docker compose -f "$COMPOSE" cp "$DUMP" db:/tmp/verify.dump
sudo docker compose -f "$COMPOSE" exec -T db \
  pg_restore --list /tmp/verify.dump < /dev/null > /dev/null
sudo docker compose -f "$COMPOSE" exec -T db rm -f /tmp/verify.dump < /dev/null

echo "==> uploading"
# From the app container, which already holds the S3 credentials -- so nothing
# new has to be distributed, and this works on any box that can deploy.
sudo docker compose -f "$COMPOSE" cp "$DUMP" app:/tmp/backup.dump
sudo docker compose -f "$COMPOSE" exec -T app \
  sh -c 'cd /app/backend && uv run python scripts/backup_db.py /tmp/backup.dump' \
  < /dev/null
sudo docker compose -f "$COMPOSE" exec -T app rm -f /tmp/backup.dump < /dev/null

echo "==> what is stored now"
# Every run reports the whole inventory. The failure worth catching is not a
# backup that errors, it is a backup that quietly stopped happening months ago.
sudo docker compose -f "$COMPOSE" exec -T app \
  sh -c 'cd /app/backend && uv run python scripts/backup_db.py --list' \
  < /dev/null
