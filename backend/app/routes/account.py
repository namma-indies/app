"""Deleting your account, in the app and on the web (#179).

IN THE APP (signed in)
  GET  /account                  whether deletion is scheduled, when, how much goes
  POST /account/delete           schedule it; the body must say {"confirm": "delete"}
  POST /account/delete/cancel    keep it, while it is still only scheduled

Signed in is the re-authentication (Q2): the app asks the person to type
"delete", and a notice is mailed to the address, saying when it goes and how to
keep it. A stolen phone that deletes an account gives its owner the grace
period and that mail to undo it.

ON THE WEB (Google Play's deletion-request page, no session)
  GET  /account/delete           a form asking for the address
  POST /account/delete/request   mails a link, if an account uses the address
  GET  /account/delete/confirm   shows what will go -- deletes nothing
  POST /account/delete/confirm   schedules it, exactly as the app does

Proving control of the address is the authentication. The answer to a request
is the same whether or not an account uses the address, so the page cannot be
used to find out who has one; nothing here creates an account or consults the
sign-up allowlist. Following the link only shows a page: a mail scanner that
opens every link it sees must not delete anybody, so only the button does. A
deletion link cannot sign anyone in, and a sign-in link cannot delete
(`login_tokens.purpose`).
"""
import logging
from datetime import datetime
from html import escape
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app import accounts
from app.auth.allowlist import normalize_email
from app.auth.deps import require_account
from app.auth.logintoken import consume, issue, peek
from app.config import settings
from app.deps import get_conn
from app.email.account_mail import deletion_link, deletion_scheduled, send
from app.ratelimit import check, client_key, email_addr_limit, email_ip_limit
from app.routes.join import _STYLE

logger = logging.getLogger(__name__)
router = APIRouter()

CONTACT = "nammaindies@gmail.com"


# --- in the app ---------------------------------------------------------------

class DeleteBody(BaseModel):
    confirm: str


@router.get("/account")
async def get_account(observer_id: UUID = Depends(require_account), conn=Depends(get_conn)):
    return await accounts.state(conn, observer_id)


async def _notify(conn, observer_id: UUID, when: datetime) -> None:
    """The notice that deletion is scheduled. Best effort: deletion never
    waits on the mail, and a person with no address gets the app's screen."""
    email = await conn.fetchval("SELECT email FROM observers WHERE id=$1", observer_id)
    if not email:
        return
    try:
        await send(email, deletion_scheduled(when))
    except Exception:
        logger.warning("deletion notice not sent for observer=%s", observer_id)


@router.post("/account/delete")
async def delete_account(body: DeleteBody, observer_id: UUID = Depends(require_account), conn=Depends(get_conn)):
    if body.confirm.strip().lower() != "delete":
        raise HTTPException(422, 'type "delete" to confirm')
    async with conn.transaction():
        scheduled = await accounts.request(conn, observer_id, "app")
    if scheduled is None:
        raise HTTPException(401)
    when, new = scheduled
    if new:
        await _notify(conn, observer_id, when)
    return await accounts.state(conn, observer_id)


@router.post("/account/delete/cancel")
async def cancel_deletion(observer_id: UUID = Depends(require_account), conn=Depends(get_conn)):
    async with conn.transaction():
        kept = await accounts.cancel(conn, observer_id)
    if not kept and (await accounts.state(conn, observer_id))["status"] != "active":
        raise HTTPException(409, "deleting this account has already started")
    return await accounts.state(conn, observer_id)


# --- on the web ---------------------------------------------------------------

