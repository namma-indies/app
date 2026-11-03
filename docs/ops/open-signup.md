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
