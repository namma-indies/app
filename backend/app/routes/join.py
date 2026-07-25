import hmac

from fastapi import APIRouter, Depends, Form
from starlette.responses import HTMLResponse, RedirectResponse

from app.auth.deps import set_session_cookie
from app.auth.magiclink import create_observer
from app.config import settings
from app.deps import get_conn

router = APIRouter()


def _page(*, error: str | None = None, name: str = "") -> str:
    """Server-rendered closed-pilot gate. Self-contained so it needs no
    frontend build; styled to match IndieDex's warm field-guide skin."""
    banner = (
        f'<p class="err">{error}</p>' if error else '<p class="sub">Closed pilot — enter the shared passcode to join.</p>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Join IndieDex</title>
<style>
  :root {{ --terra:#c15f3c; --terra-dark:#a44a2b; --cream:#f4ead9; --ink:#2e2016; --line:#d8c4a6; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; min-height:100dvh; display:grid; place-items:center; padding:24px;
         background:var(--cream); color:var(--ink);
         font:16px/1.5 ui-rounded,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  .card {{ width:100%; max-width:360px; background:#fffdf8; border:1px solid var(--line);
          border-radius:16px; padding:28px 24px; box-shadow:0 8px 30px rgba(46,32,22,.12); }}
  h1 {{ margin:0 0 2px; font-size:1.4rem; letter-spacing:.02em; }}
  .paw {{ font-size:1.6rem; }}
  .sub {{ margin:.2rem 0 1.3rem; color:#6b5844; font-size:.92rem; }}
  .err {{ margin:.2rem 0 1.3rem; color:var(--terra-dark); font-size:.92rem; font-weight:600; }}
  label {{ display:block; font-size:.8rem; font-weight:600; color:#6b5844; margin:0 0 6px; }}
  input {{ width:100%; padding:12px 14px; margin-bottom:16px; font-size:1rem;
          border:1px solid var(--line); border-radius:10px; background:#fff; color:var(--ink); }}
  input:focus {{ outline:2px solid var(--terra); border-color:var(--terra); }}
  button {{ width:100%; padding:13px; font-size:1rem; font-weight:700; color:#fff; cursor:pointer;
           background:var(--terra); border:0; border-radius:10px; }}
  button:active {{ background:var(--terra-dark); }}
</style>
</head>
<body>
  <form class="card" method="post" action="/auth/join">
    <div class="paw">🐾</div>
    <h1>IndieDex</h1>
    {banner}
    <label for="name">Your name</label>
    <input id="name" name="name" value="{name}" placeholder="e.g. Priya" required autocomplete="name">
    <label for="passcode">Passcode</label>
    <input id="passcode" name="passcode" type="password" placeholder="shared code" required autocomplete="off">
    <button type="submit">Join IndieDex</button>
  </form>
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
