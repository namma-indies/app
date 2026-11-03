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

The moderator view reports unconfigured delivery, pending counts and failed
messages retrying. Report clocks start at the first unanswered report; editing
or repeating an open report cannot extend its deadline. A ruling closes the
capture's open reports atomically with audit records for affected sightings.
Fresh reports open a new clock. Severe reports sort ahead of ordinary ones.

`GET /moderation/audit` exposes a cursor-paginated action history only to
moderators. The application role has insert/select access to the ledger and
cannot update or delete entries. Existing sign-in and deletion mail flows use
their existing sender; moderation adds no mail vendor or credentials.
