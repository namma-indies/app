import hmac
from html import escape

from fastapi import APIRouter, Depends, Form
from starlette.responses import HTMLResponse, RedirectResponse

from app.auth.allowlist import is_allowed, normalize_email
from app.auth.deps import set_session_cookie
from app.auth.email_login import get_or_create_observer_by_email
from app.auth.logintoken import consume as consume_login_token
from app.auth.logintoken import issue as issue_login_token
from app.auth.magiclink import create_observer
from app.config import settings
from app.deps import get_conn
from app.email.sender import get_sender

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
        banner = '<p class="sub">Closed pilot — sign in with your work email, or use the shared passcode.</p>'

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
      <label for="email">Work email</label>
      <input id="email" name="email" type="email" placeholder="you@dognosis.tech" required autocomplete="email">
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
</body>
</html>"""


@router.get("/join", response_class=HTMLResponse)
async def join_page() -> HTMLResponse:
    return HTMLResponse(_page())


@router.post("/auth/join")
async def join_submit(
    name: str = Form(...),
    passcode: str = Form(...),
    conn=Depends(get_conn),
):
    display_name = name.strip()
    if not hmac.compare_digest(passcode, settings.join_passcode):
        return HTMLResponse(
            _page(error="Wrong passcode — check with whoever invited you.", name=display_name),
            status_code=401,
        )
    if not display_name:
        return HTMLResponse(_page(error="Please enter your name.", name=""), status_code=400)

    observer_id = await create_observer(conn, display_name=display_name, created_via="passcode")
    resp = RedirectResponse(url="/", status_code=303)
    set_session_cookie(resp, observer_id)
    return resp


@router.post("/auth/email")
async def email_submit(email: str = Form(...), conn=Depends(get_conn)):
    address = normalize_email(email)
    if not address:
        return HTMLResponse(_page(error="That doesn't look like an email address."), status_code=400)
    if not is_allowed(address):
        # Told plainly, not silently swallowed: the allowlist is a domain, not
        # a secret, and silent failure just generates "did it send?" pings.
        return HTMLResponse(
            _page(error=f"{address} isn't on the pilot list yet — ask Akash to add you."),
            status_code=403,
        )
    observer_id = await get_or_create_observer_by_email(conn, email=address)
    token = await issue_login_token(conn, observer_id)
    link = f"{settings.public_base_url}/auth/email/consume?token={token}"
    await get_sender().send(address, link)
    return HTMLResponse(_page(notice=f"Check your email — a sign-in link is on its way to {address}."))


@router.get("/auth/email/consume")
async def email_consume(token: str, conn=Depends(get_conn)):
    observer_id = await consume_login_token(conn, token)
    if observer_id is None:
        return HTMLResponse(
            _page(error="That link has expired or was already used. Request a new one."),
            status_code=401,
        )
    resp = RedirectResponse(url="/", status_code=303)
    set_session_cookie(resp, observer_id)
    return resp
