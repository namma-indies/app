# Moderation operations

Implements the code scope of #191 in #198. The parent remains open for naming
moderators, staffing a rota and agreeing response targets.

## Decisions to confirm with Akash

- People/privacy, sexual-content and illegal-content reports are severe. Before
  any moderator ruling, one severe report holds the entire capture off shared
  surfaces pending review; ordinary reports need two distinct reporters.
- Re-reports after approval (decided 2026-11): once a moderator has approved
  or cleared a capture, new reports do not take it down again unless at least
  `REPORT_REHIDE_MIN_REPORTERS` distinct accounts (default `3`) have reported it
  since that ruling, whatever their reasons. A reporter already ruled against
  counts once, however often they report. Every report still reaches the queue
  (marked "reported again after review"), and severe ones still alert urgently
  with their own clock. A further ruling starts the count again. Reports are
  limited per account (`RL_REPORT_TIMES` per `RL_REPORT_WINDOW_S`, default 30
  per hour).
- Severe reports target a response within 2 hours; ordinary reports within
  24 hours. These are provisional operational targets, not legal deadlines.
- Ordinary alerts are digested every 60 minutes; urgent alerts bypass that
  timer. Email contains machine categories, opaque IDs and a sign-in-required
  queue link, without evidence, images, precise positions or reporter details.
- Every moderator decision requires a reason. Audit rows outlive removal of
  their subject/account, with stable IDs rather than identity snapshots. Decide
  audit retention and who covers escalations before launch.

## Upload screening: new accounts

