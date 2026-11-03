# Switch on launch monitoring

Code scope: #200 under #187. People/coverage: #201. Keep #187 open until
activation, a real restore rehearsal, media protection and coverage are accepted.
Record real origins, hosts, IPs, bucket names, contacts and dates in the **private
ops repository**. Credentials belong in the secret store / GitHub environment
secrets; record their locations and owners there, not their values.

## Decisions and limits

Use the external **Launch monitor** GitHub Actions workflow every ten minutes,
offset from the hour: it can detect loss of the entire app host and reuses
existing tooling. An on-box timer would disappear with that host. Actions cron
is best effort: [runs can be delayed/dropped, run only on the default branch,
and public-repository schedules can stop after inactivity](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
This is a launch safety net, not an uptime SLA. The on-call daily check includes
checking that the checker itself ran.

Alerts use an HTTPS webhook accepting `{"text":"..."}`. An existing private
webhook-to-email bridge can deliver email. Payloads contain fixed component
names/statuses only. Agree the destination and recipients in #201.

For errors, choose the implemented self-hosted aggregates and restricted local
logs. They have a small operational footprint, but lack stack grouping and
replay. A full self-hosted tracker such as [GlitchTip](https://glitchtip.com/documentation/)
adds a service to maintain and back up. Third-party Sentry/hosted GlitchTip would
provide managed tracking but require an SDK, outbound processing, and a privacy
policy / processor review (#183). **No third-party error SDK, DSN or collector
is implemented or enabled.** Adding one needs a separate reviewed change.

Signed-in web/PWA errors send only one of two fixed categories in the event JSON.
The JSON contains no message, stack, URL, user agent, account ID, photo,
transcript or form value. Requests use the normal authentication cookie/HTTP
metadata, which the counter does not retain; the page referrer is suppressed.
Reporting is limited to once per minute per page, skips offline
events and never queues/retries. The API allows ten submissions per observer
per fifteen minutes. Errors before sign-in/entrypoint load, and native errors,
need other diagnostics.

Server counters cover 5xx, unhandled errors, accepted GPU failure reports and
`app.*` ERROR logs or warnings carrying an exception. A logged 500 can count twice:
these are signals, not unique exceptions. Minute aggregates persist every
15 seconds for 14 days; a hard exit can lose the last buffer. The error window
is approximately 15 minutes. Existing raw logs stay restricted to the host;
do not paste them into public issues. App Docker logs rotate at 3 × 10 MB,
a size bound rather than a time guarantee; replacement can discard them.

## Checks

- Public `GET /health`: only `{"status":"ok"}`, without dependency I/O.
- `GET /health/detailed`: `Authorization: Bearer <health-token>` required;
  unset token gives 404, wrong token 401, query/cookie tokens do not work.
  Responses use `Cache-Control: no-store`; a broken dependency gives 503.
- Database access includes pool acquisition, queue queries, capability ages
  and error counts. Independent S3 probes run concurrently. Every probe has
  its own timeout; unrelated results remain available when one fails.
- Local ONNX presence is checked when legacy CPU re-ID or CPU fallback is in
  use. Model names/presence are private. The maintenance scripts use
  `uv run python -m app.model_health` locally; seeding also checks local liveness.
- Storage writes/reads the constant two-byte `monitoring/storage-probe`.
  Grant the API ListBucket and Get/Put on that key, plus List/Get on `backups/`
  for freshness. No delete permission is required. The existing noncurrent
  lifecycle rule bounds probe versions. The latest backup check reads its
  header/age, never exposes its name, and does not claim to prove a restore.
- A model-loaded GPU worker sends an outbound authenticated readiness heartbeat
  once per minute through the existing worker connection. It advertises re-ID,
  multi-animal, transcription and suggestion capabilities. Only the serial claim
  loop reports; a hung native job cannot stay healthy merely by renewing a
  lease. Missing capabilities are unhealthy even with empty queues.
- Re-ID, transcription, suggestion and description ages include pending and
  running jobs from creation, including retries/expired leases. Empty age is
  null. Description/transcript backfills have separate summaries and thresholds.
  Deliberately disabled features or zero-rate backfills show `paused` with
  counts/ages still visible. Re-ID monitoring covers durable media jobs, not the
  legacy unqueued CPU background path.
- The external checker fetches app HTML plus its first-party JS entry asset,
  public API liveness and detailed health. Redirects/malformed responses fail;
  the health token goes only to the detailed endpoint. This static availability
  probe does not execute a browser capture/login journey.

## API and app settings

Release the operational-health migration before enabling these. All actual
configuration changes are Aswin's private deployment procedure.

| Setting | Default | Purpose |
|---|---|---|
| `HEALTH_CHECK_TOKEN` | empty/off | Unique 32-byte random secret shared with the checker; API restart |
| `HEALTH_CHECK_TIMEOUT_S` | `3` | Entire per-probe budget including pool wait, 0.1–15 seconds |
| `HEALTH_WORKER_STALE_S` | `600` | Worker capability heartbeat age |
| `HEALTH_QUEUE_STALE_S` | `1800` | Oldest live job; **starting value to tune against actual durations** |
| `HEALTH_BACKFILL_STALE_S` | `86400` | Enabled backfill age; tune to batch size/rate |
| `HEALTH_BACKUP_STALE_S` | `129600` | 36-hour backup freshness |
| `OPS_ERROR_MONITORING_ENABLED` | `false` | Server counts and authenticated client-event intake; API restart |
| `OPS_ERROR_ALERT_THRESHOLD` | `5` | Signals in approximately 15 minutes |
| `VITE_SELF_HOSTED_ERRORS_ENABLED` | `false` | Web/PWA reporting; frontend rebuild, passed by Compose |

Review existing feature flags privately: `MEDIA_JOBS_ENABLED`,
`MULTI_ANIMAL_ENABLED`, `TRANSCRIPTION_ENABLED`, `SUGGESTIONS_ENABLED`,
`SUGGEST_VIA=worker`, `DESCRIBE_ENABLED`, `DESCRIBE_BACKFILL_PER_HOUR`,
`TRANSCRIPT_BACKFILL_PER_HOUR`. They decide expected capabilities and intentional
pauses. `MEDIA_CPU_FALLBACK_ENABLED` decides whether local CPU models are needed
on the durable path. Do not enable a product feature just to make health green.

Roll the GPU worker forward to this code after the API release using the private
release procedure. Its existing `MEDIA_GPU_TRANSCRIBE`, `MEDIA_GPU_SUGGEST`,
`MEDIA_GPU_MULTI_ANIMAL`, model pins and shared `MEDIA_GPU_TOKEN` / token file
must match. The separate health token never goes to the worker or browser.

## Checker settings and durable state

Create GitHub environment `launch-monitoring`, restricted to the default branch.
Scheduled runs must not require interactive approval every ten minutes. Protect
workflow edits through normal review. Put these secrets in that environment:

| Secret | Value |
|---|---|
| `MONITOR_APP_URL` | HTTPS app origin, no path/query: `<app-origin>` |
| `MONITOR_API_URL` | HTTPS API origin; may equal app origin |
| `HEALTH_CHECK_TOKEN` | Same dedicated secret as the API |
| `MONITOR_WEBHOOK_URL` | Private HTTPS destination including its credential |
| `MONITOR_STATE_BUCKET` | `<monitor-state-bucket>`, private/encrypted |
| `MONITOR_STATE_ACCESS_KEY_ID` | Dedicated state identity |
| `MONITOR_STATE_SECRET_ACCESS_KEY` | Its secret; no media/backup access |
| `MONITOR_STATE_ENDPOINT` | Optional custom HTTPS S3 origin; empty uses regional AWS endpoint |

Environment/repository variables: `MONITOR_STATE_REGION` (default `ap-south-1`)
and `MONITOR_REMINDER_S` (default `21600`, minimum `600`). The enable switch
`LAUNCH_MONITORING_ENABLED=true` must be a **repository variable**, because the
job condition is evaluated before environment-only variables are available.

The state identity needs only GetObject/PutObject on
`arn:aws:s3:::<monitor-state-bucket>/monitoring/launch-state.json`, plus the
specific key's encrypt/decrypt/data-key permissions if customer KMS is used.
Block public access, enable encryption, and do not expire current state. There
is no Actions cache/artifact holding state. Coordinate health-token rotation on
API and checker; document credential/destination rotation privately.

**Seed state once**, with the private bucket exported in the operator environment.
This avoids granting ListBucket merely to distinguish a missing key from denial:

```sh
state_file="$(mktemp)"
printf '%s\n' '{"version":1,"checks":{},"last_notified":0}' > "$state_file"
aws s3api put-object --bucket "$MONITOR_STATE_BUCKET" \
  --key monitoring/launch-state.json --body "$state_file" \
  --content-type application/json > /dev/null
rm -f "$state_file"
```

State contains fixed codes and notification times. Send on first incident,
changed failing components, recovery, and six-hour reminders while unhealthy;
first healthy startup is quiet. Increasing ages/counts do not create new alerts.
Delivery failures do not advance notification state. A failed state read/write
preflight aborts the workflow rather than repeatedly sending untracked alerts.
Delivery is at least once: successful delivery followed by a failed final save
can duplicate a notification on retry. Keep GitHub workflow-failure notifications
enabled as the fallback for broken state/delivery/checker execution.

## Aswin's activation checklist

1. Add actual origins, worker location/release steps, media/state bucket names,
   regions, IAM/KMS policies, secret-store references and owners to private ops
   `OPERATIONS.md`. Add the destination and its acknowledgement test there.
2. Release API/schema and worker code through the private procedure. Set the
   API token, probe permissions, expected flags and thresholds; enable server
   counts and rebuild with the browser flag if wanted. Confirm readiness arrives.
3. Enable/verify the nightly backup and create a fresh one using
   [BACKUPS.md](../../deploy/BACKUPS.md). Configure [media protection](media-backups.md)
   and [deletion/erasure-ledger](media-deletion.md) settings and retention.
4. Create checker environment/secrets/variables and seed private state. Verify
   correct-token detailed health, minimal public health and rejected bad tokens.
   Enable the repository switch; dispatch **Launch monitor** with `test_alert=true`,
   then normally. Verify receipt and the next scheduled execution.
5. On staging, simulate worker loss or a safe synthetic stale job. Verify one
   incident across repeated runs, reminders with a temporarily shorter interval,
   and recovery. Verify a signed-in synthetic browser error increments only a
   count. Restore intended thresholds/interval and record safe evidence privately.
6. Configure the separate restore environment/read-only identity, approve runner
   processing, and run **Database restore drill** against the latest backup.
   CI's synthetic drill is not evidence that a deployment backup restored. No
   agent has run that workflow against production.
7. Complete [the on-call plan](launch-on-call.md) in #201. Agree coverage,
   recovery targets, retention and privacy wording (#183). Check recent monitor
   execution and backup freshness each launch day. Keep #187 open until accepted.
