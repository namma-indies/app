# What it takes to launch on both stores

Scoped 2026-08-29, while filling in Play's content-ratings questionnaire.

Today IndieDex ships to **internal testers only** on both stores: TestFlight
via a public link, Play via an email-gated internal track. Neither store has
reviewed the app. Going public is a different bar on both, and the work
overlaps heavily — but **not identically**, which is the thing worth knowing
before building anything.

---

## 0. The fork to resolve first — it costs weeks, not hours

**Is the Play developer account personal or organization?**
Play Console → Settings → Developer account.

Personal accounts created after 13 November 2023 must run a **closed test with
12+ testers for 14 continuous days** before production access unlocks. The
testers must be real people on real devices, all overlapping in the same
window, and since 2026 Google also checks they genuinely used the app. One
dropout resets the counter.

**Organization accounts verified with a D-U-N-S number are exempt** — but
verification itself takes 2–4 weeks and needs business documents.

So the two paths are roughly the same calendar length, and both run in
parallel with engineering. Which one starts is the highest-leverage thing to
settle today; everything else in this document can wait a week without cost.

This does not affect Apple at all, and does not affect Play internal testing,
which is what we use now.

---

## 1. Calendar-clock items — start these, they run while we build

| Item | Who | Lead time |
|---|---|---|
| Resolve account type, then start D-U-N-S **or** the 12/14 closed test | Akash | 2–4 weeks either way |
| First Beta App Review per version (external TestFlight) | Apple | hours–days, every release |
| Full App Review (Apple) — first submission is the slow one | Apple | days |

---

## 2. Engineering — and Apple sets the bar, not Google

This is the part where the stores differ, and where scoping to Google first
would mean building it twice.

**Google Play** ([UGC policy](https://support.google.com/googleplay/android-developer/answer/9876937))
requires an in-app system for **reporting**. Blocking is required only for
apps with 1:1 interaction — DMs, tagging, mentions — which IndieDex has none
of.

**Apple** ([Guideline 1.2](https://developer.apple.com/app-store/review/guidelines/))
has no such carve-out. Apps with user-generated content **must** include:

> - A method for filtering objectionable material from being posted to the app
> - A mechanism to report offensive content and timely responses to concerns
> - The ability to block abusive users from the service
> - Published contact information so users can easily reach you

All four. Which means the real target is Apple's list, and Play's requirement
is a subset of it. Tracked in #27.

**Status, 2026-09-04.** Three of Apple's four are built; blocking is not.

- **Report** — *shipped.* An action in the map popup on other people's
  sightings. `sightings.review_status` turned out to be exactly the machinery
  guessed at below: it existed, `/map` already filtered on it, and nothing had
  ever written to it, so the filter was unreachable code.
- **Filter** — *shipped, as the pending state.* A reported sighting leaves
  `/map`, `/dogs` and `/proposals` at once and waits for a moderator. That is
  the human-in-the-loop reading of Apple's wording, which is the honest one at
  this cohort size; automated screening is not built and is not proposed.
- **Contact** — *shipped.* `privacy` and `contact us` on the sign-in gate and
  in the app chrome, which also covers 5.1.1's in-app privacy policy.
- **Block** — *not built.* Still the genuine product question below: what
  blocking means in a shared-map app with no messaging. It is the one remaining
  Apple item.

Original shape, kept for the reasoning:

- **Report** — an action on the sighting detail sheet. `sightings.review_status`
  already carries `pending`/`valid`/`rejected`, so hiding a reported sighting
  from `/map` and `/dogs` may be mostly plumbing rather than new machinery.
  Worth checking before designing anything.
- **Block** — observer-level. What "blocking" even means in a shared-map app
  with no messaging is a genuine product question, not an obvious one.
- **Filter** — Apple's word is "filtering objectionable material from being
  posted." Whether that means automated screening or a human-in-the-loop
  pending state for a small cohort is the open question. The `pending` state
  above may already be the honest answer.
- **Contact** — nearly free. `nammaindies@gmail.com` is already published on
  the privacy page; it needs to be reachable from the listing and the app.

**Apple 5.1.1** wants the privacy policy linked **inside the app**, not only in
App Store Connect metadata. *Done* — `privacy` and `contact us` sit on the
sign-in gate and in the app chrome.

---

## 3. Console and forms — mostly Akash, mostly a day's work

Largely the same content submitted twice, in two different shapes.

| | Apple | Google |
|---|---|---|
| Privacy policy URL | required | required |
| Privacy questionnaire | App Privacy "nutrition labels" | Data safety form |
| Age rating | Apple's questionnaire | IARC content rating |
| Screenshots | per device size | per device size |
| Description / listing copy | required | required |
| Support URL | required | required |
| Reviewer sign-in credentials | required (same passcode approach) | done |
| Export compliance | done (in `Info.plist`) | n/a |

The privacy and data-safety answers should agree with each other and with
`nammaindies.org/privacy` — the honest answers there are that we collect
photos, email, and full-precision coordinates, and that coordinates are
visible to every signed-in user. Divergence between these three is the kind
of thing that gets found later and read as concealment.

---

## 4. Already done

- Both apps build and ship from a single tag, iOS and Android together.
- TestFlight public link live; builds now distribute to testers automatically
  (`deploy-ios.yml`), export compliance declared permanently.
- Privacy policy written from the schema and published.
- Play: sign-in details declaration, content rating questionnaire.

---

## 5. Deliberately not in this document

- **The DPDP age question.** The privacy page says under-13, which is the
  American COPPA threshold; India's DPDP Act defines a child as under 18. That
  needs a real opinion rather than a guess, and listing it as a task would
  imply the answer is known.
- **#5 and #15** — cohort visibility, and sightings-versus-individuals. They
  interact with everything above, but they are design threads, not store
  requirements. Resolving them may change what "blocking" and "reporting"
  should mean, which is an argument for having that conversation before
  building #27, not for folding them in here.
