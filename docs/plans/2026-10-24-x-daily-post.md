# Daily @nammaindies Post — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Contributors with consent can share a sighting with the team. Moderators pick shared photos and captions. Once a day, the oldest pick is posted to @nammaindies on X.

**Architecture:**
- One new table, `social_picks`, holds the queue and the post history. The database enforces the "at most once" rules with unique indexes.
- A plain module, `app/social.py`, holds eligibility, captions and the daily run.
- A thin X client, `app/x_client.py`, signs requests with OAuth 1.0a and posts.
- Moderator routes live in `app/routes/social.py`.
- A systemd timer, shaped like the DB backup's, runs `scripts/post_daily.py` once a day.

**Tech Stack:** FastAPI, asyncpg, Postgres/PostGIS, Pillow, httpx + Authlib (OAuth1), React/Vite, vitest, pytest.

**Spec:** `docs/specs/2026-10-24-x-daily-post-design.md`. Read it first; it is the source of truth for behaviour.

## Global Constraints

- **Only moderators** (`trust_tier = 'moderator'`) see or act on social routes, via `require_moderator`, which returns 404 to everyone else.
- **Only observers with `social_consent = true` and an `x_handle`** can share, and only their sightings can be picked or posted.
- **No caption may contain a URL.** Reject `http`, `www.` and any `domain.tld/` pattern at pick time.
- **Caption limit:** at most **260 characters** as Python counts them. That is a safe margin under X's weighted 280.
- **Default caption:** exactly `Spotted in {area} 📸 @{handle}`.
- **Eligible to pick or post:**
  - the sighting has `review_status = 'valid'` and `duplicate_of IS NULL`
  - it is shared (`shared_with_team_at IS NOT NULL`)
  - the photographer has consent
  - it is not a video. Exclude `captures.kind = 'video'` and legacy `sightings.attrs->>'source' = 'video'`
  - Eligibility is rechecked at posting time.
- **Postable photos for a sighting:**
  - photos with `content_sha256 IS NOT NULL`, either `photos.sighting_id = s.id` (legacy) or the source photo `animal_instances.source_photo_id` of its instances (capture)
  - this is the same pairing as `_MY_HASHES` in `app/identical.py`
  - crops are never posted
- **At most one post per IST calendar day; never the same sighting or photo twice.** Enforce both in the schema, not only in code.
- **Posting time:** 09:00 IST, which is `OnCalendar=*-*-* 03:30:00 UTC`.
- **Posting starts disabled** (`social_posting_enabled = False`). Disabled means dry run: log what would post, call nothing.
- **Secrets** (`X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`) live only in the box `.env`. The repo is public.
- **Handles stay out of the repo.** Akash's and Aswin's handles are set by SQL at enable time (Task 9), from the ops repo; tests use placeholder handles.
- **Copy:** an animal is "them", never "it". Wording is flat and plain.

## Review Focus

1. **The timer fires twice in one day**, from `Persistent=true` after a reboot plus the scheduled run. Exactly one post must result (Task 6 test `test_second_run_same_day_posts_nothing`).
2. **X accepted the post but the response was lost** (timeout on create). The row must not be reposted. It is left `posting` and shown as needing attention (Task 6 test `test_timeout_on_create_leaves_posting_and_never_reposts`).
3. **A sighting is rejected, hidden as a duplicate, or unshared after it was picked.** It must be skipped at posting time and the next pick posted instead (Task 6 test `test_ineligible_head_is_skipped`).
4. **The admin submits a caption with a URL, or that is blank, or still contains `{area}`.** It must be refused with 422 (Task 4 test `test_pick_rejects_bad_captions`).
5. **Consent is removed (SQL) between pick and post.** It must not post (covered by the Task 6 ineligible test with a consent variant).

---

### Task 1: Schema and settings

**Files:**
- Create: `backend/migrations/versions/0017_social_picks.py` (down_revision `0016_photo_content_hash`)
- Modify: `backend/app/config.py` (add settings next to `multi_animal_enabled`)
- Modify: `backend/tests/conftest.py` (add `social_picks` to both TRUNCATE lists)
- Test: `backend/tests/test_social_schema.py`

