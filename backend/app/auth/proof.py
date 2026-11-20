"""First-party, IP-bound, expiring, single-use SHA-256 passcode challenges."""
import hashlib
import hmac
import secrets

from fastapi import HTTPException
from itsdangerous import BadData, SignatureExpired, URLSafeTimedSerializer

from app.config import settings
from app.ratelimit import key_hash

# The server-rendered /join fallback implements the same public work format
# as the app. It needs to work even when no frontend build is present.
FORM_SCRIPT = r"""
<script>
document.querySelector('form[action="/auth/join"]').addEventListener('submit', async function(event) {
  event.preventDefault();
  const form = event.currentTarget, button = form.querySelector('button');
  button.disabled = true; button.textContent = 'Checking…';
  try {
    if (!globalThis.crypto?.subtle) throw new Error('Please sign in by email in this browser.');
    const res = await fetch('/auth/join/challenge', {headers: {Accept: 'application/json'}});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || data.detail || 'Please try again.');
    if (data.escalated) button.textContent = 'Checking… this can take a minute';
    const encoder = new TextEncoder(), deadline = Date.now() + data.expires_in * 1000;
    for (let n = 0; n < 2 ** 32 && Date.now() < deadline; n += 64) {
      const hashes = await Promise.all(Array.from({length: 64}, (_, i) =>
        crypto.subtle.digest('SHA-256', encoder.encode(data.challenge + ':' + (n + i)))));
      const found = hashes.findIndex(hash => {
        const bytes = new Uint8Array(hash);
        for (let bit = 0; bit < data.difficulty; bit++) {
          if (bytes[bit >> 3] & (128 >> (bit & 7))) return false;
        }
        return true;
      });
      if (found < 0) continue;
      for (const [name, value] of Object.entries({challenge: data.challenge, solution: String(n + found)})) {
        let input = form.querySelector('input[name="' + name + '"]');
        if (!input) { input = document.createElement('input'); input.type = 'hidden'; input.name = name; form.append(input); }
        input.value = value;
      }
      form.submit(); return;
    }
    throw new Error('The sign-in check timed out. Please try again or sign in by email.');
  } catch (error) {
    let notice = form.querySelector('[role="alert"]');
    if (!notice) { notice = document.createElement('p'); notice.setAttribute('role', 'alert'); form.prepend(notice); }
    notice.textContent = error.message;
    button.disabled = false; button.textContent = 'Join with passcode';
  }
});
</script>
"""


class Stale(HTTPException):
    """A proof we issued, signed correctly, that can no longer be used: it
    expired, was issued to another address, or was already spent. Nothing
    about it suggests guessing, so it never counts toward backoff -- the
    client just needs a fresh challenge."""

    def __init__(self) -> None:
        super().__init__(400, "The sign-in check expired. Please try again.")


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.magic_link_secret, salt="passcode-proof-v1")


def required_bits(escalated: bool) -> int:
    return settings.join_pow_bits + (settings.join_pow_escalated_bits if escalated else 0)


def issue(client: str, *, escalated: bool = False) -> dict:
    """A challenge at the current difficulty.

    Escalated (the door-wide wrong-passcode budget is spent), the work is
    harder and the client is given less time to do it: `expires_in` is the
    solver's own deadline in every shipped app, so it bounds how long a slow
    phone, on any version, spins before saying "timed out, try again or sign in
    by email" -- rather than five minutes of "Checking...". `escalated` lets a
    current client say why it is slow; an older one ignores it."""
    bits = required_bits(escalated)
    return {
        "challenge": _signer().dumps({
            "nonce": secrets.token_urlsafe(24), "client": key_hash(client).hex(),
            "bits": bits,
        }),
        "difficulty": bits,
        "expires_in": (min(settings.join_pow_ttl_s, settings.join_pow_escalated_wait_s)
                       if escalated else settings.join_pow_ttl_s),
        "escalated": escalated,
    }


async def consume(conn, client: str, challenge: str, solution: str, *, min_bits: int | None = None) -> None:
    """Spend a solved challenge. `min_bits` is the difficulty required now: a
    genuine challenge issued easier than that (before the door escalated) is
    Stale -- solve a fresh one -- not a failure, so it never counts as guessing."""
    error = HTTPException(400, "The sign-in check expired or failed. Please try again.")
    if not challenge or len(challenge) > 1024 or not solution.isascii() or not solution.isdecimal() or len(solution) > 12:
        raise error
    try:
        data = _signer().loads(challenge, max_age=settings.join_pow_ttl_s)
    except SignatureExpired:
        raise Stale() from None
    except BadData:
        raise error from None
    if not isinstance(data, dict):
        raise error
    if not hmac.compare_digest(str(data.get("client", "")), key_hash(client).hex()):
        # Ours, but issued to another address: a phone that moved from wifi
        # to mobile data mid-solve, most likely.
        raise Stale()
    bits = data.get("bits")
    if not isinstance(bits, int) or not 8 <= bits <= 22:
        raise error
    digest = hashlib.sha256(f"{challenge}:{solution}".encode()).digest()
    if int.from_bytes(digest, "big") >> (256 - bits):
        raise error
    if min_bits is not None and bits < min_bits:
        raise Stale()
    used = await conn.fetchval("""INSERT INTO join_pow_uses (challenge_hash, expires_at)
        VALUES ($1, statement_timestamp() + interval '11 minutes')
        ON CONFLICT DO NOTHING RETURNING true""", hashlib.sha256(challenge.encode()).digest())
    if not used:
        raise Stale()
