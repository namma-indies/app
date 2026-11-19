"""First-party, IP-bound, expiring, single-use SHA-256 passcode challenges."""
import hashlib
import hmac
import secrets

from fastapi import HTTPException
from itsdangerous import BadData, URLSafeTimedSerializer

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


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.magic_link_secret, salt="passcode-proof-v1")


def issue(client: str) -> dict:
    return {
        "challenge": _signer().dumps({
            "nonce": secrets.token_urlsafe(24), "client": key_hash(client).hex(),
            "bits": settings.join_pow_bits,
        }),
        "difficulty": settings.join_pow_bits,
        "expires_in": settings.join_pow_ttl_s,
    }


async def consume(conn, client: str, challenge: str, solution: str) -> None:
    error = HTTPException(400, "The sign-in check expired or failed. Please try again.")
    if not challenge or len(challenge) > 1024 or not solution.isascii() or not solution.isdecimal() or len(solution) > 12:
        raise error
    try:
        data = _signer().loads(challenge, max_age=settings.join_pow_ttl_s)
    except BadData:
        raise error from None
    if not isinstance(data, dict) or not hmac.compare_digest(str(data.get("client", "")), key_hash(client).hex()):
        raise error
    bits = data.get("bits")
    if not isinstance(bits, int) or not 8 <= bits <= 22:
        raise error
    digest = hashlib.sha256(f"{challenge}:{solution}".encode()).digest()
    if int.from_bytes(digest, "big") >> (256 - bits):
        raise error
    used = await conn.fetchval("""INSERT INTO join_pow_uses (challenge_hash, expires_at)
        VALUES ($1, statement_timestamp() + interval '11 minutes')
        ON CONFLICT DO NOTHING RETURNING true""", hashlib.sha256(challenge.encode()).digest())
    if not used:
        raise error
