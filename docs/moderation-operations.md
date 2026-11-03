# Moderation operations

Implements the code scope of #191 in #198. The parent remains open for naming
moderators, staffing a rota and agreeing response targets.

## Decisions to confirm with Akash

- People/privacy, sexual-content and illegal-content reports are severe. One
  severe report holds the entire capture off shared surfaces pending review,
  including a fresh concern following an earlier approval. Ordinary reports
  retain the two-distinct-reporter threshold and cannot overturn a ruling.
- Severe reports target a response within 2 hours; ordinary reports within
  24 hours. These are provisional operational targets, not legal deadlines.
- Ordinary alerts are digested every 60 minutes; urgent alerts bypass that
  timer. Email contains machine categories, opaque IDs and a sign-in-required
  queue link, without evidence, images, precise positions or reporter details.
- Every moderator decision requires a reason. Audit rows outlive removal of
  their subject/account, with stable IDs rather than identity snapshots. Decide
  audit retention and who covers escalations before launch.

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

Set contacts privately in deployment settings, never in this public repository.
No deployment or production configuration changes are part of this PR.

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

The moderator view reports unconfigured delivery, pending counts and failed
messages retrying. Report clocks start at the first unanswered report; editing
or repeating an open report cannot extend its deadline. A ruling closes the
capture's open reports atomically with audit records for affected sightings.
Fresh reports open a new clock. Severe reports sort ahead of ordinary ones.

`GET /moderation/audit` exposes a cursor-paginated action history only to
moderators. The application role has insert/select access to the ledger and
cannot update or delete entries. Existing sign-in and deletion mail flows use
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
per-window abuse budgets, not people/complaint quotas.

### Product and people decisions for Akash

- Confirm severe-report categories, one-report holding (including after a
  previous approval), response targets and the 60-minute ordinary digest.
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
