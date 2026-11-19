"""Public support/contact and configurable links to the published policies."""
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from starlette.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.legal_links import LEGAL_LINKS_STYLE, legal_links_html
from app.routes.join import _STYLE

router = APIRouter()

DOCUMENTS = {
    "terms": ("Terms of Service", "terms_url"),
    "privacy": ("Privacy Policy", "privacy_url"),
    "community-guidelines": ("Community Guidelines", "community_guidelines_url"),
    "grievance": ("Grievances", "grievance_url"),
}


def _page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — IndieDex</title>
<style>{_STYLE}{LEGAL_LINKS_STYLE}
  .contact-details {{ white-space:pre-wrap; overflow-wrap:anywhere; }}
</style>
</head>
<body><main class="card">
  <h1>{title}</h1>
  {body}
  <p><a href="/">Open IndieDex</a> · <a href="/account/delete">Delete your account</a></p>
  {legal_links_html()}
</main></body>
</html>""", status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/support", response_class=HTMLResponse)
async def support_page() -> HTMLResponse:
    contact = ""
    if settings.support_contact_name:
        contact += f"<p>{escape(settings.support_contact_name)}</p>"
    if settings.support_contact_email:
        email = settings.support_contact_email
        contact += f'<p>Email <a href="mailto:{quote(email, safe="@")}">{escape(email)}</a></p>'
    if settings.support_contact_details:
        contact += f'<p class="contact-details">{escape(settings.support_contact_details)}</p>'
    if not settings.support_contact_email and not settings.support_contact_details:
        contact += "<p>Support contact details have not been configured yet. Please check back.</p>"
    return _page("Support", "<p>Get help with IndieDex, your account, or a problem in the app.</p>" + contact)


@router.get("/policies/{document}")
async def published_policy(document: str):
    if document not in DOCUMENTS:
        raise HTTPException(404, "not found")
    title, setting = DOCUMENTS[document]
    target = getattr(settings, setting)
    if target:
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})
    return _page(title, f"<p>The {title} link has not been configured yet. Please check back.</p>", status=503)
