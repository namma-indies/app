"""Fixed-window rate limiting, in process.

There was none of this anywhere in the application until now. The endpoint
that needed it most was never `/stats` -- it is the email login path, which
sits in front of production SES with no throttle of any kind.

In-process and no Redis: `deploy/entrypoint.sh` runs a single uvicorn with no
--workers, so one dict is the whole picture and the counts are exact. Two
consequences are deliberate rather than overlooked:

  - limits reset on deploy, which is fine for dampening abuse and would not be
    fine if this ever became a quota;
  - adding --workers would silently multiply every limit by the worker count,
    at which point this file is the thing to revisit.
"""

import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import HTTPException
from starlette.requests import Request

from app.config import settings

# Sweep is O(n), so it runs every SWEEP_EVERY writes rather than on each one.
# The LRU cap is the real bound; the sweep just keeps idle memory honest.
SWEEP_EVERY = 1000


@dataclass(frozen=True)
class Limit:
    times: int
    window_s: int


class _FixedWindow:
    """Counters keyed by (key, window index), capped and LRU-evicted.

    The cap is not tidiness. A map keyed by client IP that grows without bound
    is itself the denial-of-service: one forged key per request and the
    limiter exhausts the box it was added to protect.
    """

    def __init__(self, max_keys: int = 10_000) -> None:
        self._hits: OrderedDict[tuple[str, int], tuple[int, float]] = OrderedDict()
        self._max_keys = max_keys
        self._writes = 0

    def clear(self) -> None:
        self._hits.clear()
        self._writes = 0

    def hit(self, key: str, limit: Limit, now: float) -> int | None:
        """Count one request. Returns None if allowed, else seconds to wait."""
        window = int(now // limit.window_s)
        expires_at = (window + 1) * limit.window_s
        slot = (key, window)

        count, _ = self._hits.get(slot, (0, expires_at))
        count += 1
        self._hits[slot] = (count, expires_at)
        self._hits.move_to_end(slot)

        self._writes += 1
        if self._writes % SWEEP_EVERY == 0:
            for stale in [s for s, (_, exp) in self._hits.items() if exp <= now]:
                del self._hits[stale]
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)

        if count > limit.times:
            return max(1, int(expires_at - now))
        return None


_counter = _FixedWindow()


def reset() -> None:
    _counter.clear()


def client_key(request: Request) -> str:
    """The caller's IP, from the one header the proxy controls.

    Caddy sets X-Real-IP from the actual peer and overwrites anything the
    client sent (deploy/Caddyfile). Reading X-Forwarded-For instead would mean
    depending on whether the proxy appends to or replaces a client-supplied
    value -- and getting that backwards is not a bug, it is a bypass: the
    limiter would key on a string the attacker chose.

    Off by default, because in dev and in tests there is no proxy in front and
    the header is simply something the caller typed.
    """
    if settings.trust_proxy_header:
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"


def check(key: str, limit: Limit) -> None:
    if not settings.rate_limit_enabled:
        return
    retry_after = _counter.hit(key, limit, time.time())
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Try again shortly.",
            headers={"Retry-After": str(retry_after)},
        )


# Keys are built by the caller rather than by a dependency factory. The
# authenticated surfaces key on `obs:<observer_id>` -- identity, not address,
# so they never consult a client-controllable header at all and two people
# behind one NAT do not share a budget. The unauthenticated ones key on
# `<scope>:ip:<addr>`, scoped so one endpoint's budget cannot be spent by
# another.
#
# The limits are functions rather than module constants so that a test can
# monkeypatch the underlying setting and have it take effect.


def stats_limit() -> Limit:
    return Limit(settings.rl_stats_times, settings.rl_stats_window_s)


def email_limit() -> Limit:
    return Limit(settings.rl_email_times, settings.rl_email_window_s)


def join_limit() -> Limit:
    return Limit(settings.rl_join_times, settings.rl_join_window_s)