**Interfaces:**
- **Produces:**
  - `observers.x_handle text NULL` (stored without `@`)
  - `observers.social_consent boolean NOT NULL DEFAULT false`
  - `sightings.shared_with_team_at timestamptz NULL`
  - table `social_picks`:

    | column | type |
    |---|---|
    | `id` | uuid PK |
    | `sighting_id` | uuid NOT NULL UNIQUE REFERENCES sightings ON DELETE CASCADE |
    | `photo_id` | uuid NULL REFERENCES photos ON DELETE CASCADE |
    | `caption` | text NULL |
    | `status` | text NOT NULL CHECK IN (`'queued'`,`'posting'`,`'posted'`,`'passed'`) |
    | `picked_by` | uuid REFERENCES observers ON DELETE SET NULL |
    | `picked_at` | timestamptz NOT NULL DEFAULT now() |
    | `posted_at` | timestamptz NULL |
    | `posted_on` | date NULL |
    | `x_post_id` | text NULL |
    | `last_error` | text NULL |
    | `last_attempt_at` | timestamptz NULL |

  - Indexes: unique `(photo_id) WHERE photo_id IS NOT NULL`; unique `(posted_on) WHERE posted_on IS NOT NULL`.
  - CHECK: `status='passed' OR (photo_id IS NOT NULL AND caption IS NOT NULL)`.
  - `GRANT SELECT, INSERT, UPDATE, DELETE ON social_picks TO app_rw`.
  - Settings:
    - `social_posting_enabled: bool = False`
    - `x_api_key: str = ""`, `x_api_secret: str = ""`, `x_access_token: str = ""`, `x_access_token_secret: str = ""`
    - `social_area_kind: str = "bbmp_ward"`
    - `social_timezone: str = "Asia/Kolkata"`

- [ ] **Step 1: Write failing tests** in `test_social_schema.py`, using the `migrated_db` fixture:
  - `test_one_pick_per_sighting`: a second insert with the same `sighting_id` raises `asyncpg.UniqueViolationError`.
  - `test_one_pick_per_photo`: two sightings picking the same `photo_id` raises `UniqueViolationError`.
  - `test_one_post_per_day`: two rows with the same `posted_on` raises `UniqueViolationError`.
  - `test_queued_needs_photo_and_caption`: `status='queued'` with a NULL caption raises `CheckViolationError`; `status='passed'` with NULLs succeeds.
  - `test_consent_defaults_false`: a new observer has `social_consent = false` and `x_handle IS NULL`.
- [ ] **Step 2:** `cd backend && uv run pytest tests/test_social_schema.py -q`. Expected: FAIL, because the relation `social_picks` does not exist.
- [ ] **Step 3:** Write the migration (with a downgrade that drops everything) and the settings.
- [ ] **Step 4:** Re-run. Expected: PASS. Then run `uv run pytest tests/test_migration.py -q` and expect PASS.
- [ ] **Step 5:** Commit `feat(social): schema for shared sightings and the post queue`.

---

### Task 2: Eligibility, area and caption rules

**Files:**
- Create: `backend/app/social.py`
- Test: `backend/tests/test_social_rules.py`

**Interfaces:**
- **Consumes:** Task 1 schema; `settings.social_area_kind`.
- **Produces:**
  - `async def postable_photos(conn, sighting_id: UUID) -> list[UUID]`: photo ids per Global Constraints, ordered by `photos.created_at, id`; `[]` when ineligible.
  - `async def is_postable(conn, sighting_id: UUID, photo_id: UUID) -> bool`: true when `photo_id in await postable_photos(...)`.
  - `async def area_name(conn, sighting_id: UUID) -> str | None`: the first `areas` row of kind `social_area_kind` that `ST_Covers` the sighting's geog, `ORDER BY a.id LIMIT 1`, the same rule as `_AREA_LATERAL` in `app/aggregates.py`; `None` for no geog or no cover.
  - `def default_caption(area: str | None, handle: str) -> str`: returns `f"Spotted in {area} 📸 @{handle}"`, or `f"Spotted in {{area}} 📸 @{handle}"` when `area` is None (a literal placeholder that `validate_caption` refuses).
  - `def validate_caption(text: str) -> str | None`: returns an error message, or None when valid.

