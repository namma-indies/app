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
