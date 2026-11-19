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
from app.auth.email_login import absorb_passcode_observers
from app.auth.logintoken import issue as issue_login_token
from app.auth import proof
from app.auth.audit import audit_failures
from app.config import settings
from app.consent import (ConsentRequired, ConsentSubmission, consent_form, consume_email_token,
                         current_policy, email_account, passcode_account)
from app.consent_html import CONSENT_STYLE, consent_fields, email_signup_fields
from app.deps import get_conn
from app.email.sender import get_sender
from app.email.delivery import check_capacity, deliver, reserve
from app.abuse import record
from app.legal_links import LEGAL_LINKS_STYLE, legal_links_html, support_contact_phrase
from app.ratelimit import (Limit, check, check_backoff, clear_failures, client_key, consume_limit,
                           cooldown, email_addr_limit, email_ip_day_limit, email_ip_limit, join_global_limit, join_limit,
                           join_success_limit, record_failure, refund)
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


def _page(*, error: str | None = None, name: str = "", notice: str | None = None,
          email: str = "", signup: bool = False) -> str:
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
{LEGAL_LINKS_STYLE}
{CONSENT_STYLE}
</style>
</head>
<body>
  <div class="card">
    <div class="paw">🐾</div>
    <h1>IndieDex</h1>
    {banner}
    <form method="post" action="/auth/email">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" value="{escape(email)}" placeholder="you@example.org" required autocomplete="email">
      {email_signup_fields(signup)}
      <button type="submit">Email me a link</button>
    </form>
    <div class="divider">or</div>
    <form method="post" action="/auth/join">
      <label for="name">Your name</label>
      <input id="name" name="name" value="{escape(name)}" placeholder="e.g. Priya" required autocomplete="name">
      <label for="passcode">Passcode</label>
      <input id="passcode" name="passcode" type="password" placeholder="shared code" required autocomplete="off">
      {consent_fields("passcode")}
      <button type="submit" class="alt">Join with passcode</button>
    </form>
    {legal_links_html()}
  </div>
{proof.FORM_SCRIPT}
</body>
</html>"""


def _wants_json(request: Request) -> bool:
    """True when the caller is the app rather than a browser navigating to
    /join. Both paths share these endpoints; only the reply shape differs."""
    return "application/json" in request.headers.get("accept", "")


def _fail(request: Request, *, error: str, status: int, name: str = "", email: str = "",
          signup: bool = False, consent_required: bool = False, headers=None) -> Response:
    headers = {"Cache-Control": "no-store", **(headers or {})}
    if _wants_json(request):
        extra = {"consent_required": True, "consent": current_policy()} if consent_required else {}
        return JSONResponse({"ok": False, "error": error, **extra}, status_code=status, headers=headers)
    return HTMLResponse(_page(error=error, name=name, email=email, signup=signup), status_code=status, headers=headers)


def _limited(request: Request, exc: HTTPException, name: str = "") -> Response:
    return _fail(request, error=str(exc.detail), status=exc.status_code, name=name, headers=exc.headers)


def _email_sent(request: Request, message: str) -> Response:
    if _wants_json(request):
        return JSONResponse({"ok": True, "message": message}, headers={"Cache-Control": "no-store"})
    return HTMLResponse(_page(notice=message), headers={"Cache-Control": "no-store"})


@router.get("/join", response_class=HTMLResponse)
async def join_page(signup: bool = False) -> HTMLResponse:
    return HTMLResponse(_page(signup=signup), headers={"Cache-Control": "no-store"})


@router.get("/auth/join/challenge")
async def join_challenge(request: Request, conn=Depends(get_conn)):
    client = client_key(request)
    await check(conn, f"challenge:ip:{client}", Limit(settings.rl_join_challenge_times, 60))
    await check_backoff(conn, f"join:ip:{client}")
    return JSONResponse(proof.issue(client), headers={"Cache-Control": "no-store"})


_GLOBAL_GUESSES = "join:wrong-passcode:global"


async def _charge_guess(conn, failure_key: str) -> None:
    """Spend one unit of this source's budget and one of the whole door's.

    The global budget is the backstop against a guessing run spread over
    many addresses: once wrong passcodes exhaust it, the passcode door is
    shut for the rest of its window for everyone (email sign-in is
    unaffected), and moderators see it on the abuse-signals screen."""
    async with conn.transaction():
        await check(conn, failure_key, join_limit())
        try:
            hits = await check(conn, _GLOBAL_GUESSES, join_global_limit())
        except HTTPException as exc:
            closed = exc
        else:
            if hits == settings.rl_join_global_wrong_times:
                logger.warning("passcode door: global wrong-passcode budget reached; "
                               "passcode joins are refused until the window ends")
            return
    # Outside the rolled-back transaction, so the signal itself survives.
    await record(conn, "passcode_lockouts")
    raise HTTPException(429, "Passcode sign-in is paused for now. Sign in by email, or try again in "
                        f"{closed.headers['Retry-After']} seconds.", headers=closed.headers)


@router.post("/auth/join")
@audit_failures
async def join_submit(
    request: Request,
    name: str = Form(..., max_length=200),
    passcode: str = Form(..., max_length=512),
    challenge: str = Form("", max_length=1024),
    solution: str = Form("", max_length=12),
    consent: ConsentSubmission = Depends(consent_form),
    conn=Depends(get_conn),
):
    display_name = name.strip()
    client = client_key(request)
    failure_key = f"join:ip:{client}"
    try:
        await check_backoff(conn, failure_key)
        try:
            await proof.consume(conn, client, challenge, solution)
        except proof.Stale:
            # Expired, issued to another address, or already spent: ask for
            # a new proof, but nothing here looks like guessing.
            raise
        except HTTPException:
            await check(conn, failure_key, join_limit())
            await record_failure(conn, failure_key, after=settings.auth_ip_backoff_after)
            raise
        # Both budgets are charged, atomically, *before* the compare: N
        # parallel guesses each need a budget unit, so they cannot all slip
        # between a read and a write. A correct passcode gets its charge back
        # below, so the budgets are still spent by wrong guesses only -- a
        # dozen field testers joining from one carrier NAT is not an attack.
        await _charge_guess(conn, failure_key)
    except HTTPException as exc:
        return _limited(request, exc, display_name)
    if not hmac.compare_digest(passcode.encode(), settings.join_passcode.encode()):
        try:
            await record_failure(conn, failure_key, after=settings.auth_ip_backoff_after)
        except HTTPException as exc:
            return _limited(request, exc, display_name)
        return _fail(
            request,
            error="Wrong passcode — check with whoever invited you.",
            status=401,
            name=display_name,
        )
    await refund(conn, failure_key, join_limit())
    await refund(conn, _GLOBAL_GUESSES, join_global_limit())
    if not display_name:
        return _fail(request, error="Please enter your name.", status=400)

    await clear_failures(conn, failure_key)
    success_key = f"join-ok:ip:{client}"
    try:
        # Correct passcodes are not free either: a leaked code must not let
        # one source mint accounts without end.
        await check(conn, success_key, join_success_limit())
    except HTTPException as exc:
        return _limited(request, exc, display_name)
    try:
        observer_id = await passcode_account(conn, display_name, consent)
    except ConsentRequired as exc:
        await refund(conn, success_key, join_success_limit())
        return _fail(request, error=exc.detail, status=428, name=display_name, consent_required=True)
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


async def _admit_email(request: Request, conn, email: str, create_account: bool, consent: ConsentSubmission):
    """Everything before the send. Returns a reply to give, or what to send."""
    client = client_key(request)
    failure_key = f"email:ip:{client}"
    try:
        await check(conn, failure_key, email_ip_limit())
        await check(conn, f"email-day:ip:{client}", email_ip_day_limit())
        await check_backoff(conn, failure_key)
    except HTTPException as exc:
        return _limited(request, exc)
    address = normalize_email(email)
    if not address:
        try:
            await record_failure(conn, failure_key, after=settings.auth_ip_backoff_after)
        except HTTPException as exc:
            return _limited(request, exc)
        return _fail(request, error="That doesn't look like an email address.", status=400)
    # IP admission is above; address cooldown/window and provider capacity
    # reserve together below, so a busy provider cannot consume inbox credit.
    if not is_allowed(address):
        # Not a failed sign-in: a person on a carrier NAT typing an address
        # that is not on the list yet must not lock out everyone behind it.
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
            error=f"This account has been suspended. If you think that's wrong, {support_contact_phrase()}.",
            status=403,
        )
    try:
        async with conn.transaction():
            await cooldown(conn, f"email:addr:{address}", settings.auth_email_cooldown_s)
            await check(conn, f"email:addr:{address}", email_addr_limit())
            # Admission is checked before the account lookup so an address
            # with no account gets the same throttling and provider-busy
            # answers as one with -- but only an address with an account
            # reserves (spends) real provider budget.
            await check_capacity(conn, "signin")
            observer_id, pending = await email_account(conn, address, consent, creating=create_account)
            slot = token = None
            if observer_id is not None:
                slot = await reserve(conn, "signin")
                token = await issue_login_token(conn, observer_id,
                                                signup_consent=pending.model_dump_json() if pending else None)
    except ConsentRequired as exc:
        return _fail(request, error=exc.detail, status=428, email=address, signup=True, consent_required=True)
    except HTTPException as exc:
        await record(conn, "email_throttled")
        return _limited(request, exc)
    return address, observer_id, token, slot


@router.post("/auth/email")
@audit_failures
async def email_submit(request: Request, email: str = Form(..., max_length=320), create_account: bool = Form(False),
                       consent: ConsentSubmission = Depends(consent_form)):
    # The pool, not a request-long connection: the send below may wait on the
    # provider's rate or retry, and must not hold a connection meanwhile.
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        admitted = await _admit_email(request, conn, email, create_account, consent)
    if isinstance(admitted, Response):
        return admitted
    address, observer_id, token, slot = admitted
    message = ("Check your email. If an account uses that address, a sign-in link is on its way."
               if settings.consent_enabled else f"Check your email — a sign-in link is on its way to {address}.")
    if observer_id is None:
        return _email_sent(request, message)
    link = f"{settings.public_base_url}/auth/email/consume?token={token}"
    try:
        await deliver(pool, lambda: get_sender().send(address, link), reserved=slot)
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
    return _email_sent(request, message)


@router.get("/auth/email/consume")
@audit_failures
async def email_consume(request: Request, token: str = Query(max_length=512), conn=Depends(get_conn)):
    # Tokens are high-entropy, so guessing is hopeless and failure backoff
    # would only punish everyone behind the same carrier NAT. A plain
    # per-source window bounds the load.
    try:
        await check(conn, f"consume:ip:{client_key(request)}", consume_limit())
    except HTTPException as exc:
        return _limited(request, exc)
    observer_id = await consume_email_token(conn, token)
    if observer_id is None:
        return HTMLResponse(
            _page(error="That link has expired or was already used. Request a new one."),
            status_code=401,
        )
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