def _html(body: str, *, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>Delete your IndieDex account</title>
<style>
{_STYLE}
  .danger {{ background:#a33a24; }}
  ul {{ padding-left:1.1rem; margin:.4rem 0 1.2rem; font-size:.92rem; }}
  .small {{ color:#6b5844; font-size:.82rem; }}
</style>
</head>
<body>
  <div class="card">
    <div class="paw">🐾</div>
    <h1>Delete your account</h1>
    {body}
  </div>
</body>
</html>""", status_code=status)


def _what_goes() -> str:
    days = settings.account_deletion_grace_days
    return f"""<p class="sub">This deletes your IndieDex account and everything you logged with it.</p>
    <ul>
      <li><strong>Deleted:</strong> your photos and videos, the sightings and dogs you logged, your voice
        notes and their words, your reports, and your name and email address.</li>
      <li><strong>Kept, without your name:</strong> dogs other people logged too stay, with their photos.
        Your "same dog / different dog" answers on other people's photos are kept without your name.</li>
      <li><strong>When:</strong> your sightings are hidden from everyone at once. Everything is deleted
        {days} days later. Until then you can sign in to the app and keep your account.</li>
    </ul>"""


@router.get("/account/delete", response_class=HTMLResponse)
async def web_form() -> HTMLResponse:
    return _html(_what_goes() + f"""
    <form method="post" action="/account/delete/request">
      <label for="email">The email address you sign in with</label>
      <input id="email" name="email" type="email" required autocomplete="email">
      <button type="submit" class="danger">Email me a deletion link</button>
    </form>
    <p class="small">Signed up without an email address? Delete your account from inside the app, or write
      to <a href="mailto:{CONTACT}">{CONTACT}</a>.</p>""")


@router.post("/account/delete/request", response_class=HTMLResponse)
async def web_request(request: Request, email: str = Form(...), conn=Depends(get_conn)) -> HTMLResponse:
    address = normalize_email(email)
    if not address:
        return _html('<p class="err">That doesn\'t look like an email address.</p>'
                     '<p><a href="/account/delete">Try again</a></p>', status=400)
    await check(conn, f"delete:ip:{client_key(request)}", email_ip_limit())
    await check(conn, f"delete:addr:{address}", email_addr_limit())
    observer_id = await conn.fetchval(
        "SELECT id FROM observers WHERE email=$1 AND deleted_at IS NULL", address)
    if observer_id is not None:
        token = await issue(conn, observer_id, purpose="delete")
        try:
            await send(address, deletion_link(f"{settings.public_base_url}/account/delete/confirm?token={token}"))
        except Exception:
            # The same page either way: an error here, and only here, would say
            # that an account uses this address.
            logger.exception("deletion link not sent")
    return _html(f"""<p class="ok">If an IndieDex account uses {escape(address, quote=False)}, we've emailed
      it a link to confirm deleting it. The link works once, for 30 minutes.</p>
      <p class="small">Nothing is deleted until you open the link and confirm.</p>""")


async def _counts(conn, observer_id: UUID) -> dict:
    return (await accounts.state(conn, observer_id))["counts"]


@router.get("/account/delete/confirm", response_class=HTMLResponse)
async def web_confirm_page(token: str, conn=Depends(get_conn)) -> HTMLResponse:
    observer_id = await peek(conn, token, purpose="delete")
    if observer_id is None:
        return _html('<p class="err">That link has expired or was already used.</p>'
                     '<p><a href="/account/delete">Ask for a new one</a></p>', status=401)
    n = await _counts(conn, observer_id)
    return _html(_what_goes() + f"""
    <p class="sub">Your account has {n['sightings']} sightings from {n['uploads']} uploads, and
      {n['voice_notes']} added voice notes.</p>
    <form method="post" action="/account/delete/confirm">
      <input type="hidden" name="token" value="{escape(token)}">
      <button type="submit" class="danger">Delete my account</button>
    </form>""")


@router.post("/account/delete/confirm", response_class=HTMLResponse)
async def web_confirm(token: str = Form(...), conn=Depends(get_conn)) -> HTMLResponse:
    async with conn.transaction():
        observer_id = await consume(conn, token, purpose="delete")
        scheduled = await accounts.request(conn, observer_id, "web") if observer_id else None
    if scheduled is None:
        return _html('<p class="err">That link has expired or was already used.</p>'
                     '<p><a href="/account/delete">Ask for a new one</a></p>', status=401)
    when, new = scheduled
    if new:
        await _notify(conn, observer_id, when)
    return _html(f"""<p class="ok">Your account will be deleted on {when:%d %B %Y}.</p>
      <p class="sub">Your sightings are hidden from everyone now. To keep your account, sign in to the
        IndieDex app before then and tap <strong>Keep my account</strong>.</p>""")
