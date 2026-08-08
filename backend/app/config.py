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
    reid_radius_m: float = 2000.0
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
    reid_propose_min: float = 0.25      # >= this: ask a human
    reid_max_candidates: int = 5
    s3_bucket: str = "indiedex-dev"
    s3_access_key: str = "minio"
    s3_secret_key: str = "minio123"
    s3_region: str = "ap-south-1"


settings = Settings()
