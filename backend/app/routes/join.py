import hmac
import logging
from html import escape

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from app.auth.allowlist import is_allowed, normalize_email
from app.auth.deps import clear_session_cookie, set_session_cookie
from app.auth.email_login import (
    absorb_passcode_observers,
    get_or_create_observer_by_email,
)
from app.auth.logintoken import consume as consume_login_token
from app.auth.logintoken import issue as issue_login_token
from app.auth.magiclink import create_observer
from app.auth import proof
from app.auth.audit import audit_failures
from app.config import settings
from app.deps import get_conn
from app.email.sender import get_sender
from app.email.delivery import deliver, reserve
from app.abuse import record
from app.ratelimit import (Limit, check, check_backoff, clear_failures, client_key,
                          cooldown, email_addr_limit, email_ip_limit, join_limit, record_failure)
from app.security import email_hash

logger = logging.getLogger(__name__)
router = APIRouter()

_STYLE = """
  :root { --terra:#c15f3c; --terra-dark:#a44a2b; --cream:#f4ead9; --ink:#2e2016; --line:#d8c4a6; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100dvh; display:grid; place-items:center; padding:24px;
         background:var(--cream); color:var(--ink);
         font:16px/1.5 ui-rounded,-apple-system,"Segoe UI",Roboto,sans-serif; }
  .card { width:100%; max-width:360px; background:#fffdf8; border:1px solid var(--line);
          border-radius:16px; padding:28px 24px; box-shadow:0 8px 30px rgba(46,32,22,.12); }
  h1 { margin:0 0 2px; font-size:1.4rem; letter-spacing:.02em; }
  .paw { font-size:1.6rem; }
  .sub { margin:.2rem 0 1.3rem; color:#6b5844; font-size:.92rem; }
  .err { margin:.2rem 0 1.3rem; color:var(--terra-dark); font-size:.92rem; font-weight:600; }
  .ok { margin:.2rem 0 1.3rem; color:#3d7a4a; font-size:.92rem; font-weight:600; }
  label { display:block; font-size:.8rem; font-weight:600; color:#6b5844; margin:0 0 6px; }
  input { width:100%; padding:12px 14px; margin-bottom:16px; font-size:1rem;
          border:1px solid var(--line); border-radius:10px; background:#fff; color:var(--ink); }
  input:focus { outline:2px solid var(--terra); border-color:var(--terra); }
  button { width:100%; padding:13px; font-size:1rem; font-weight:700; color:#fff; cursor:pointer;
           background:var(--terra); border:0; border-radius:10px; }
  button:active { background:var(--terra-dark); }
  .divider { display:flex; align-items:center; gap:10px; margin:22px 0 18px;
             color:#6b5844; font-size:.78rem; }
  .divider::before, .divider::after { content:""; flex:1; height:1px; background:var(--line); }
  .alt { background:none; color:var(--terra); border:1px solid var(--line); }
"""


def _page(*, error: str | None = None, name: str = "", notice: str | None = None) -> str:
    """Server-rendered gate with two doors. Email is the identified path for the
    internal team; the passcode below it is the anonymous path for field testers
    recruited over WhatsApp, who have no company address.

    Everything interpolated here is attacker-controlled -- `name` comes straight
    off the passcode form and `error` embeds the submitted address -- so every
    value is escaped. This closes a pre-existing reflected XSS in the name field.
    """
    # quote=False on the banners: they land in text content, not an attribute,
    # so escaping apostrophes would only mangle copy like "isn't". The `name`
    # value below *is* an attribute and keeps full escaping.
    if notice:
        banner = f'<p class="ok">{escape(notice, quote=False)}</p>'
    elif error:
        banner = f'<p class="err">{escape(error, quote=False)}</p>'
    else:
        intro = ("Sign in with your email, or use the shared passcode." if settings.email_open_signup
                 else "Closed pilot — sign in with an invited email, or use the shared passcode.")
        banner = f'<p class="sub">{intro}</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Join IndieDex</title>
