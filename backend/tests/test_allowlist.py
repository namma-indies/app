import pytest

from app.auth.allowlist import is_allowed, normalize_email


@pytest.mark.parametrize("raw,expected", [
    ("  Akash@Dognosis.TECH ", "akash@dognosis.tech"),
    ("a@b.co", "a@b.co"),
    ("no-at-sign", ""),
    ("", ""),
    ("two@at@signs.com", ""),
    ("trailing@", ""),
    ("@leading.com", ""),
])
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


def test_domain_is_allowed(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "email_allowlist_domains", "dognosis.tech")
    monkeypatch.setattr(settings, "email_allowlist_addresses", "")
    assert is_allowed("akash@dognosis.tech") is True
    assert is_allowed("someone@gmail.com") is False


def test_individual_address_is_allowed(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "email_allowlist_domains", "dognosis.tech")
    monkeypatch.setattr(settings, "email_allowlist_addresses", "friend@gmail.com")
    assert is_allowed("friend@gmail.com") is True
    assert is_allowed("other@gmail.com") is False


def test_allowlist_entries_are_whitespace_and_case_tolerant(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "email_allowlist_domains", " Dognosis.tech , example.org ")
    monkeypatch.setattr(settings, "email_allowlist_addresses", " Friend@Gmail.com ")
    assert is_allowed("x@example.org") is True
    assert is_allowed("friend@gmail.com") is True


def test_empty_allowlist_allows_nobody(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "email_allowlist_domains", "")
    monkeypatch.setattr(settings, "email_allowlist_addresses", "")
    assert is_allowed("akash@dognosis.tech") is False


# --- open signup -------------------------------------------------------------
# Stage 2 of the rollout ladder. Blocked on SES production access until
# 2026-08-01; since that grant (50k/day, any recipient) the allowlist check is
# the only thing between today and open signup.
#
# An explicit flag rather than "empty allowlist means everyone": `is_allowed`
# deliberately fails closed so a misconfigured env var cannot open the door, and
# inverting that meaning would turn a deleted line in `.env` into a silent
# public launch. Opening up should take a deliberate act.


def test_open_signup_allows_any_valid_address(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "email_allowlist_domains", "dognosis.tech")
    monkeypatch.setattr(settings, "email_allowlist_addresses", "")
    monkeypatch.setattr(settings, "email_open_signup", True)
    assert is_allowed("stranger@gmail.com") is True
    assert is_allowed("akash@dognosis.tech") is True


def test_open_signup_still_rejects_non_addresses(monkeypatch):
    """Open does not mean unvalidated -- these go to SES as recipients."""
    from app.config import settings
    monkeypatch.setattr(settings, "email_open_signup", True)
    assert is_allowed("") is False
    assert is_allowed("no-at-sign") is False


def test_open_signup_defaults_off(monkeypatch):
    """The door stays where it is unless someone deliberately moves it."""
    from app.config import settings
    assert type(settings).model_fields["email_open_signup"].default is False


def test_allowlist_governs_when_the_flag_is_off(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "email_allowlist_domains", "dognosis.tech")
    monkeypatch.setattr(settings, "email_allowlist_addresses", "")
    monkeypatch.setattr(settings, "email_open_signup", False)
    assert is_allowed("stranger@gmail.com") is False
    assert is_allowed("akash@dognosis.tech") is True
