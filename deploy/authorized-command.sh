#!/usr/bin/env bash
# The forced command for the CI deploy key. Install on the box and point
# authorized_keys at THIS file instead of `bash -s`:
#
#   command="/home/ubuntu/authorized-command.sh",no-agent-forwarding,\
#   no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding ssh-ed25519 AAAA...
#
# WHY THIS EXISTS
# ---------------
# The key was previously pinned with `command="bash -s"`, and the runbook
# described that as meaning a leaked key "can only run the deploy script, not
# an arbitrary shell". That is not what it does. A forced command replaces the
# command the *client asked for*; it does not constrain the command that runs.
# `bash -s` reads its script from stdin, and stdin over SSH is entirely the
# client's to write. So `echo 'anything' | ssh -i leaked_key box` ran anything,
# as the deploy user, which runs `sudo docker` -- and access to the docker
# daemon is root by construction. A leaked DEPLOY_SSH_KEY was root on the box.
#
# This script is the actual restriction: a fixed set of named actions, each
# mapping to a script already committed in ~/app/deploy. Stdin is drained and
# discarded, never executed. The blast radius of a leaked key becomes "can
# trigger a deploy or a backfill", not "can do anything".
#
# What still governs *content* is branch protection: the scripts this runs come
# from the box's checkout of main, so changing what a deploy does still takes a
# reviewed PR. That is the same trust boundary as before and is unchanged.
#
# One consequence worth knowing: for `deploy`, the script that runs is the
# box's CURRENT copy, before the pull. A change to remote.sh therefore takes
# effect on the deploy AFTER the one that ships it. Everything else here runs
# from a checkout that is already current.
set -euo pipefail

APP="${APP_DIR:-$HOME/app}"
DEPLOY="$APP/deploy"

# Drain stdin rather than closing it. CI still pipes a script in, so that both
# this wrapper and the old `bash -s` pinning work with the same workflow -- the
# workflows can be merged before the box is switched over, and a rollback of
# this file needs no coordinated change. Closing stdin instead would give the
# writer SIGPIPE, which `set -o pipefail` in the workflow turns into a failed
# deploy. The bytes are read and dropped on the floor; nothing here executes
# them.
cat >/dev/null 2>&1 || true

deny() {
    echo "refused: $1" >&2
    echo "this key may only run: deploy | deploy-staging <branch> |" >&2
    echo "  backfill <dry-run|run> [sleep] | find-duplicates [detail] |" >&2
    echo "  seed-models <upload|no-upload>" >&2
    exit 2
}

# Unset means someone opened an interactive session with this key.
[ -n "${SSH_ORIGINAL_COMMAND:-}" ] || deny "no action given"

# Deliberately word-split into a fixed arity. No eval, no quotes to honour:
# every argument below is matched against a literal or a narrow pattern, so
# there is nothing for a shell metacharacter to do.
#
# `set -f` first, and it is load-bearing rather than tidy. Unquoted expansion
# does pathname expansion as well as word splitting, so without it an action of
# `deploy *` would expand against the current directory and arrive as a list of
# filenames -- which is a way to smuggle attacker-chosen strings into the
# arguments checked below.
set -f
# shellcheck disable=SC2206
argv=(${SSH_ORIGINAL_COMMAND})
action="${argv[0]:-}"
a1="${argv[1]:-}"
a2="${argv[2]:-}"
[ "${#argv[@]}" -le 3 ] || deny "too many arguments"

run() { exec /usr/bin/env bash "$@" </dev/null; }

case "$action" in
  deploy)
      [ -z "$a1" ] || deny "deploy takes no arguments"
      run "$DEPLOY/remote.sh"
      ;;

  deploy-staging)
      # A ref name, not a path: rejecting `-` up front stops a leading dash
      # being read as an option by anything downstream, and `..` keeps this
      # from reaching outside the refs namespace.
      case "$a1" in
        ""|-*) deny "deploy-staging needs a branch name" ;;
        *..*|*' '*) deny "bad branch name" ;;
      esac
      printf '%s' "$a1" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$' \
          || deny "bad branch name"
      BRANCH="$a1" run "$DEPLOY/remote-staging.sh"
      ;;

  backfill)
      case "$a1" in
        dry-run) dry=true ;;
        run)     dry=false ;;
        *)       deny "backfill needs 'dry-run' or 'run'" ;;
      esac
      extra=""
      if [ -n "$a2" ]; then
          printf '%s' "$a2" | grep -Eq '^[0-9]+(\.[0-9]+)?$' \
              || deny "sleep must be a number"
          extra="--sleep $a2"
      fi
      DRY_RUN="$dry" EXTRA_ARGS="$extra" run "$DEPLOY/remote-backfill.sh"
      ;;

  find-duplicates)
      case "$a1" in
        "")     detail="" ;;
        detail) detail="--detail" ;;
        *)      deny "find-duplicates takes only 'detail'" ;;
      esac
      DETAIL="$detail" run "$DEPLOY/remote-find-duplicates.sh"
      ;;

  seed-models)
      case "$a1" in
        upload)    upload=true ;;
        no-upload) upload=false ;;
        *)         deny "seed-models needs 'upload' or 'no-upload'" ;;
      esac
      UPLOAD="$upload" run "$DEPLOY/remote-seed-models.sh"
      ;;

  backup-db)
      [ -z "$a1" ] || deny "backup-db takes no arguments"
      run "$DEPLOY/remote-backup-db.sh"
      ;;

  *)
      deny "unknown action '$action'"
      ;;
esac
