from app.config import settings


def normalize_email(raw: str) -> str:
    """Trim and lowercase an address. Returns "" for anything that isn't a
    plausible `local@domain`, so callers can treat falsy as invalid."""
    email = raw.strip().lower()
    local, sep, domain = email.partition("@")
    if not sep or not local or not domain or "@" in domain:
        return ""
    return email


def _split(csv: str) -> set[str]:
    return {part.strip().lower() for part in csv.split(",") if part.strip()}


def is_allowed(email: str) -> bool:
    """True if the address may be sent a login link.

    An empty allowlist allows nobody -- a misconfigured env var must not open
    the door. `email_open_signup` is the deliberate way to open it, and it still
    requires a plausible address: these strings are handed to SES as recipients,
    and open signup is not unvalidated signup.
    """
    if not email or not normalize_email(email):
        return False
    if settings.email_open_signup:
        return True
    domain = email.rpartition("@")[2]
    return (
        email in _split(settings.email_allowlist_addresses)
        or domain in _split(settings.email_allowlist_domains)
    )