- [ ] **Step 1: Write failing tests:**
  - `test_postable_photos_legacy_and_capture_paths`: a legacy sighting returns its hashed photo. A capture sighting returns its instance's source photo, never the evidence crop.
  - `test_not_postable_when_rejected_duplicate_unshared_unconsented_or_video`: one parametrised case per condition in Global Constraints, each returning `[]`.
  - `test_area_name_uses_covering_ward_else_none`: insert a square `areas` row (kind `bbmp_ward`, name `Indiranagar`). A covered point gives `"Indiranagar"`; an uncovered point gives `None`; NULL geog gives `None`.
  - `test_default_caption_exact`: `default_caption("Indiranagar", "a_handle") == "Spotted in Indiranagar 📸 @a_handle"`.
  - `test_validate_caption`: each of `""`, `"   "`, `"see https://x.co"`, `"www.nammaindies.org"`, `"nammaindies.org/app"`, `"Spotted in {area} 📸 @a"` and a 261-character string returns a message. `"Spotted in Indiranagar 📸 @a_handle"` returns None.
- [ ] **Step 2:** `uv run pytest tests/test_social_rules.py -q`. Expected: FAIL (ImportError).
- [ ] **Step 3:** Implement the five functions with the signatures above. Write the eligibility as one SQL query.
- [ ] **Step 4:** Re-run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(social): who and what may be posted, and the caption rules`.

---

### Task 3: Contributors share with the team

**Files:**
- Modify: `backend/app/routes/moderation.py` (`GET /me` adds `can_share_socially`)
- Modify: `backend/app/routes/sighting.py` (Form field `share_with_team: bool = Form(False)`; set `shared_with_team_at = now()` on insert when true **and** the observer has consent)
- Modify: `backend/app/routes/capture.py` (same Form field; store `attrs["share_with_team"] = True` on the capture)
- Modify: `backend/app/captures.py` (`publish_groups`: set `shared_with_team_at` on sightings it inserts when the capture's `attrs->>'share_with_team' = 'true'` and the observer has consent)
- Create: route `POST /sighting/{sighting_id}/share` in `backend/app/routes/social.py` (router registered in `backend/app/main.py`)
- Test: `backend/tests/test_social_share.py`

**Interfaces:**
- **Produces:**
  - `GET /me` → `{..., "can_share_socially": bool}`, true when `social_consent AND x_handle IS NOT NULL`.
  - `POST /sighting/{id}/share`, JSON body `{"shared": bool}` → `200 {"shared": bool}`. Owner only (403 otherwise); 403 when the observer has no consent; 404 for an unknown id. Unsharing clears `shared_with_team_at` and deletes a `queued` pick for that sighting; it never touches a `posted` one.

- [ ] **Step 1: Write failing tests:**
  - `test_me_reports_can_share`
  - `test_post_sighting_share_flag_respected_only_with_consent`: posting with `share_with_team=true` and consent sets the timestamp; without consent the field is ignored and the timestamp stays NULL.
  - `test_capture_share_flag_carries_to_published_sightings`
  - `test_share_toggle_owner_only_and_consent_only`
  - `test_unshare_removes_queued_pick_keeps_posted`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/test_social_share.py tests/test_sighting.py tests/test_multi_captures.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(social): contributors can share a sighting with the team`.

---

### Task 4: Moderator routes: shared list, pick, pass, queue

**Files:**
- Modify: `backend/app/routes/social.py`
- Test: `backend/tests/test_social_routes.py`

