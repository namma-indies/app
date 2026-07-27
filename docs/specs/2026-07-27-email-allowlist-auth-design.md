# Email + allowlist sign-in — design

Stage 1 of the rollout ladder in `docs/OPERATIONS.md`: enter an email, and if it
is allowlisted, get a magic link. Adds a **stable, identified** way in alongside
the existing anonymous passcode gate.

_Status: design approved 2026-07-27. Not yet implemented._

---

## Why

Two problems, one change.

**1. Identity is not stable today.** `routes/join.py` calls `create_observer`
unconditionally on every passcode submit — there is no lookup. The same person
joining twice becomes two observers: clear cookies, switch phone, or reinstall
the PWA and their sightings split across identities. `create_observer_and_link`
has the same shape; each mint makes a fresh observer.

So "who logged what" is currently answerable only per *session*, not per
*person*. Email is a real key, so looking observers up by it fixes this
structurally. **This is the larger win — the allowlist is almost a side effect.**

**2. Onboarding the internal team needs a shared secret over WhatsApp.** An
allowlisted domain is self-service and doesn't leak.

## Scope

In: email entry, domain/address allowlist, emailed single-use magic link,
observer lookup-or-create, login audit trail.

Out: open signup (stage 2), Google sign-in (stage 3), merging existing duplicate
observers, any admin UI.

---

## Decisions

### Two doors, one session — and they mean different things

`/join` grows an email section above the existing passcode form. Both mint the
same session cookie. The split becomes explicit and is worth stating plainly:

| Door | Identity | For |
|---|---|---|
| Passcode | Anonymous, unstable — new observer per join | WhatsApp-recruited field testers with no company email |
| Email | Stable, identified — one observer per address | Internal `@dognosis.tech` testers |

The passcode path **keeps** its create-every-time behaviour. Matching on typed
name was considered and rejected: a name is not a key, two Priyas collide, and
anyone could assume a colleague's identity by typing it. Anonymous-and-honest
beats identified-and-wrong.

### Lookup-or-create on email

The core fix. Normalize (trim, lowercase), look up by email, create only if
absent. A returning person resolves to their existing observer.

### `observers.email` — plaintext, unique, lowercased

A deliberate break from the `phone_hash` pattern. You need the plaintext to send
the link, so a hash sitting beside it buys nothing. Note `contact_enc bytea` was
declared in `0001_full_v2_schema.py` but never implemented — there is no
encryption helper to reuse, and building one for colleagues' corporate addresses
is not warranted.

Revisit at stage 2, when open signup brings public addresses.

### Allowlist lives in `.env`

```
EMAIL_ALLOWLIST_DOMAINS=dognosis.tech
EMAIL_ALLOWLIST_ADDRESSES=
```

Comma-separated; addresses covers one-off outsiders. Matches how `JOIN_PASSCODE`
is already operated (sed + `compose up`) — no new admin surface, no DB table, no
UI.

### Rejection is visible, not silent

A non-allowlisted address gets told so. Standard practice is anti-enumeration
silence, but the allowlist is a *domain*, not a secret, and silent failure
generates "did it even send?" pings during internal testing. The failure mode we
are optimising against is confusion, not attack.

### `login_tokens` table — single-use, 30-minute links

| Column | Purpose |
|---|---|
| `id` | Token identifier (the thing in the URL) |
| `observer_id` | Who it logs in |
| `expires_at` | 30 min for emailed links |
| `used_at` | NULL until redeemed; single-use enforcement |

Single-use is safe here: `dognosis.tech` is **Google Workspace** (verified via
MX), and Gmail proxies images but does not pre-click links. This would be unsafe
on Microsoft 365, where Safe Links pre-fetches and would burn the token before
the human clicks. **If the allowlist ever grows a Microsoft-hosted domain,
revisit single-use.**

Side benefit: this is a login audit trail — who signed in, when.

The existing stateless `MagicLinkProvider` token stays for the CLI mint path, so
hand-minted long-lived WhatsApp links keep working unchanged.

### Sender: Amazon SES

Reversed on 2026-07-27, having originally specified Resend. The stated reason
for avoiding SES was its sandbox-approval gate — but **the sandbox does not
block internal testing**. Sandbox restricts sending to *verified* identities,
and the entire pilot cohort is a handful of addresses that can be verified in
minutes; 200/day is far past what a few testers logging in will use. Production
access is only needed before stage 2 opens signup to the public.

That removed the one argument for adding a vendor. SES is in the NI AWS account
we already use for the S3 photo bucket, and the sender seam made the swap a
single class.

`EMAIL_SENDER=console|ses` in the box `.env`. Credentials come from the ambient
AWS chain, not app settings — the same IAM user that already holds the S3 keys,
with `ses:SendEmail` added, so the box gains sending without a new credential.

---

## What this does not fix

Existing passcode observers keep their sightings under their old identity.
Signing in by email later creates a clean, separate observer — **no merge, no
claim flow** (decided 2026-07-27). Data isn't lost, it's just not reattributed.
For a pilot cohort this size, the merge tooling isn't worth building.

## Risks

- **New sending domain.** A brand-new sending domain mailing Google Workspace
  can land in spam at first. Send a test before telling the team it's ready.
- **SES is in sandbox** until AWS grants production access (requested
  2026-07-27). Until then, a login link only reaches a **verified** recipient —
  so each tester's address must be verified in SES, or the mail bounces.
- **Single-use links assume Google Workspace.** Microsoft 365 Safe Links
  pre-clicks URLs and would burn a token before the human does. If the allowlist
  ever gains a Microsoft-hosted domain, revisit single-use.

## Dependencies

_All resolved 2026-07-27 except production access._

1. ~~SES sending domain~~ — `nammaindies.org` verified in the NI AWS account,
   region `ap-south-1`. DKIM `SUCCESS`, custom MAIL FROM `mail.nammaindies.org`.
2. ~~DNS~~ — 3 DKIM CNAMEs, MAIL FROM MX + SPF, and DMARC (`p=none`) live in the
   NI Cloudflare zone.
3. `ses:SendEmail` on the IAM user whose S3 keys are already in the box `.env`,
   plus `EMAIL_SENDER=ses`.
4. SES production access — **pending AWS review**. Not blocking internal
   testing, since verified recipients work in sandbox.