New accounts do not wait for a moderator (decided 2026-11, "what apps usually
do"): `SCREENING_TRUSTED_AFTER` defaults to `0`, so a brand-new account's clean
upload goes public as soon as screening clears it, like anyone else's. Every
other hold is unchanged and applies to every account: a person filling the
frame, an indoor scene, a detector failure, a never-finished screening and, when
switched on, the local model's second look. To bring the new-account hold back,
set `SCREENING_TRUSTED_AFTER` to N: each account's first N uploads then wait in
the held queue (marked NEW ACCOUNT) until N of them have gone public.
Moderators are always exempt from that one rule.

## Settings Aswin must configure

| Setting | Purpose | Default |
| --- | --- | --- |
| `MODERATOR_ALERT_EMAILS` | Comma-separated moderator recipients, maintained alongside the rota | empty; events wait |
| `EMAIL_SENDER` | Existing mail transport; `ses` enables actual email | `console` |
| `EMAIL_FROM`, `SES_REGION` | Existing verified sender and region | existing mail configuration |
| `PUBLIC_BASE_URL` | Existing public app base, used for the authenticated queue link | existing app configuration |
| `MODERATION_DIGEST_MINUTES` | Ordinary alert batch interval | `60` |
| `MODERATION_RESPONSE_HOURS` | Ordinary report response target | `24` |
| `MODERATION_SEVERE_RESPONSE_HOURS` | Severe report response target | `2` |
| `MODERATION_MAIL_POLL_S` | Idle delivery poll interval | `5` |
| `MODERATION_REMINDER_FRACTION` | Share of a severe-report / urgent-grievance window after which one urgent reminder is sent; a second goes at the deadline | `0.75` |
| `MODERATION_REMINDER_CHECK_S` | How often deadlines are scanned for reminders | `60` |
| `MODERATION_MAIL_MAX_ATTEMPTS` | Failed sends before a message is marked undeliverable instead of retried forever | `8` (about two hours of backoff) |
| `REPORT_REHIDE_MIN_REPORTERS` | Distinct accounts reporting an approved capture since its ruling before it is hidden again | `3` |
| `SCREENING_TRUSTED_AFTER` | Uploads a new account must have gone public before the next skips the moderator; `0` = new accounts never wait | `0` |

Set contacts privately in deployment settings, never in this public repository.
No deployment or production configuration changes are part of this PR.

## Bans

A ban (moderator people screen or the report queue) hides everything the person
logged from every shared surface, ends their sessions and refuses their address
at sign-in, as before. In addition (decided 2026-11):

- **X takedowns.** Any of their photos already posted by the project's X account
  is queued for deletion there, through the same owed-delete records account
  deletion uses (`x_post_purges`, reason `banned`): the delete is tried straight
  away when X credentials are configured and retried by every daily run until X
  confirms. A post that was on its way and never confirmed is kept for a person
  to check in the Social screen's "To take down from X" list, as on erasure.
  Their queued picks never post. Banning again owes nothing twice. Unbanning
  does not repost anything.
- **Moderation stays visible.** Their reported items stay in the report queue,
  marked **BANNED ACCOUNT**, so a moderator can still rule on them; the audit
  trail keeps every action. Public surfaces keep hiding all their content. An
  account being deleted still leaves the queue with its content.

A suspended person's screen links straight to the grievance form with
**Appeal a suspension** chosen (`/grievances?kind=appeal`).

## Shared alert interface for #181 and grievances

```python
from app.moderator_alerts import alert_moderators

async with conn.transaction():
    # Persist the held upload or other work to review here.
    await alert_moderators(
        conn,
        event_key=f"upload_screening:{capture_id}:{screening_revision}",
        category="upload_screening",
        subject_id=capture_id,
        urgent=True,
    )
```

Use a stable event key (at most 200 characters), a machine category (at most
80), and a UUID. The key makes retries idempotent. Choose urgency when the
event is created. Call inside the originating transaction: a rolled-back
screening decision must not leave an alert. No request waits for mail.

Events are retained when no recipients are configured. A background consumer
expands due events into one outbox row per recipient. Each send has a lease,
timeout and bounded retry delay. Successful recipients are recorded separately;
one failed mailbox does not resend mail to everybody else. A crash between
provider acceptance and recording success can duplicate that one message
(at-least-once delivery). Urgent mail takes priority over digest backlog.
Console mode keeps mail pending; a log message never counts as delivery.

Decision drafts hold automatic service-worker reloads until they are submitted
or the view is closed. A failed request keeps its reason visible for retry.

The moderator view reports unconfigured delivery, pending counts, failed
messages retrying and messages given up on. The same signals reach the
protected `/health/detailed` check `moderation_mail` (and so the launch
monitor): unconfigured delivery, the oldest unsent message's age, and recently
undeliverable mail all degrade it. After fixing the cause (for example an
unverified sender), an operator requeues given-up mail privately with
`UPDATE moderation_mail SET failed_at=NULL, attempts=0, available_at=now()
WHERE sent_at IS NULL AND failed_at IS NOT NULL`. Report clocks start at the first unanswered report; editing
or repeating an open report cannot extend its deadline. A ruling closes the
capture's open reports atomically with audit records for affected sightings.
Fresh reports open a new clock. Severe reports sort ahead of ordinary ones.

Escalation: while a severe report episode, or an urgent (non-`other`)
grievance's removal or resolution clock, is still open, the alert consumer
queues one urgent reminder once `MODERATION_REMINDER_FRACTION` of the window
has elapsed and one more when it is overdue (an item first seen overdue gets
only the second). Reminders reuse the alert outbox with stable per-episode
keys, so restarts and several workers never duplicate them. Ordinary reports
and `other` grievances get no reminders. Overdue grievances (including an
unsent acknowledgement past its clock) also degrade the protected
`/health/detailed` check `grievances`, so the launch monitor pages on them.

`GET /moderation/audit` exposes a cursor-paginated action history only to
moderators. The ledger is append-only in the database itself: triggers refuse
UPDATE, DELETE and TRUNCATE for every role, including the table owner and the
`postgres` login the deployment connects as (the `app_rw` REVOKE alone does not
bind those). Only a superuser deliberately dropping or disabling the triggers
can rewrite it. Downgrading past migration 0036 refuses while the ledger holds
rows unless run with `-x drop_moderation_audit=yes` after exporting it;
downgrading 0046 removes the guard but keeps every row. Existing sign-in and deletion mail flows use
their existing sender; moderation adds no mail vendor or credentials.

## Public grievances (#190, code scope #199)

The public `/grievances` page works without an account. It reads the officer's
name, email and other contact details from settings, with honest placeholders
until a person is appointed. Links from sign-in and the app make it reachable;
the service worker leaves it to the server, including on installed PWAs.

Intake accepts a type, link or sighting ID, details and contact email. Request
size and field lengths are bounded. Separate per-IP and per-email budgets
dampen spam without requiring an account. The form's submission key makes a
network/browser retry return the original reference and queue no extra email.
Complaint details/contact information are moderator-only and are never copied
into acknowledgement or moderator-alert emails. Linked sighting frames can be
opened at full size from the moderator queue; URLs are signed together in one
batch. No complaint status lookup is exposed to anonymous callers.

Each accepted complaint gets a random reference and a durable acknowledgement
email. The public receipt says that email is queued, not already delivered.
The acknowledgement clock stops only when the real sender accepts the email;
console mode does not drain the outbox. Closed complaints with an unsent
acknowledgement stay in the active queue. Failed delivery and overdue clocks
remain visible.

### Deadline defaults — all require lawyer confirmation

| Complaint | Acknowledge | Resolve | Removal/action review |
| --- | --- | --- | --- |
| Takedown | 24 hours | 7 days | 36 hours |
| Privacy | 24 hours | 7 days | 36 hours |
| Non-consensual intimate imagery | 24 hours | 7 days | 2 hours |
| Government or court order | 24 hours | 7 days | 3 hours |
| Other | 24 hours | 7 days | — |

These are provisional tracking targets from the task, not a legal conclusion.
Every deadline and its applicability is labelled for lawyer confirmation on the
public page and moderator queue. In particular, applying the 36-hour clock to
all privacy requests is a conservative provisional classification. A lawyer
must confirm the scope, start events, exceptions and what counts as completion.
Deadlines are snapshotted on intake; configuration changes do not rewrite them.

Intimate-imagery and government/order cases sort first. All categories except
`other` alert immediately; `other` uses the ordinary digest. Filing an
unauthenticated complaint does not itself hide content: a moderator verifies
the concern/order and acts, preventing a claimed order from being a public
take-down switch. Hiding linked content affects the shared capture and records
both content and complaint actions in one transaction. It stops the removal
clock but leaves resolution open. Resolving or dismissing requires a reason;
closed-without-removal is shown explicitly. Moderators reply using the contact
email; marking resolved records the decision, and does not send a resolution
email automatically. Reference lookup includes closed complaints.

### Grievance settings Aswin must fill in

| Setting | Purpose | Default |
| --- | --- | --- |
| `GRIEVANCE_OFFICER_NAME` | The appointed person's published name | empty placeholder |
| `GRIEVANCE_OFFICER_EMAIL` | Published officer email | empty placeholder |
| `GRIEVANCE_OFFICER_CONTACT` | Other published contact instructions/details | empty placeholder |
| `GRIEVANCE_ACKNOWLEDGE_HOURS` | All acknowledgement clocks | `24` |
| `GRIEVANCE_RESOLVE_HOURS` | All resolution clocks | `168` |
| `GRIEVANCE_REMOVAL_HOURS` | Takedown/privacy removal review | `36` |
| `GRIEVANCE_INTIMATE_HOURS` | Intimate-imagery urgent review | `2` |
| `GRIEVANCE_GOVERNMENT_HOURS` | Government/court-order urgent review | `3` |

The moderator-recipient, real sender and public-base settings above are also
required. Optional intake controls are `RL_GRIEVANCE_IP_TIMES` (20),
`RL_GRIEVANCE_EMAIL_TIMES` (5) and `RL_GRIEVANCE_WINDOW_S` (3600). These are
per-window abuse budgets, not people/complaint quotas. Both are charged before
and outside the intake transaction, so invalid, conflicting or rolled-back
attempts still count; an identical retry of an already-saved submission returns
its receipt without spending the address budget.

### Optional bot check (Cloudflare Turnstile)

Off by default. With `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY` both empty
the form is exactly as described above. Startup refuses one without the other.
To switch it on:

1. In the Cloudflare dashboard, Turnstile → Add widget: name it, add the public
   hostname that serves `/grievances` (and any staging host), mode "Managed".
2. Set the site key and secret key privately in deployment settings on every
   API worker. Never commit either; the site key is rendered into the page, the
   secret is not.
3. Deploy and load `/grievances`: the check appears above "Send complaint".

When on, the page loads Turnstile's script and frame from
`https://challenges.cloudflare.com` (its CSP allows exactly that origin for
`script-src` and `frame-src`), and the token is verified server-side against
siteverify before anything is saved, with `TURNSTILE_TIMEOUT_S` (default 5 s).
It fails closed: a missing or failed check returns the filled-in form asking
the person to complete the check; an unreachable or erroring siteverify (or a
rejected secret, logged at error level) returns it with a "try again" message.
Nothing is saved and no email is charged or queued in either case. The token
and secret are never logged. An identical browser retry of an already-saved
complaint still gets its receipt without a second check. Cloudflare's test keys
(`1x00000000000000000000AA` / `1x0000000000000000000000000000000AA`, always pass)
are fine for staging. Turnstile sends the visitor's browser signals to
Cloudflare: mention it in the privacy policy before switching it on.

### Product and people decisions for Akash

- Confirm severe-report categories, one-report holding before a ruling, the
  three-reporter re-hide after one, response targets and the 60-minute digest.
- Confirm the grievance category-to-clock mapping with a lawyer, including
  whether every privacy complaint needs the 36-hour clock.
- Name the officer, rota, escalation contacts and coverage for 2/3-hour cases.
- Decide complaint/audit/mail retention, access after a moderator leaves, and
  the privacy-policy wording for retaining anonymous grievance contact data.
- Confirm the manual final-reply workflow, or commission a reply composer if
  automatic resolution emails are wanted. The current automatic email is the
  acknowledgement requested in this task.

Complaints retain an opaque sighting reference after photo erasure and can also
refer to content already gone at intake. This link deliberately has no foreign
key: erasure must not be held up by a complaint, or introduce a reverse lock
order with moderator actions. Photo erasure removes the media/rows; a grievance
does not preserve a copy of that evidence. Legal retention/hold requirements
remain a question for counsel.

Parents #190 and #191 remain open for those people, legal and operations tasks.
