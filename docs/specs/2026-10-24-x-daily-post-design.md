# A street indie a day on @nammaindies

_Design, 2026-10-24. Product owner: Akash. Status: for approval. Target: the release after v1.2.1._

## Why

The @nammaindies account on X should show what the app is for: ordinary
street dogs, seen by real people, in real neighbourhoods. One good sighting a
day does that better than any announcement, and the photos already exist in
the app. The work is getting a photo from someone's phone to the account
without anyone hand-copying images, and without exposing a dog's exact
location or a contributor who never agreed to be posted.

Success: the account posts a real indie every day there is something to post,
and keeping it fed takes the two admins a minute or two, not a chore.

## The flow (decided by Akash)

1. **Offer.** When someone logs a sighting, they can choose to share it with
   the Namma Indies team. Nothing is offered unless they choose to.
2. **Choose.** Admins see the sightings people have shared and click the ones
   to post.
3. **Post.** From there it is automatic: **one post a day**. If nothing has
   been picked, that day is **skipped quietly**, with no nudge.

Each post carries:
- the photo
- a **general location**, meaning the area name, never the exact spot
- **credit** to the person who took it, by their X handle

**What goes out:** the **whole photo**, not a crop to the animal. **Cats
are posted too**; "indie" covers both.

**If a posted sighting goes bad later** (rejected by moderation, hidden as a
duplicate): the app **flags it on the admin screen** so an admin can take the
post down on X. Nothing is deleted from X automatically.

**Who, for now:** only Akash's and Aswin's sightings. Both have consented.
Asking other contributors for handles and consent is issue #92.

## Goals

- A contributor can mark a sighting "share with the Namma Indies team" at
  capture time.
- A moderator-only screen lists shared sightings and lets an admin pick or
  pass on each one.
- Picked sightings form a queue. Once a day, the oldest picked sighting is
  posted to @nammaindies with its photo, area name and credit.
- An admin can see what has been posted and what is queued, and can remove
  something from the queue before it goes out.

## Non-goals (for now)

- Asking other users for handles or consent (#92).
- Contributors posting from their own accounts (#93).
- Naming-based captions, such as "we think this is Bruno" or "do you know who
  this is?". These wait until naming is further along (#4, #55).
- Instagram or any other network.
- Engagement tracking, replies, threads, choosing the best photo
  automatically, or more than one post a day.

## Anti-goals: failure even if it technically works

- **A post reveals where a dog lives more precisely than the area name.** This
  includes GPS left in the image's metadata, a map, coordinates or a street
  name in the caption.
- **A post credits or shows someone who did not consent.**
- **A post goes out that an admin did not pick**, or one moderation has since
  rejected.
- **The same sighting is posted twice**, or two posts go out on one day.
- **It costs real money without anyone noticing.**

## Constraints

- **X API is pay-per-use.** About $0.015 per post. A post that contains a
  **link costs ~$0.20**, roughly 13×, so **posts carry no URLs**; the app link
  lives in the account bio. @mentions are not links. Credits are bought;
  billing is set up with a monthly spending limit on X's side.
- **Credentials:** OAuth 1.0a user context for @nammaindies, with Read and
  write permission. The four values (Consumer Key, Consumer Secret, Access
  Token, Access Token Secret) are in Akash's Bitwarden. They go into the
  production box's `.env` and never into the repo, which is public.
- **Location rule #45:** the public sees an area, never an exact point. An X
  post is public, so the same rule applies to it.
- **Moderation:** only sightings with `review_status = 'valid'` can be picked
  or posted. Hidden duplicates (`duplicate_of` set, from #89) are excluded.
  The rule is checked again at posting time, not only at picking time.
- **The box is one Lightsail instance.** The only existing scheduled job is
  the DB backup, run by a systemd timer (`deploy/backup-db.timer`).

## Decisions I made (Claude's calls, flagged for review)

- **Who counts as an admin:** existing moderators (`trust_tier =
  'moderator'`, `require_moderator`). No new role.
- **Consent and handle storage for now:**
  - Two new fields on `observers`: an X handle and a "may be posted to Namma
    Indies' social accounts" flag.
  - Both are set by hand (SQL) for Akash and Aswin until #92 builds the UI.
  - Only observers with the flag see the "share with the team" option, so
    nobody offers something that can't be used yet.
- **Caption:**
  - Built from a fixed template, `Spotted in {area} 📸 @{handle}`.
  - **Pre-filled when the admin picks the sighting, and editable then.** The
    edited text is stored with the pick and posted exactly as stored.
  - `{area}` comes from the `areas` table. That table holds only Bengaluru's
    2022 wards, so a sighting outside it, or with no location, gets a blank
    area that the admin must fill in before the pick saves. Nothing ever
    guesses a city.
- **The admin's pick is the check on what the image shows** (house numbers,
  shop signs, faces, number plates), since the whole photo goes out. The pick
  screen shows the image at full size, exactly as it will be posted, next to
  the final caption. There is no automatic blurring or cropping.
- **Multi-animal frames:** a sighting cut from a multi-animal capture posts
  its full source frame, not the per-animal crop.
- **One image per post, and one post per image.**
  - When a sighting has several photos (a burst), the admin chooses which one
    goes out.
  - The pick records a photo, not just a sighting.
  - "Never post twice" holds per photo and per sighting. Two sightings cut
    from the same frame can't both go out.
- **Video captures are not eligible** in this version.
- **Posting time:** one fixed time a day, 09:00 IST.
- **Encoding:** the chosen image is re-encoded for X's image limits with all
  metadata stripped. Stored originals already have none, so this is belt and
  braces.
- **Failure:** a failed post stays at the head of the queue and is retried at
  the next daily run. The error shows on the admin screen. No email or push.
- **Kill switch:** an `.env` setting, changed over SSH. Admins don't toggle it
  in the app. With posting off, the daily run logs the post it would have
  made.
  - Launch with posting **off** for the first run, check the logged post,
    then turn it on.

## Left to the builder

- Schema shape: a share flag on the sighting vs a separate table, and how the
  queue and post history are stored. A record of the X post id and the time
  it went out is required, so a sighting can never be posted twice.
- Where the "share" control sits on the capture screen and in the Journal
  (for sightings already logged), and how it threads through `POST /sighting`
  and `POST /capture` / capture review.
- **How the daily run is scheduled.** A systemd timer like the backup's,
  invoking a script, is the obvious fit. An in-process loop is the
  alternative. Either way, at most one post per day, even across restarts and
  overlapping runs.
- The X client: OAuth 1.0a signing, media upload, then create post.
- **Looking up the area name for a single sighting.** There is no
  per-sighting area helper yet; `aggregates.py`'s `_AREA_LATERAL` does it in
  SQL for stats.
- The admin screen's layout. It sits with the existing moderator tools in the
  Dex view.

## Later (filed or noted)

- #92: handles and posting consent for every contributor.
- #93: share a sighting to your own X or Instagram.
- Naming-based captions, once naming exists (#4, #55).
