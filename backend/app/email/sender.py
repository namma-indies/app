from typing import Protocol

import httpx

from app.config import settings

SUBJECT = "Your IndieDex sign-in link"


def _bodies(link: str) -> tuple[str, str]:
    text = (
        "Tap to sign in to IndieDex:\n\n"
        f"{link}\n\n"
        "This link works once and expires in 30 minutes.\n"
        "If you didn't ask for it, you can ignore this email."
    )
    html = (
        '<div style="font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#2e2016">'
        "<p>Tap to sign in to IndieDex:</p>"
        f'<p><a href="{link}" style="background:#c15f3c;color:#fff;padding:12px 20px;'
        'border-radius:10px;text-decoration:none;display:inline-block">Sign in</a></p>'
        "<p style=\"color:#6b5844;font-size:14px\">This link works once and expires in "
        "30 minutes. If you didn't ask for it, you can ignore this email.</p>"
        "</div>"
    )
    return text, html


class LoginEmail(Protocol):
    async def send(self, to: str, link: str) -> None: ...


class ConsoleSender:
    """Used whenever RESEND_API_KEY is unset -- local dev and tests. Prints the
    link instead of sending, so the flow is exercisable without a mail account."""

    async def send(self, to: str, link: str) -> None:
        print(f"[login-email] to={to} link={link}", flush=True)


class ResendSender:
    async def send(self, to: str, link: str) -> None:
        text, html = _bodies(link)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={
                    "from": settings.email_from,
                    "to": [to],
                    "subject": SUBJECT,
                    "text": text,
                    "html": html,
                },
            )
            resp.raise_for_status()


def get_sender() -> LoginEmail:
    return ResendSender() if settings.resend_api_key else ConsoleSender()
