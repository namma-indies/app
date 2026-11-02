import hashlib
import hmac
from uuid import UUID

from itsdangerous import BadData, BadSignature, URLSafeSerializer

from app.config import settings


def phone_hash(e164: str) -> str:
    """HMAC-SHA256 hex digest of an E.164 phone number, keyed by
    `settings.phone_hash_secret`. Never store raw numbers or plain SHA-256."""
    return hmac.new(
        settings.phone_hash_secret.encode(), e164.encode(), hashlib.sha256
    ).hexdigest()


def email_hash(normalized_email: str) -> str:
    """A keyed hash of an address, for remembering a banned one (#180) without
    keeping the address. Same key as `phone_hash`, separated by a prefix."""
    return hmac.new(
        settings.phone_hash_secret.encode(), b"email:" + normalized_email.encode(), hashlib.sha256
    ).hexdigest()


def _session_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.session_secret, salt="session")


def issue_session(observer_id: UUID) -> str:
    return _session_serializer().dumps(str(observer_id))


def read_session(cookie_value: str) -> UUID | None:
    try:
        raw = _session_serializer().loads(cookie_value)
        return UUID(raw)
    except (BadSignature, BadData, ValueError):
        return None
