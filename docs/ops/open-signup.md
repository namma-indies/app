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
| `AUTH_IP_BACKOFF_AFTER` | 10 | Failure that first triggers a lockout on an IP-keyed door (all of them today) |
| `RL_JOIN_GLOBAL_WRONG_TIMES` | 100 | Wrong passcodes per window across all sources before the passcode door pauses |
| `RL_JOIN_GLOBAL_WRONG_WINDOW_S` | 3600 | That window |
| `RL_JOIN_SUCCESS_IP_DAY_TIMES` | 50 | Successful passcode joins per source per day |
| `RL_CONSUME_TIMES` / `RL_CONSUME_WINDOW_S` | 60 / 900 | Sign-in link clicks per source per window |

Existing `RL_EMAIL_ADDR_*`, `RL_EMAIL_IP_*` and `RL_JOIN_*` budgets remain.
The address cooldown is separate from the address window. Wrong passcodes and
forged proofs trigger backoff; a genuine proof that expired, was issued to
another address or was already used only asks for a new one. Every passcode
attempt is charged against the per-source and global budgets before the
compare (a correct one is refunded), so parallel guesses cannot all be tried.
When the global budget runs out the passcode door pauses for the rest of its
window (email sign-in is unaffected) and the moderators' abuse signals say so.
Sign-in links are high-entropy, so consuming one has no failure backoff, only
the plain per-source window. An address that is not on the allowlist is not a
failed sign-in. A blocked request does not extend its lockout. Replies include
a readable wait and `Retry-After`. An IP is a shared resource on carrier NAT,
so the default lockout is bounded, never permanent. IPv6 sources are keyed by
their /64.

Limiter keys are domain-separated HMACs using `PHONE_HASH_SECRET`; raw email
addresses and IPs are not written to limiter tables. Expired rows are swept in
bounded batches by a background sweeper every minute; requests never pay for it.
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

Sends are paced by a shared token bucket (`EMAIL_SEND_RATE_PER_S`, burst
`EMAIL_SEND_BURST`): a send that finds it briefly empty waits up to
`EMAIL_SEND_MAX_WAIT_S` without holding a database connection, instead of
failing. The daily budget is split: account mail (deletion links and notices)
is capped at `EMAIL_ACCOUNT_DAILY_BUDGET` and can never use the
`EMAIL_SIGNIN_FLOOR_FRACTION` of the budget held back for sign-in links.
Requests for an address with no account answer exactly as for one with, but
spend no provider budget. Every source is also capped per day across sign-in
and deletion mail (`RL_EMAIL_IP_DAY_TIMES`, per /64 for IPv6).

SES errors are classified by error code, never by message text. Throttling
and transport failures back off across workers/restarts; a request retries
at most three times, with short waits only. `LimitExceededException` (the
24-hour quota) sets a shared `EMAIL_QUOTA_BACKOFF_S` pause.
`AccountSuspendedException`, `SendingPausedException` and
`MailFromDomainNotVerifiedException` need a human: they set a shared
`EMAIL_ACCOUNT_PAUSE_S` pause and log the code at error level. Per-message
refusals (`MessageRejected`, `BadRequestException`, ...) do not retry or pause. When admission
fails, the address cooldown rolls back, so it cannot contradict the displayed
wait. A failed send returns readable retry guidance. SDK retries are disabled
so every actual attempt is accounted for. No provider error text, recipient,
token or link goes into error logs.

| Setting | Default | Operator action |
| --- | --- | --- |
| `EMAIL_DAILY_BUDGET` | 200 | Set below the confirmed regional quota, leaving headroom for other senders |
| `EMAIL_SEND_RATE_PER_S` | 10 | Below the account's confirmed `MaxSendRate` (about 14/s), with headroom for other senders |
| `EMAIL_SEND_BURST` | 10 | At most the rate |
| `EMAIL_SEND_MAX_WAIT_S` | 2 | Longest a send waits for its turn before answering "busy" |
| `EMAIL_ACCOUNT_DAILY_BUDGET` | 40 | Cap on deletion links and notices per 24 hours |
| `EMAIL_SIGNIN_FLOOR_FRACTION` | 0.5 | Share of the daily budget only sign-in mail may use |
| `RL_EMAIL_IP_DAY_TIMES` | 100 | Mail requests per source per day (IPv6: per /64) |
| `RL_DELETE_REQUEST_DAY_TIMES` | 200 | Deletion-link requests per day, all sources, known address or not |
| `EMAIL_ACCOUNT_PAUSE_S` | 3600 | Pause after SES refuses for the whole account |
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
   Banned email addresses are hashed with `BAN_HASH_SECRET`, falling back to
   `PHONE_HASH_SECRET` while it is unset. Before ever rotating
   `PHONE_HASH_SECRET`, set `BAN_HASH_SECRET` to its current value, or every
   email ban is lifted; never rotate `BAN_HASH_SECRET` itself.