**Interfaces:**
- **Consumes:** Task 2 functions, `require_moderator`, `S3Storage.urls`, and `thumb_key` from `app.photos`.
- **Produces:**
  - `GET /social/shared`: eligible shared sightings with no `social_picks` row, newest `shared_with_team_at` first. Each item: `{sighting_id, captured_at, species, handle, area (str|null), default_caption, photos: [{photo_id, url, thumb_url}]}`. `url` is the full-size presigned URL.
  - `POST /social/pick`, JSON `{sighting_id, photo_id, caption}` → `201 {id}`.
    - 422 when `validate_caption` returns a message, or when `is_postable` is false.
    - 409 on either unique violation (sighting or photo already picked).
    - Inserts a `queued` row with `picked_by`.
  - `POST /social/pass`, JSON `{sighting_id}` → `201`. Inserts a `passed` row.
  - `DELETE /social/pick/{id}` → `204` when `queued` or `passed`; `409` when `posting` or `posted`.
  - `GET /social/queue` → `{queued: [...oldest picked_at first], attention: [...status posting, or queued with last_error], posted: [...30 most recent]}`.
    - Each row: `{id, sighting_id, photo_id, thumb_url, caption, status, picked_at, posted_at, x_post_id, last_error, flagged}`.
    - `flagged` is true for a `posted` row whose sighting is now not `valid`, or has `duplicate_of` set, or is no longer shared.

- [ ] **Step 1: Write failing tests:**
  - `test_social_routes_404_for_non_moderators`: every route.
  - `test_shared_lists_only_eligible_unpicked`
  - `test_pick_rejects_bad_captions`: parametrised over Task 2's bad captions → 422.
  - `test_pick_refuses_photo_not_postable_for_that_sighting`: 422.
  - `test_pick_twice_same_sighting_or_photo_is_409`
  - `test_pass_hides_from_shared`
  - `test_delete_only_unposted`
  - `test_queue_flags_posted_sighting_later_rejected`
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(social): moderators pick, pass and see the queue`.

---

### Task 5: X client and image preparation

**Files:**
- Modify: `backend/pyproject.toml` (move `httpx` into runtime `dependencies`; add `authlib`) and `uv.lock` via `uv add httpx authlib`
- Create: `backend/app/x_client.py`
- Test: `backend/tests/test_x_client.py`

**Interfaces:**
- **Produces:**
  - `def prepare_image(original: bytes) -> bytes`:
    - JPEG, RGB, longest edge ≤ 4096 px, no EXIF or other metadata, ≤ 5 MB.
    - Steps quality down from 90 in tens until it fits; raises `ValueError` if it still doesn't fit at 50.
  - `class XClient` with `__init__(self, api_key, api_secret, access_token, access_token_secret, *, transport: httpx.AsyncBaseTransport | None = None)`:
    - `async def upload_image(self, jpeg: bytes) -> str`: returns the media id.
    - `async def create_post(self, text: str, media_id: str) -> str`: returns the post id.
  - Exceptions:
    - `class XRejected(Exception)`: X answered with 4xx/5xx. Safe to retry later, because nothing was created.
    - `class XUncertain(Exception)`: transport error or timeout **during `create_post`**. The post may exist.
  - `def x_client_from_settings() -> XClient`
  - Endpoints:
    - media: `POST https://api.x.com/2/media/upload` (multipart `media`, `media_category=tweet_image`)
    - post: `POST https://api.x.com/2/tweets` with JSON `{"text": text, "media": {"media_ids": [media_id]}}`
    - Signed with `authlib.integrations.httpx_client.OAuth1Auth`.
    - **Check both endpoint shapes against docs.x.com before implementing**, and change the constants if X has moved them.

- [ ] **Step 1: Write failing tests**, using `httpx.MockTransport`:
  - `test_prepare_image_strips_metadata_and_fits`: a 6000×4000 WebP with EXIF GPS → JPEG, longest edge 4096, `Image.open(out).getexif()` empty.
  - `test_upload_then_post_sends_oauth_and_media_id`: both requests carry an `Authorization: OAuth ...` header; the post body has the returned media id.
  - `test_http_error_raises_rejected`: a 403 on either call → `XRejected`.
  - `test_timeout_on_create_raises_uncertain_but_on_upload_rejected`
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(social): an X client that can say when it doesn't know`.

