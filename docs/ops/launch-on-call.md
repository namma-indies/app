# First two launch weeks

Technical response plan for #187. Assign people/coverage in **#201**, with rota,
exact dates, contacts and escalation channel in private ops. The milestone is
due 30 November 2026; agree actual coverage dates with the launch date.

Proposed targets, **pending #201 agreement**: acknowledge within 15 minutes of
an alert during coverage, escalate to the backup at 30 minutes, 24-hour database
recovery point and four-hour recovery rehearsal. Actions timing is best effort;
it cannot guarantee a detection/acknowledgement target. Agree out-of-hours
coverage explicitly.

At each handover/daily launch check, the primary verifies a recent successful
**Launch monitor** execution (normally under 30 minutes old), delivery, detailed
health, and a fresh backup/nightly timer result. Red checker runs can mean broken
state or notification delivery. Record active incidents, paused backfills,
releases and the last good backup/drill privately. The next primary accepts
ownership. Run database and synthetic media restore/deletion drills before
launch, weekly for the first two weeks, and after relevant changes.

When an alert arrives:

1. Acknowledge privately and own the incident; record start, failing components
   and impact without copying user data to public issues. Escalate if response
   or acknowledgement targets will be missed.
2. Compare app HTML/asset, public API and protected detailed health. App-only
   failure suggests static/release problems; database/storage failures affect
   captures; GPU/queue failures can affect processing while capture still saves.
   Use the private operator diagnostics and last-release record.
3. For GPU loss, check private model/readiness configuration and the established
   restart/rollback procedure. Watch oldest ages/counts **drain** after recovery.
   Do not discard jobs to silence the alert. Review paused-backfill rates as an
   operator workload decision.
4. For elevated errors, reproduce with synthetic data and restricted local logs.
   Aggregates do not contain stack grouping/payloads. Any richer collection is
   a reviewed privacy change; the current window clears as buckets age out.
5. For missing/stale/invalid backups, investigate timer, storage permission and
   capacity, create a fresh backup, then drill it in isolation. Actual recovery
   involves the release operator and product/privacy contact, the private
   restore procedure, and erasure replay before reconnecting traffic.
6. Close the incident when healthy dependencies, queue drainage, app behaviour
   and recovery notification agree. Hand over remaining work, write a private
   incident note, and file sanitized code follow-ups publicly. Alerts never
   authorise an automatic merge or deploy.

Ordinary alerts send on change and remind at the configured interval, not on
each count change. GitHub workflow-failure notifications are the fallback for
broken state/delivery/checker execution; agree/test their recipient too.

Product questions for the launch owner: coverage hours and outage communication,
acceptable recovery targets, retention/deletion wording, and any future
third-party error processor. Assign primary, backup, product/privacy contact and
release operator privately. Mark #201 complete only after people accept the
rota and escalation drill. Keep #187 open for activation and acceptance.