<style>
{_STYLE}
</style>
</head>
<body>
  <div class="card">
    <div class="paw">🐾</div>
    <h1>IndieDex</h1>
    {banner}
    <form method="post" action="/auth/email">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" placeholder="you@example.org" required autocomplete="email">
      <button type="submit">Email me a link</button>
    </form>
    <div class="divider">or</div>
    <form method="post" action="/auth/join">
      <label for="name">Your name</label>
      <input id="name" name="name" value="{escape(name)}" placeholder="e.g. Priya" required autocomplete="name">
      <label for="passcode">Passcode</label>
      <input id="passcode" name="passcode" type="password" placeholder="shared code" required autocomplete="off">
      <button type="submit" class="alt">Join with passcode</button>
    </form>
  </div>
{proof.FORM_SCRIPT}
</body>
</html>"""


def _wants_json(request: Request) -> bool:
    """True when the caller is the app rather than a browser navigating to
    /join. Both paths share these endpoints; only the reply shape differs."""
    return "application/json" in request.headers.get("accept", "")


def _fail(request: Request, *, error: str, status: int, name: str = "", headers=None) -> Response:
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": error}, status_code=status, headers=headers)
    return HTMLResponse(_page(error=error, name=name), status_code=status, headers=headers)


def _limited(request: Request, exc: HTTPException, name: str = "") -> Response:
    return _fail(request, error=str(exc.detail), status=exc.status_code, name=name, headers=exc.headers)


@router.get("/join", response_class=HTMLResponse)
async def join_page() -> HTMLResponse:
    return HTMLResponse(_page())


@router.get("/auth/join/challenge")
async def join_challenge(request: Request, conn=Depends(get_conn)):
    client = client_key(request)
    await check(conn, f"challenge:ip:{client}", Limit(settings.rl_join_challenge_times, 60))
    await check_backoff(conn, f"join:ip:{client}")
    return JSONResponse(proof.issue(client), headers={"Cache-Control": "no-store"})


@router.post("/auth/join")
@audit_failures
async def join_submit(
    request: Request,
    name: str = Form(..., max_length=200),
    passcode: str = Form(..., max_length=512),
    challenge: str = Form("", max_length=1024),
    solution: str = Form("", max_length=12),
    conn=Depends(get_conn),
):
    display_name = name.strip()
    failure_key = f"join:ip:{client_key(request)}"
    try:
        await check_backoff(conn, failure_key)
        try:
            await proof.consume(conn, client_key(request), challenge, solution)
        except HTTPException:
            await check(conn, failure_key, join_limit())
            await record_failure(conn, failure_key)
            raise
    except HTTPException as exc:
        return _limited(request, exc, display_name)
    if not hmac.compare_digest(passcode, settings.join_passcode):
        # Counted here rather than at the top of the handler so the budget is
        # spent by *wrong* guesses only. The threat is someone walking a
        # keyspace against a shared code; a dozen field testers joining from
        # one carrier NAT during an onboarding push is the opposite of that,
        # and a limiter that cannot tell them apart blocks the wrong one.
        try:
            await check(conn, failure_key, join_limit())
            await record_failure(conn, failure_key)
        except HTTPException as exc:
            return _limited(request, exc, display_name)
        return _fail(
            request,
            error="Wrong passcode — check with whoever invited you.",
            status=401,
            name=display_name,
        )
    if not display_name:
        return _fail(request, error="Please enter your name.", status=400)

    await clear_failures(conn, failure_key)
    observer_id = await create_observer(conn, display_name=display_name, created_via="passcode")
    # The app can't follow a 303 into an HTML page -- it wants a verdict and
    # will re-probe its own session. The browser still gets the redirect.
    resp: Response = (
        JSONResponse({"ok": True})
        if _wants_json(request)
        else RedirectResponse(url="/", status_code=303)
    )
    set_session_cookie(resp, observer_id)
    return resp


@router.post("/auth/logout")
async def logout(request: Request):
    """Drop the session. POST-only on purpose: a GET would let a link
    prefetcher, mail scanner, or stray <img> sign someone out.

    Deliberately harmless without a session -- logging out twice, or from a
    dead session, is not an error worth surfacing.
    """
    resp: Response = (
        JSONResponse({"ok": True})
        if _wants_json(request)
        else RedirectResponse(url="/join", status_code=303)
    )
    clear_session_cookie(resp)
    return resp


@router.post("/auth/email")
@audit_failures
async def email_submit(request: Request, email: str = Form(..., max_length=320), conn=Depends(get_conn)):
    failure_key = f"email:ip:{client_key(request)}"
    try:
        await check(conn, failure_key, email_ip_limit())
        await check_backoff(conn, failure_key)
    except HTTPException as exc:
        return _limited(request, exc)
    address = normalize_email(email)
    if not address:
        try:
            await record_failure(conn, failure_key)
        except HTTPException as exc:
            return _limited(request, exc)
        return _fail(request, error="That doesn't look like an email address.", status=400)
    # IP admission is above; address cooldown/window and provider capacity
    # reserve together below, so a busy provider cannot consume inbox credit.
    if not is_allowed(address):
        try:
            await record_failure(conn, failure_key)
        except HTTPException as exc:
            return _limited(request, exc)
        # Told plainly, not silently swallowed: the allowlist is a domain, not
        # a secret, and silent failure just generates "did it send?" pings.
        return _fail(
            request,
            error=f"{address} isn't on the pilot list yet — ask Akash to add you.",
            status=403,
        )
    if await conn.fetchval("SELECT 1 FROM banned_emails WHERE email_hmac = $1", email_hash(address)):
        # Said plainly: this person was banned by a moderator (#180), and a
        # silent failure would only bring them back to try again.
        return _fail(
            request,
            error="This account has been suspended. If you think that's wrong, write to nammaindies@gmail.com.",
            status=403,
        )
    try:
        async with conn.transaction():
            await cooldown(conn, f"email:addr:{address}", settings.auth_email_cooldown_s)
            await check(conn, f"email:addr:{address}", email_addr_limit())
            slot = await reserve(conn)
            observer_id = await get_or_create_observer_by_email(conn, email=address)
            token = await issue_login_token(conn, observer_id)
    except HTTPException as exc:
        await record(conn, "email_throttled")
        return _limited(request, exc)
    link = f"{settings.public_base_url}/auth/email/consume?token={token}"
    try:
        await deliver(conn, lambda: get_sender().send(address, link), reserved=slot)
    except HTTPException as exc:
        # The address cooldown has committed. Never promise a provider retry
        # sooner than this inbox is allowed to request another link.
        wait = max(settings.auth_email_cooldown_s, int((exc.headers or {}).get("Retry-After", "60")))
        message = ("The email provider is busy." if exc.status_code == 429 else "We couldn't send that email.")
        return _fail(request, error=f"{message} Try again in {wait} seconds.", status=exc.status_code,
                     headers={"Retry-After": str(wait)})
    except Exception:
        # Never let a sender failure surface as a bare 500. SES refusing a
        # message (suppressed recipient, throttling, misconfiguration) is an
        # operational problem, and an unhandled traceback makes it look like
        # the button is broken -- which is exactly what happened once, for two
        # days, when sandbox rejected an unverified recipient. The detail goes
        # to the log; the reader gets something actionable.
        logger.error("login email sender failed")
        return _fail(
            request,
            error="We couldn't send that email just now. Try again in a moment — "
                  "if it keeps failing, tell Akash.",
            status=500,
        )
    # The unused token simply expires in 30 minutes, so a failed send leaves
    # nothing to clean up.
    message = f"Check your email — a sign-in link is on its way to {address}."
    if _wants_json(request):
        return JSONResponse({"ok": True, "message": message})
    return HTMLResponse(_page(notice=message))


@router.get("/auth/email/consume")
@audit_failures
async def email_consume(request: Request, token: str = Query(max_length=512), conn=Depends(get_conn)):
    failure_key = f"consume:ip:{client_key(request)}"
    try:
        await check_backoff(conn, failure_key)
    except HTTPException as exc:
        return _limited(request, exc)
    observer_id = await consume_login_token(conn, token)
    if observer_id is None:
        try:
            await record_failure(conn, failure_key)
        except HTTPException as exc:
            return _limited(request, exc)
        return HTMLResponse(
            _page(error="That link has expired or was already used. Request a new one."),
            status_code=401,
        )
    await clear_failures(conn, failure_key)
    # Carry-over happens *here*, not at submit: consuming the link is the first
    # moment we know this person actually controls the address. Testers were
    # told to use their work email as their passcode-door name, so anything
    # logged under that string folds into this now-verified identity.
    email = await conn.fetchval("SELECT email FROM observers WHERE id = $1", observer_id)
    if email:
        await absorb_passcode_observers(conn, target=observer_id, email=email)
    resp = RedirectResponse(url="/", status_code=303)
    set_session_cookie(resp, observer_id)
    return resp
