# Switching on open sign-up

`EMAIL_OPEN_SIGNUP` remains **false by default**. Deploying auth hardening does
not open the allowlist. Public launch also needs provider capacity, upload
budgets, abuse visibility and people assigned to moderation (#184).

## Auth controls (#60)

All endpoint budgets live in Postgres, including existing stats, place-search,
voice-note, blocking and account-deletion email limits. Multiple workers share
one budget; deploying does not reset it. Database failure refuses the request.
`RATE_LIMIT_ENABLED=true` is required for launch.

The passcode door requires first-party SHA-256 proof-of-work on **every** join.
There is no captcha vendor, third-party script or transfer of visitor data.
The work is signed, bound to the client IP, expires after five minutes, and can
be used once across all workers. This changes the passcode protocol: older
native bundles must update to use that door; email sign-in remains available.

Defaults (environment variables use the uppercased setting names):

| Setting | Default | Meaning |
| --- | --- | --- |
| `AUTH_EMAIL_COOLDOWN_S` | 60 | Minimum gap between requests for one normalized address, across source IPs |
| `AUTH_BACKOFF_AFTER` | 3 | Failure that first triggers a lockout |
| `AUTH_BACKOFF_BASE_S` | 5 | First lockout; subsequent failures double it |
| `AUTH_BACKOFF_MAX_S` | 900 | Maximum lockout |
| `AUTH_FAILURE_WINDOW_S` | 3600 | Inactivity interval that resets failure history |
| `JOIN_POW_BITS` | 16 | Work difficulty; validate on the oldest supported phone before changing |
| `JOIN_POW_TTL_S` | 300 | Proof lifetime |
| `RL_JOIN_CHALLENGE_TIMES` | 60 | Challenge requests per IP per minute |

Existing `RL_EMAIL_ADDR_*`, `RL_EMAIL_IP_*` and `RL_JOIN_*` budgets remain.
The address cooldown is separate from the address window. Failed passcodes,
invalid proofs and invalid sign-in tokens trigger backoff. A blocked request
does not extend its lockout. Replies include a readable wait and `Retry-After`.
An IP is a shared resource on carrier NAT, so the default lockout is bounded,
never permanent.

Limiter keys are domain-separated HMACs using `PHONE_HASH_SECRET`; raw email
addresses and IPs are not written to limiter tables. Expired rows are swept in
bounded batches during requests and every minute while the service is idle.
Keep the key stable across workers and deployments.

## Development-secret guard (#42)

[PR #42](https://github.com/namma-indies/app/pull/42) is a separate launch
dependency. Limits and proof-of-work cannot protect a session signed with the
published development key: that key can forge a session for any account.
Before enabling open sign-up, land that guard or equivalent startup enforcement
and independently confirm non-default `SESSION_SECRET`, `MAGIC_LINK_SECRET`,
`PHONE_HASH_SECRET` and `JOIN_PASSCODE`. This work does not take ownership of #42.

## Upload budgets (#202, code scope of #184)

`/sighting` and `/capture` share account-keyed Postgres budgets. Defaults are
`RL_UPLOAD_HOUR_TIMES=60` and `RL_UPLOAD_DAY_TIMES=200`, in fixed UTC windows.
They count requests, including retries and malformed forms. Both reservations
commit together; a denial does not spend the other window. Admission happens
before multipart parsing and does not hold a connection during media work.

A 429 carries `Retry-After`. The app keeps the capture's bytes and client token
in IndexedDB, persists the retry time, pauses the rest of the queue and resumes
when the wait ends. Closing the app does not discard either the capture or its
wait. The app says when sync can resume. These initial budgets need validation
against long field walks; changing them is configuration, not a release.

## Email capacity

The provider in `app/email/sender.py` is **Amazon SES v2**, using the ambient AWS
credential chain and `SES_REGION`. There is no new email or captcha vendor.

SES has a regional per-second rate and a rolling 24-hour recipient quota. Its
sandbox defaults are 1 recipient/second and 200/24h, and only verified recipients
are eligible there. Production quotas are account-specific. Historical comments
about an approval or a quota are not evidence of today's configuration.
See [SES quotas](https://docs.aws.amazon.com/ses/latest/dg/manage-sending-quotas.html)
and [GetAccount](https://docs.aws.amazon.com/ses/latest/APIReference-V2/API_GetAccount.html).

The app uses **bounded retries, not a token queue**. A shared Postgres slot
paces every SES attempt; a rolling ledger reserves daily capacity, including
failed/ambiguous attempts. A process dying after SES accepts therefore cannot
recover that reservation and send past the budget. Tokens remain hashed in
`login_tokens`; no raw token, address or link is saved in the capacity ledger.
Account-deletion mail uses the same capacity.

The public deletion-link form reserves capacity before account lookup, including
requests for unknown addresses, so a shared pause cannot reveal membership.
Those reservations count conservatively against the app budget even when no
mail is sent. A known capacity pause returns an HTML wait with `Retry-After` for
every address. An admitted request gets a neutral inbox/retry message; it never
claims a message was sent, and a recipient-specific failure cannot reveal who
has an account.

Transient provider failures back off across workers/restarts. A request retries
at most three times, with short waits only. A daily-quota refusal immediately
sets a shared one-hour pause. Permanent refusals do not retry. When admission
fails, the address cooldown rolls back, so it cannot contradict the displayed
wait. A failed send returns readable retry guidance. SDK retries are disabled
so every actual attempt is accounted for. No provider error text, recipient,
token or link goes into error logs.

| Setting | Default | Operator action |
| --- | --- | --- |
| `EMAIL_DAILY_BUDGET` | 200 | Set below the confirmed regional quota, leaving headroom for other senders |
| `EMAIL_SEND_INTERVAL_S` | 1 | Set at least `1 / usable recipients-per-second`, with headroom |
| `EMAIL_SEND_ATTEMPTS` | 3 | Maximum attempts per request; at most 5 |
| `EMAIL_BACKOFF_MAX_S` | 60 | Cap on shared transient backoff |
| `EMAIL_QUOTA_BACKOFF_S` | 3600 | Pause after SES reports a daily quota refusal |
| `SES_CONFIGURATION_SET` | empty | Set privately if using an SES event destination for delivery/bounce/complaint monitoring |

### What Aswin must confirm with SES

Run this personally in the intended region; do not paste its account-specific
output into public issues or PRs:

```sh
aws sesv2 get-account --region "$SES_REGION" \
  --query '{ProductionAccessEnabled:ProductionAccessEnabled,SendingEnabled:SendingEnabled,EnforcementStatus:EnforcementStatus,SendQuota:SendQuota}'
```

Confirm all of these:

- Production access is enabled, sending is enabled and the account is healthy.
- `Max24HourSend`, `MaxSendRate`, `SentLast24Hours`, and capacity used by **other**
  applications in this region leave enough room for the expected sign-in peak.
  Request a quota increase before opening if they do not.
- The configured `EMAIL_FROM` identity is verified in that same region; DKIM,
  SPF/custom MAIL FROM and DMARC are correct. Verify delivery with a controlled
  external inbox as well as the pilot inboxes.
- Runtime IAM permits `ses:SendEmail`, including any configured event set.
- Bounce/complaint/suppression monitoring has an owner and alert destination;
  `SendEmail` acceptance is not proof of inbox delivery. Confirm the SES
  configuration set/event destination and suppression policy, if used.

This worker does not query the production account or change provider settings.

## Signals for moderators and ops

The Admin → Numbers screen and `GET /moderation/abuse` show hourly account
creations, failed sign-ins (including throttled attempts), accepted uploads,
upload/email throttles and email failures. A non-moderator gets 404. Signals
start at this migration; earlier hours are not fabricated. The counter rows
contain only hour, event and count, are shared across workers and are pruned
after seven days. Accepted uploads count captures once, even if they publish
several animals; account creation includes addresses awaiting email verification.

The private upload-spike list identifies accounts already visible in the
moderator people screen. It flags at least `ABUSE_UPLOAD_SPIKE_HOUR=20` accepted
uploads in the last hour and at least twice the preceding hour. Defaults also
flag `ABUSE_SIGNUP_WARN_HOUR=20` account creations or
`ABUSE_SIGNIN_WARN_HOUR=50` failed sign-ins in an hour. These are prompts for a
human to inspect, never automatic bans or takedowns. Tune against actual traffic.

Ops can read the same snapshot without making an API session:

```sh
uv run python scripts/abuse_report.py --hours 24
```

The command opens a read-only transaction on the configured app database. Its
output is private operational data; keep it out of this public repository.
Assign someone to inspect signals and provider feedback during launch hours.

## Production switch checklist

Before changing the switch, Aswin must confirm/set these in private production
configuration, consistently across workers, and deploy the migrations:

1. Non-default, stable `SESSION_SECRET`, `MAGIC_LINK_SECRET`, `PHONE_HASH_SECRET`
   and `JOIN_PASSCODE`, with #42 or equivalent public-deployment enforcement.
2. `EMAIL_SENDER=ses`, the verified `EMAIL_FROM`, the confirmed `SES_REGION`,
   and the correct public HTTPS origin in `PUBLIC_BASE_URL`.
3. `EMAIL_DAILY_BUDGET` and `EMAIL_SEND_INTERVAL_S` chosen from the live SES
   confirmations above, plus `SES_CONFIGURATION_SET` if configured for feedback.
4. `RATE_LIMIT_ENABLED=true`; accept or explicitly set
   `RL_UPLOAD_HOUR_TIMES=60` and `RL_UPLOAD_DAY_TIMES=200`. Keep the documented
   auth/proof/backoff defaults unless device or field measurements justify a change.
5. Keep `TRUST_PROXY_HEADER=true` only behind the proxy that overwrites
   `X-Real-IP`; the app must not be directly reachable through an untrusted path.
6. Test the installed clients' proof support, a controlled email sign-in,
   throttling/recovery and the moderator signals. Confirm privacy-policy coverage
   for auth/operational data and assign moderation and email-feedback owners.
7. Keep store-reviewer access off unless deliberately enabled for a review.
8. **Last**, set `EMAIL_OPEN_SIGNUP=true`. Startup refuses it if SES sending or
   persistent rate limiting is disabled. Turning it back to false restores the
   allowlist; it does not sign out existing accounts.

#184 remains open for provider capacity confirmation and moderator readiness.
