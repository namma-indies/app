import hashlib

from app.ids import uuid7
from app.security import issue_session, phone_hash, read_session


def test_phone_hash_is_hmac_not_plain_sha256():
    h = phone_hash("+919999999999")
    assert h != hashlib.sha256(b"+919999999999").hexdigest()
    assert phone_hash("+919999999999") == h  # deterministic


def test_email_ban_hash_survives_rotating_the_phone_key(monkeypatch):
    """A ban hashed before `ban_hash_secret` existed keeps matching while it is
    unset, and keeps matching after the phone key rotates once it is set to
    the old phone key (the documented order)."""
    from app.config import settings
    from app.security import email_hash
    monkeypatch.setattr(settings, "phone_hash_secret", "old-phone")
    monkeypatch.setattr(settings, "ban_hash_secret", "")
    banned = email_hash("a@example.org")                       # as stored before this setting
    monkeypatch.setattr(settings, "ban_hash_secret", "old-phone")
    assert email_hash("a@example.org") == banned
    monkeypatch.setattr(settings, "phone_hash_secret", "new-phone")
    assert email_hash("a@example.org") == banned               # rotation lifts no ban
    assert phone_hash("+919999999999") != email_hash("+919999999999")
    monkeypatch.setattr(settings, "ban_hash_secret", "other")
    assert email_hash("a@example.org") != banned               # it is the key


def test_session_roundtrip_and_tamper():
    oid = uuid7()
    tok = issue_session(oid)
    assert read_session(tok) == oid
    assert read_session(tok[:-2] + "xx") is None
    assert read_session("garbage") is None
