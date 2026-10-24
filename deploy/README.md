# Deploy mechanics

Two kinds of key reach the boxes, and they are not interchangeable.

**An operator's own key** is unrestricted. `deploy/deploy.sh` and
`deploy/deploy-staging.sh` use it, and they pipe a script to a plain `bash -s`
because that key can already run anything.

**The CI deploy key** is restricted by a forced command in the box's
`authorized_keys`, and the workflows in `.github/workflows` use it.

## Why the forced command changed

The CI key used to be pinned with `command="bash -s"`, described as limiting a
leaked key to "the deploy script, not an arbitrary shell". That is not what a
forced command does. It replaces the command the *client asked for*; it places
no constraint on the command that runs. `bash -s` reads its script from stdin,
and stdin over SSH is entirely the client's to write:

```sh
echo 'whoami; cat ~/app/.env' | ssh -i leaked_key box    # ran, as the deploy user
```

The deploy user runs `sudo docker`, and access to the docker daemon is root by
construction. So a leaked `DEPLOY_SSH_KEY` was root on the box, holding every
secret in `.env` including the S3 credentials and the database password.

`authorized-command.sh` is the actual restriction: a fixed set of named actions,
each mapping to a script already committed under `deploy/`. Stdin is drained and
discarded, never executed.

What still governs *content* is branch protection. The scripts this runs come
from the box's checkout of `main`, so changing what a deploy does still takes a
reviewed pull request. That trust boundary is unchanged; only the "any command
at all" path is closed.

## Actions

| Action | Runs |
|---|---|
| `deploy` | `remote.sh` |
| `deploy-staging <branch>` | `remote-staging.sh` |
| `backfill <dry-run\|run> [sleep]` | `remote-backfill.sh` |
| `rescore <dry-run\|run\|histogram> [sleep]` | `remote-rescore.sh` |
| `find-duplicates [detail]` | `remote-find-duplicates.sh` |
| `seed-models <upload\|no-upload>` | `remote-seed-models.sh` |
| `backup-db` | `remote-backup-db.sh` |
| `merge-identical <dry-run\|apply>` | `remote-merge-identical.sh` |

Words rather than flags, so the grammar is matched against literals instead of
parsed. `backend/tests/test_authorized_command.py` runs the real script and
pins both the allowlist and the refusals.

## Installing it

**Order matters.** Merge the workflow change first, install this second.

The workflows send the action name *and* still pipe the script on stdin, so they
work under either pinning: with `bash -s` the action name is discarded and stdin
runs, and with this wrapper the action is honoured and stdin is dropped. That
makes the two steps independent and either one reversible on its own. Installing
the wrapper before the workflows know to send an action would refuse every
deploy.

On each box, as the deploy user:

```sh
install -m 0755 ~/app/deploy/authorized-command.sh ~/authorized-command.sh
```

Then edit `~/.ssh/authorized_keys` and replace the CI key's `command="bash -s"`
with the wrapper plus the restrictions a deploy never needs:

```
command="/home/<user>/authorized-command.sh",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding ssh-ed25519 AAAA... ci-deploy
```

Leave the operator key's line alone.

Verify before trusting it, from a machine holding the CI private key:

```sh
echo id | ssh -i ci_key <user>@<box> 'bash -s'   # must be refused
ssh -i ci_key <user>@<box> 'deploy'              # must deploy
```

The copy at `~/authorized-command.sh` is deliberate rather than pointing
`authorized_keys` straight into `~/app/deploy/`. A forced command that lives
inside the checkout it is meant to constrain can be replaced by anything that
can write to that checkout, which includes the deploy itself.

Re-run the `install` line after any change to this file. It is the one script
here that a deploy does not update, by design.

## The daily @nammaindies post

`social-post.timer` runs `remote-social-post.sh` at 09:00 IST, which runs
`backend/scripts/post_daily.py` in the app container. Design:
`docs/specs/2026-10-24-x-daily-post-design.md`.

It needs the four X values in the box `.env` (`X_API_KEY`, `X_API_SECRET`,
`X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`) and posts only when
`SOCIAL_POSTING_ENABLED=true`. Without that it is a dry run that logs what it
would have posted. Install once, the same way as the backup timer:

```sh
sudo cp ~/app/deploy/social-post.service ~/app/deploy/social-post.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now social-post.timer
systemctl list-timers social-post.timer      # next run should be 03:30 UTC
```

Run it by hand, and read what it did:

```sh
sudo systemctl start social-post.service
journalctl -u social-post.service -n 20 --no-pager
```

The unit is marked **failed** in two cases, both of which need a person:
a post that X refused (it is retried tomorrow; the error shows on the POSTS
screen), and a post that was started but never confirmed (`posting`). The
second blocks every later post until someone has checked @nammaindies on X
and resolved the row, because retrying could post the same dog twice:

```sql
-- the post is on X: record it
UPDATE social_picks SET status='posted', x_post_id='<id from the post URL>',
  posted_at=now(), posted_on=current_date WHERE id='<pick id>';
-- the post is not on X: put it back in the queue
UPDATE social_picks SET status='queued' WHERE id='<pick id>';
```
