#!/usr/bin/env bash
# What actually happens on the staging box during a deploy. Fed over SSH via
# `ssh "$HOST" "BRANCH=$BRANCH bash -s" < deploy/remote-staging.sh` -- see
# deploy-staging.sh (manual runs) and .github/workflows/deploy-staging.yml
# (CI runs: every merge to main, plus whatever branch you dispatch).
#
# Deliberately separate from deploy/remote.sh (prod): prod only ever
# fast-forwards `main` -- a hard reset there would be a bug that could nuke
# unpushed history on the box. Staging deploys arbitrary feature branches
# that aren't ancestors of each other, so a plain `git pull --ff-only` would
# fail every time you switch branches. `checkout` + `reset --hard` is correct
# here because the staging box never has local commits worth preserving.
#
# The same is true of their migrations, which is why the database is walked
# back before the switch (below). Prod never does this: its chain only grows.
#
# Every docker call reads from /dev/null. This script arrives on bash's stdin,
# and `compose exec`/`run` forward stdin to the container -- left attached,
# they would swallow the rest of the script.
set -euo pipefail

BRANCH="${BRANCH:?set BRANCH to the ref to deploy}"
COMPOSE="docker-compose.prod.yml"
VERSIONS="backend/migrations/versions"

cd ~/app
git fetch -q origin "$BRANCH"

# `revision` or `down_revision` from a migration on stdin, in either spelling
# alembic has generated here (`x = "..."`, `x: str = "..."`). Empty for None.
field() { sed -nE "s/^$1(:[^=]*)?[[:space:]]*=[[:space:]]*[\"']([^\"']*)[\"'].*/\2/p" | head -1; }

# The migration file in the current checkout that defines revision $1.
file_for() {
    for f in "$VERSIONS"/*.py; do
        if [ "$(field revision < "$f")" = "$1" ]; then echo "$f"; return 0; fi
    done
    true
}

# Switching branches can leave the database stamped with a revision the new
# code has never heard of. alembic then refuses to start and the app
# restart-loops (2026-10-25: main against feat/match-calibration's 0017). So
# walk the database back to the newest revision $BRANCH also has, using the
# downgrade()s of the code still checked out -- $BRANCH doesn't have them --
# and let the new entrypoint migrate forward from there. Staging data that
# lives only in the departing branch's tables goes with them; the dump taken
# first is the way back.
target_revs="$(git ls-tree --name-only "origin/$BRANCH" "$VERSIONS/" | grep '\.py$' \
    | while read -r f; do git show "origin/$BRANCH:$f" | field revision; done)"
current="$(sudo docker compose -f "$COMPOSE" exec -T db \
    psql -U postgres -d indiedex -Atc 'SELECT version_num FROM alembic_version' \
    </dev/null 2>/dev/null | tr -d '[:space:]' || true)"

keep="$current"
while [ -n "$keep" ] && ! grep -qxF "$keep" <<<"$target_revs"; do
    file="$(file_for "$keep")"
    if [ -z "$file" ]; then
        echo "!! database is at $keep, which the checked-out code doesn't know either -- roll it back by hand" >&2
        exit 1
    fi
    if grep -qE '^down_revision[^=]*=[[:space:]]*\(' "$file"; then
        echo "!! $keep sits behind a merge migration -- roll it back by hand" >&2
        exit 1
    fi
    keep="$(field down_revision < "$file")"
done
if [ -n "$current" ] && [ -z "$keep" ]; then
    echo "!! $BRANCH shares no migration with the database at $current -- not downgrading to an empty schema" >&2
    exit 1
fi

if [ "$keep" != "$current" ]; then
    mkdir -p ~/staging-db-backups
    backup=~/staging-db-backups/"$(date -u +%Y%m%dT%H%M%SZ)-$current.dump"
    echo "==> database is at $current, which $BRANCH doesn't have -- backing up to $backup"
    # shellcheck disable=SC2024  # the dump belongs to the deploy user, not root
    sudo docker compose -f "$COMPOSE" exec -T db pg_dump -U postgres -Fc indiedex </dev/null > "$backup"
    echo "==> rolling back to $keep, the newest revision both branches share"
    sudo docker compose -f "$COMPOSE" run --rm -T --no-deps -w /app/backend \
        --entrypoint uv app run alembic downgrade "$keep" </dev/null
fi

git checkout -q "$BRANCH"
git reset --hard "origin/$BRANCH"
echo "==> now at $BRANCH ($(git rev-parse --short HEAD))"
sudo docker compose -f "$COMPOSE" up -d --build </dev/null
sudo docker image prune -f >/dev/null </dev/null
