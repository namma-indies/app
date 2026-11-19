"""Optional Cloudflare Turnstile check for the public grievance form.

Off unless both `turnstile_site_key` and `turnstile_secret_key` are set, and
then nothing changes. When on, the browser widget produces a single-use token
that is checked server-side against Cloudflare's siteverify endpoint before a
complaint is saved. It fails closed: an unreachable or erroring siteverify
refuses the submission with a retry message rather than letting it through.
The token is never logged.
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
# The form field the widget writes its token into.
TOKEN_FIELD = "cf-turnstile-response"
# Tokens are at most 2048 characters (Cloudflare docs); refuse anything longer
# without a network call.
MAX_TOKEN = 2048

# Tests swap this for an httpx.MockTransport.
_transport: httpx.AsyncBaseTransport | None = None


class TurnstileUnavailable(Exception):
    """siteverify could not give an answer (timeout, network, 5xx, bad body)."""


def turnstile_enabled() -> bool:
    return bool(settings.turnstile_site_key.strip() and settings.turnstile_secret_key.strip())


async def verify_turnstile(token: str) -> bool:
    """True when Cloudflare confirms the token. False for a missing, invalid,
    expired or reused token. Raises TurnstileUnavailable when there is no
    answer to be had, so the caller can ask the person to retry."""
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN:
        return False
    try:
        async with httpx.AsyncClient(timeout=settings.turnstile_timeout_s, transport=_transport) as client:
            response = await client.post(SITEVERIFY_URL, data={
                "secret": settings.turnstile_secret_key.strip(), "response": token})
        if response.status_code >= 500:
            raise TurnstileUnavailable(f"siteverify answered {response.status_code}")
        result = response.json()
    except TurnstileUnavailable:
        logger.warning("turnstile siteverify unavailable: server error")
        raise
    except (httpx.HTTPError, ValueError) as exc:
        # Type only: the request carries the secret and the token.
        logger.warning("turnstile siteverify unavailable: %s", type(exc).__name__)
        raise TurnstileUnavailable(type(exc).__name__) from None
    if not isinstance(result, dict):
        raise TurnstileUnavailable("unexpected siteverify body")
    if result.get("success") is True:
        return True
    codes = result.get("error-codes") or []
    # A misconfigured secret is ours, not the visitor's: say so in the log and
    # fail closed with the retry message.
    if any(c in {"missing-input-secret", "invalid-input-secret", "internal-error"} for c in codes):
        logger.error("turnstile siteverify refused our configuration: %s", ",".join(map(str, codes)))
        raise TurnstileUnavailable("configuration")
    logger.info("turnstile check failed: %s", ",".join(map(str, codes)) or "no reason")
    return False
