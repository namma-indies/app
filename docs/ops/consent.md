# Account consent

Consent enforcement defaults off while the documents are finalized. This code
does not contain or publish legal text. Ship together with #222's policy links
and support routes, and synchronize the migration number with main before the
launch stack lands. This branch starts at `0034_account_consents` after
`0033_blocking`.

Aswin must set these privately once the approved documents are published:

| Setting | Meaning |
| --- | --- |
| `TERMS_VERSION` | Opaque identifier for the published terms revision |
| `PRIVACY_VERSION` | Opaque identifier for the published privacy revision |
| `COMMUNITY_GUIDELINES_VERSION` | Opaque identifier for the guidelines revision |
| `CONSENT_ENABLED` | Set true after current clients and the final links are available |
| `CONSENT_MINIMUM_AGE` | Optional integer for the age assertion; leave unset while #194 is undecided |

Also configure #222's `TERMS_URL`, `PRIVACY_URL`,
`COMMUNITY_GUIDELINES_URL`, `GRIEVANCE_URL`, `SUPPORT_CONTACT_NAME`,
`SUPPORT_CONTACT_EMAIL` and optional `SUPPORT_CONTACT_DETAILS`. Check that the
public documents are the approved versions before enabling consent. Reuse
neither a version identifier nor an existing document revision for changed
wording: bump the corresponding version whenever that document changes.

When enabled, email offers separate sign-in and account-creation choices.
Returning accounts can request a link without agreeing again. An unknown
address using the sign-in choice receives the same neutral screen, without
creating an account; it can choose **Create an account** from either sign-in
screen. Creating an account requires both unchecked-by-default boxes before
the server creates it. Passcode joins always create a new account and require
the same affirmative fields.

For a new email signup, the submitted versions and server timestamp travel
with that single-use login token. Redemption writes the receipt in the same
transaction as consuming the token. A later login request cannot inherit an
unused token's pending agreement. Choosing account creation with an existing
address never changes that account's receipt; its owner reviews policies
after signing in. Absorbing passcode
accounts by a typed email name never transfers their consent to the verified
account.

Normal authenticated use returns 428 until the account has a current receipt.
The app prompts on initial load or foreground resume, using a modal that
preserves the draft underneath. Account management/deletion, sign-out, public
support and document links remain available. Queued uploads retain bytes,
endpoint and retry token as pending; accepting resumes the queue. An older
client that does not implement the consent flow needs updating before
enforcement is enabled.

Receipts store document versions, a server timestamp, the entry path and the
minimum-age assertion shown (zero means none). They contain no birth date, IP
address or user-agent. Duplicate acceptance preserves the original timestamp;
new versions add history. Account erasure deletes receipts and pending tokens.
No existing account is backfilled as having agreed.

`CONSENT_MINIMUM_AGE` adds **I am at least N years old** at signup and, when
needed, to the existing-account prompt. Raising it asks accounts whose prior
assertion is insufficient to confirm again. It is self-assertion, with no
parental-consent or age-verification implementation.

## Product questions for Akash

- Confirm blocking normal use until agreement, including after a document
  change. Deletion, sign-out and help remain available.
- Confirm the explicit email account-creation choice, which avoids repeated
  agreement on ordinary email sign-in.
- Decide N and the under-age/parental policy in #194 before configuring an age
  assertion; the setting alone does not establish that policy.
- Choose the approved version identifiers, publication owners and when a
  document change warrants a fresh acceptance request.
- Confirm receipt erasure with the account under the current deletion promise.
- Decide the shared store-reviewer account's consent/reset policy; the same
  account-level receipt applies and no reviewer bypass is introduced.