2. `EMAIL_SENDER=ses`, the verified `EMAIL_FROM`, the confirmed `SES_REGION`,
   and the correct public HTTPS origin in `PUBLIC_BASE_URL`.
3. `EMAIL_DAILY_BUDGET` and `EMAIL_SEND_RATE_PER_S` chosen from the live SES
   confirmations above, plus `SES_CONFIGURATION_SET` if configured for feedback.
4. `RATE_LIMIT_ENABLED=true`; accept or explicitly set
   `RL_UPLOAD_HOUR_TIMES=60` and `RL_UPLOAD_DAY_TIMES=200` per account (voice
   notes and re-transcriptions have buckets of their own at the same sizes),
   `RL_UPLOAD_IP_HOUR_TIMES=600` per source and `RL_UPLOAD_GLOBAL_HOUR_TIMES=5000`
   for the service. Replays of an accepted upload and requests the server turns
   away are refunded. Keep the documented
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

## Store reviewer access (#203, separate from open sign-up)

This door is **off by default** and does not need `EMAIL_OPEN_SIGNUP=true`, an
allowlisted address, a real inbox or the pilot passcode. It is for both Apple
and Google reviewers. Ship a native build containing the new sign-in option;
enabling the server flag cannot add it to an older installed bundle.

Aswin sets only these additional values, privately and consistently on all API
workers, when a store review needs access:

- `STORE_REVIEW_SECRET`: an independently generated random ASCII secret, 32–512
  characters (for example, generate one with `openssl rand -hex 32`). Do not
  reuse the pilot passcode or session/signing keys. It is a backend setting,
  never a `VITE_*` value or a credential committed to this repository.
- `STORE_REVIEW_ENABLED=true`. Startup refuses an enabled door without a usable
  secret. Keep false for deployments that do not need reviewer access.

Supply the code only through the stores' private app-access/review notes:
“On the sign-in screen choose **Store reviewer**, paste the supplied reviewer
code, and choose **Sign in as reviewer**. No email address is needed.” If a
console requires an account label, use “Store reviewer”; authentication uses
the code. Do not put the live credential in an issue, PR, screenshot or build.

The first valid login creates one `Store reviewer` account; later logins reuse
it across stores, workers, deployments and secret rotations. A partial unique
index enforces one marked account. It has no email. The account marker blocks
moderation server-side, even if its trust tier is accidentally promoted, and
`/me` never advertises it as a moderator.

The account has the normal capture experience and the normal upload budgets.
Review uploads therefore join the same cohort; decide what demonstration data
and reset policy the stores should see before supplying access. This code does
not seed or touch production data. A banned or erased review account is refused,
never replaced to bypass the ban or deletion. A pending deletion can still be
kept from the account screen within its grace period.

### Resetting the review account

Reviewers are asked to test account deletion, and do. Once the review account
is erased (or banned) the door answers 503 until an operator resets it. From
`/app/backend` inside the API container:

```sh
uv run python scripts/reset_store_reviewer.py                 # erase its content, start a fresh empty account
uv run python scripts/reset_store_reviewer.py --keep-content  # only cancel a deletion still in its grace period
```

The default erases whatever the account still holds exactly as an account
deletion would, retires it, and creates a fresh reviewer identity; existing
reviewer sessions stop working, the reviewer code does not change. A banned
review account is refused unless `--allow-banned` is passed. Run it before each
review round so reviewers start from an empty account.

Reviewer sessions require a separate signed, seven-day, HTTP-only credential
in addition to the normal session. **Disabling the flag or rotating the reviewer
secret revokes them on the next request**, including already-issued sessions.
Logout clears both credentials. Failed attempts use persisted rate limits and
progressive backoff; defaults are `RL_STORE_REVIEW_TIMES=5` failed attempts per
`RL_STORE_REVIEW_WINDOW_S=900` per source IP. Successful sign-ins do not count. Success/failure outcomes are logged
without code/cookie values; successful reviewer logins also appear in the private
hourly abuse signals.

Before submission, verify a fresh install can sign in, cannot moderate, and can
use the intended demo content. Disable the door after review; rotate its secret
before the next review round. #185 stays open for labels, support, ratings,
screenshots, review notes, release builds and the rest of store submission work.