---

### Task 6: The daily run and its timer

**Files:**
- Modify: `backend/app/social.py` (add `run_daily`)
- Create: `backend/scripts/post_daily.py`
- Create: `deploy/social-post.timer`, `deploy/social-post.service`, `deploy/remote-social-post.sh`
- Modify: `deploy/README.md` (install steps, mirroring `deploy/BACKUPS.md`)
- Test: `backend/tests/test_social_daily.py`

**Interfaces:**
- **Consumes:** Tasks 2 and 5; `S3Storage.get`.
- **Produces:**
  - `@dataclass class RunResult: outcome: str; pick_id: UUID | None = None; x_post_id: str | None = None; detail: str | None = None`. `outcome` is one of `'posted'`, `'dry_run'`, `'already_posted_today'`, `'empty'`, `'blocked_by_uncertain'`, `'failed'`.
  - `async def run_daily(pool, storage, client: XClient | None, *, today: date, enabled: bool) -> RunResult`

  **Algorithm** (the order matters; the tests pin it):
  1. In a transaction holding `pg_advisory_xact_lock(734821903)`:
     - if a row has `posted_on = today` → `already_posted_today`
     - if any row is `posting` → `blocked_by_uncertain`
     - otherwise walk `queued` rows by `picked_at, id`. For each one that fails `is_postable`, set `last_error = 'no longer eligible'` and continue; it stays visible under attention.
     - the first eligible row is the head. None → `empty`.
     - if `enabled` is false: log the caption and photo id → `dry_run`, and change nothing.
     - otherwise set the head to `status='posting'`, `last_attempt_at=now()` and commit.
  2. Outside the transaction: fetch the original, `prepare_image`, `upload_image`, `create_post`.
     - success → `posted`, `x_post_id`, `posted_at=now()`, `posted_on=today`, `last_error=NULL`.
     - `XRejected`, or any error before `create_post` was sent → back to `queued` with `last_error` → `failed`.
     - `XUncertain` → leave `posting`, set `last_error` → `failed`.

  - `scripts/post_daily.py`:
    - computes `today` in `settings.social_timezone`
    - passes `enabled=settings.social_posting_enabled`, and `client=None` when disabled
    - prints the `RunResult`
    - exits 0 for every outcome except `failed`, which exits 1
  - Timer: `OnCalendar=*-*-* 03:30:00 UTC`, `Persistent=true`, `RandomizedDelaySec=120`.
  - The service runs `deploy/remote-social-post.sh`, which `docker compose exec -T app sh -c 'cd /app/backend && uv run python scripts/post_daily.py' < /dev/null`, the same shape as `remote-backup-db.sh`.

- [ ] **Step 1: Write failing tests**, with a fake `XClient` recording calls:
  - `test_dry_run_calls_nothing_and_changes_nothing`
  - `test_posts_oldest_queued_and_records_day`
  - `test_second_run_same_day_posts_nothing`
  - `test_empty_queue_is_quiet`
  - `test_ineligible_head_is_skipped`: rejected, duplicate, unshared and consent-removed variants. The next eligible pick posts.
  - `test_rejected_by_x_requeues_with_error`
  - `test_timeout_on_create_leaves_posting_and_never_reposts`: the next day's run returns `blocked_by_uncertain` and makes no X calls.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement `run_daily`, the script and the deploy files.
- [ ] **Step 4:** Run `uv run pytest tests/test_social_daily.py -q`, then the full backend suite the way `.github/workflows/ci.yml` runs it. Expected: PASS.
- [ ] **Step 5:** Commit `feat(social): one post a day, and never a second`.

---

### Task 7: Frontend — sharing from capture and the Journal

