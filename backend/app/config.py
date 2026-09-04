from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://postgres:dev@localhost:5432/indiedex"
    database_url_sync: str = "postgresql+psycopg://postgres:dev@localhost:5432/indiedex"
    test_database_url: str = "postgresql://postgres:dev@localhost:5432/indiedex_test"
    test_database_url_sync: str = (
        "postgresql+psycopg://postgres:dev@localhost:5432/indiedex_test"
    )

    join_passcode: str = "dev-join"  # shared passcode for the /join closed-pilot gate

    # Comma-separated. Empty allowlist allows nobody -- fail closed.
    email_allowlist_domains: str = "dognosis.tech"
    email_allowlist_addresses: str = ""

    # Stage 2 of the rollout ladder: anyone with a working address can sign in,
    # no allowlist. A flag of its own rather than "empty allowlist means
    # everyone", because `is_allowed` fails closed on purpose -- inverting that
    # would make deleting a line in `.env` a silent public launch. Opening the
    # door should take a deliberate act. Requires SES production access, granted
    # 2026-08-01 (50k/day, any recipient).
    email_open_signup: bool = False

    # "console" (default) prints the link to the log; "ses" actually sends.
    # Fails safe: any other value means console.
    email_sender: str = "console"
    ses_region: str = "ap-south-1"
    email_from: str = "IndieDex <hello@nammaindies.org>"
    public_base_url: str = "http://localhost:8000"

    phone_hash_secret: str = "dev-phone-secret"
    session_secret: str = "dev-session-secret"
    magic_link_secret: str = "dev-magic-secret"

    s3_endpoint: str = "http://localhost:9000"
    # Host the browser fetches objects from, when it differs from where the
    # server writes them (dev proxy, or a CDN in front of S3). Empty means
    # "same as s3_endpoint".
    s3_public_endpoint: str = ""

    # --- re-identification -------------------------------------------------
    # Candidate scope for a new sighting. Street dogs hold small territories,
    # so a wide radius mostly buys look-alike collisions.
    # 1 km. A free-roaming street dog's range is typically a few hundred metres
    # around a food source, so this is generous rather than tight, and every
    # extra kilometre mostly buys look-alike collisions from dogs that cannot
    # be the same animal. Narrow enough that the vector check is asked to
    # separate neighbours, not strangers.
    reid_radius_m: float = 1000.0
    reid_recent_days: int = 365

    # Two-threshold merge rule, on cosine similarity of L2-normalised
    # MiewID-msv3 vectors (1.0 = identical).
    #
    # THESE NUMBERS ARE NOT CALIBRATED. Observed on a handful of real captures:
    # same dog ~0.36, different dogs ~0.10-0.15 -- a real gap but far narrower
    # than offline benchmarks imply, and measured on kennel footage rather than
    # street sightings. The defaults below are deliberately conservative: auto
    # merge is effectively disabled (1.01 can never be reached) so every
    # plausible match goes to a human instead of being silently merged.
    # Fit these to real verdicts in `confirmations` before lowering.
    reid_auto_merge_min: float = 1.01   # >= this: link automatically
    reid_propose_min: float = 0.50      # >= this: engage a human
    reid_max_candidates: int = 5

    # WHY 0.50, AND WHAT IT COSTS
    # ---------------------------
    # Lowered from 0.71 once merging had a surface (#29, #30). At 0.71 this
    # system raises almost nothing -- two prompts in the measured run, both
    # wrong -- so a review queue built on it is empty on the first day and every
    # day after. A threshold nothing reaches is not conservative, it is off.
    #
    # 0.50 is the point where the exchange below stops being a coin flip: 22
    # prompts at 36.4% precision, the best precision anywhere on the curve. It
    # finds 28.6% of true matches rather than 89.3%, which is the deliberate
    # half of the trade -- a queue people trust and work through beats a longer
    # one they learn to ignore.
    #
    # This is expected to move again, and downward, once real verdicts exist.
    # The circularity is the whole problem: the numbers below come from lab
    # footage, fitting them properly needs street verdicts, and street verdicts
    # need a queue with something in it. 0.50 breaks that loop by accepting a
    # known-imperfect number long enough to collect the data that replaces it.
    # Refit against `confirmations` once a few hundred rows exist.
    #
    # 0.7122 is what two dogs who genuinely resemble each other score. Brandi
    # and cheetah are different animals that look alike, and the number is not
    # a fluke of shared lighting: the 0.7122 pair was photographed seven days
    # apart (2026-06-08 and 2026-06-15), while a same-day pair of the same two
    # dogs scored only 0.5625. The resemblance is stable across time, so this is
    # a real look-alike ceiling rather than the highest noise we happened to see.
    #
    # The rule it encodes -- below the ceiling at which two look-alikes are
    # indistinguishable, you cannot claim sameness, so do not ask -- is sound.
    # What the same run also shows is that no genuine pair ever got near it: the
    # best score between two sightings of the SAME dog was 0.5532.
    #
    #   threshold  prompts  true  false  precision   true matches found
    #      0.30      130     25    105      19.2%      89.3%
    #      0.40       75     18     57      24.0%      64.3%
    #      0.50       22      8     14      36.4%      28.6%
    #      0.55        8      2      6      25.0%       7.1%
    #      0.71        2      0      2       0.0%       0.0%
    #
    # At 0.71 this system raised two prompts, both wrong, and never linked two
    # sightings of the same dog -- re-ID contributed nothing at all. That was
    # defensible while there was no way to act on a proposal; it stopped being
    # defensible once there was.
    #
    # At 0.50 roughly two thirds of what surfaces is still wrong. That is the
    # cost, and it is why the reviewer is asked "same dog?" rather than told.
    # Move to ~0.40 to trade precision for recall (64.3% of true matches at
    # 24.0% precision); the table above is the exchange rate. Change it here,
    # not in code.
    #
    # WHERE THIS NUMBER CAME FROM, AND WHY IT EXPIRES
    # -----------------------------------------------
    # Every figure above comes from the Dognosis v2 clips: 10 dogs filmed in a
    # single indoor scent-detection room. Constant background, constant
    # lighting, fixed camera positions, animals in harnesses, and a population
    # skewed to a few breeds -- several beagles and several black labs, which is
    # why look-alikes are so prominent in it.
    #
    # Street sightings share none of that. Backgrounds, lighting, distance,
    # angle and weather all vary; free-roaming indies vary far more in coat,
    # markings and size than a kennel of beagles; and the population is much
    # larger, so genuine look-alikes are rarer per pair but more numerous
    # overall. Those pull in opposite directions and neither is measured. The
    # honest position is that 0.71 is a placeholder shaped by a lab dataset,
    # not a property of MiewID or of dogs.
    #
    # So: recalibrate weekly against real verdicts as they arrive. The data is
    # already being collected -- every row in `confirmations` is a labelled pair
    # (sighting, verdict) and `match_proposals.score` holds what the model said
    # about it. Fit the threshold to those two columns; do not port a number
    # from the research repo, and do not treat this default as evidence.

    # Below this many embedded frames, a sighting is thin evidence: one photo
    # matches the right dog 37% of the time, eight frames 83%. When a candidate
    # clears propose_min on a thin sighting, asking for a short clip is worth
    # more than asking for a yes/no the contributor cannot answer confidently.
    reid_thin_evidence_frames: int = 4
    # Sized for request handlers plus the background tasks that run after the
    # response; see the comment in main.py's lifespan.
    db_pool_min: int = 5
    db_pool_max: int = 30

    # How coarse another observer's sightings look on the map and on a dog
    # card. Full precision is reserved for animals you photographed yourself.
    #
    # 1 km is a neighbourhood in Bangalore: enough to show where dogs are and
    # how varied they are, not enough to find one. The number is a product
    # judgement rather than a derived one, and it is here so it can move
    # without a code change. See app/precision.py for why this is a grid cell
    # and not a jittered point -- jitter averages away under refresh, and leaks
    # further the more sightings a dog has, which protects the best-documented
    # animals least.
    map_coarsen_cell_m: float = 1000.0

    s3_bucket: str = "indiedex-dev"

    # Where the pre-exported ONNX models live, inside s3_bucket. They are
    # gitignored and not baked into the image (~430 MB, and MiewID declares no
    # upstream licence), so the container fetches them on boot instead --
    # see scripts/fetch_models.py.
    models_s3_prefix: str = "models/"
    s3_access_key: str = "minio"
    s3_secret_key: str = "minio123"
    s3_region: str = "ap-south-1"


settings = Settings()
