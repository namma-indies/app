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
    reid_propose_min: float = 0.71      # >= this: engage a human
    reid_max_candidates: int = 5

    # WHY 0.71, AND WHAT IT COSTS
    # ---------------------------
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
    # So at 0.71 this system raises two prompts, both wrong, and never links two
    # sightings of the same dog. That is the deliberate setting: silence until
    # the evidence is genuinely conclusive, rather than a stream of coin-flips.
    # It also means re-ID contributes nothing until either the score
    # distribution improves (more frames per sighting -- see below) or this
    # number comes down against real verdicts in `confirmations`.
    #
    # Lower it to ~0.40 to trade precision for actually finding matches; the
    # numbers above are the exchange rate. Change it here, not in code.

    # Below this many embedded frames, a sighting is thin evidence: one photo
    # matches the right dog 37% of the time, eight frames 83%. When a candidate
    # clears propose_min on a thin sighting, asking for a short clip is worth
    # more than asking for a yes/no the contributor cannot answer confidently.
    reid_thin_evidence_frames: int = 4
    s3_bucket: str = "indiedex-dev"
    s3_access_key: str = "minio"
    s3_secret_key: str = "minio123"
    s3_region: str = "ap-south-1"


settings = Settings()