**Files:**
- Modify: `frontend/src/api.ts` (`Me.can_share_socially: boolean`; `PostSightingInput.shareWithTeam?: boolean`, appended as `share_with_team` in `buildSightingForm`; `setSightingShared(id: string, shared: boolean): Promise<{ shared: boolean }>`)
- Modify: `frontend/src/captureApi.ts` (`postCapture` appends `share_with_team`)
- Modify: `frontend/src/screens/Capture.tsx` (a checkbox labelled **"Share with the Namma Indies team"**, unchecked by default, rendered only when `can_share_socially`)
- Modify: `frontend/src/screens/Dex.tsx`, Journal entries (own sightings only, when `can_share_socially`: a toggle labelled **"Shared with the team"** / **"Share with the team"**)
- Test: `frontend/src/screens/Capture.test.tsx`, `frontend/src/screens/Dex.share.test.tsx`

- [ ] **Step 1: Write failing tests:**
  - `hides share option without consent`
  - `sends share_with_team when ticked`
  - `journal toggle calls setSightingShared and reflects the result`
- [ ] **Step 2:** `cd frontend && npx vitest run src/screens/Capture.test.tsx src/screens/Dex.share.test.tsx`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run vitest, `npx tsc --noEmit` and `npm run build`. Expected: all pass.
- [ ] **Step 5:** Commit `feat(social): share a sighting with the team from capture or the Journal`.

---

### Task 8: Frontend — the moderators' POSTS screen

**Files:**
- Modify: `frontend/src/api.ts` (`getSocialShared`, `pickForSocial(sightingId, photoId, caption)`, `passForSocial(sightingId)`, `getSocialQueue`, `removeSocialPick(id)`, with types matching Task 4's responses)
- Create: `frontend/src/screens/Social.tsx`
- Modify: `frontend/src/screens/Dex.tsx` (a **POSTS** toggle beside **FLAGS**, moderators only)
- Test: `frontend/src/screens/Social.test.tsx`

**Behaviour:**
- **Shared:**
  - a card per sighting showing the selected photo **full-width at full resolution** (`url`), with thumbnails to choose another photo when there are several
  - a caption textarea pre-filled with `default_caption`
  - when `area` is null, the textarea starts with `{area}` highlighted and **Queue** stays disabled until it's replaced
  - buttons **Queue for X** and **Not this one**
  - API 409/422 messages are shown inline on the card
- **Queue:** oldest first, with **Remove**.
- **Needs attention:** `posting` rows read "Posting didn't confirm. Check @nammaindies on X before anything else posts." Queued rows with `last_error` show the error.
- **Posted:** each row links to `https://x.com/nammaindies/status/{x_post_id}`. Flagged rows carry the badge "Taken down in the app — remove this post on X".

- [ ] **Step 1: Write failing tests:**
  - `renders shared cards with default caption`
  - `queue disabled while caption still has {area}`
  - `queues and removes the card`
  - `shows flagged posted rows with the take-down badge`
  - `shows uncertain posting warning`
- [ ] **Step 2:** Run `npx vitest run src/screens/Social.test.tsx`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run vitest, `tsc --noEmit` and `npm run build`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(social): the POSTS screen for moderators`.

---

### Task 9: Turn it on (operator checklist, after merge and deploy)

Not code. Run each step with Akash's approval, and record it in `ops/OPERATIONS.md` under a new "Daily X post" section.

- [ ] **Secrets:** Akash writes the four X values from Bitwarden into the box `.env` as `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`. Leave `SOCIAL_POSTING_ENABLED` unset (false). Restart the app container.
- [ ] **Consent and handles** for Akash and Aswin, by SQL on prod. Look up each observer id by email, then: `UPDATE observers SET x_handle='<handle, no @>', social_consent=true WHERE id=...;` for each. The handles themselves are recorded in the private ops repo, not here.
- [ ] **Make both of them moderators** if they aren't already (`trust_tier='moderator'`).
- [ ] **Install the timer:** `sudo cp deploy/social-post.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now social-post.timer`.
- [ ] **Dry run:** share a sighting, queue it on POSTS, then `sudo systemctl start social-post.service` and check the journal shows `dry_run` with the right caption.
- [ ] **Go live:** set `SOCIAL_POSTING_ENABLED=true`, restart the app, start the service once by hand, confirm the post on @nammaindies, and confirm a second start reports `already_posted_today`.
