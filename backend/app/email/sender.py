from typing import Protocol

import aioboto3

from app.config import settings

SUBJECT = "Your IndieDex sign-in link"


def _bodies(link: str) -> tuple[str, str]:
    """Text and HTML for the sign-in mail.

    Shaped deliberately against spam filtering. A terse message whose only
    content is a prominent link, from a domain with no sending history, is the
    exact silhouette of credential-harvesting mail -- and Google Workspace
    filed our first one as spam. So: name the project and the organisation
    behind it, say why the mail arrived, and show the destination URL as
    readable text instead of hiding it behind a button. Reputation does the
    heavy lifting over time; this stops us looking like a phish meanwhile.
    """
    text = (
        "Someone asked to sign in to IndieDex with this email address.\n"
        "If that was you, here is your link:\n\n"
        f"{link}\n\n"
        "It works once and expires in 30 minutes.\n\n"
        "If it wasn't you, nothing has happened and you can ignore this --\n"
        "the link expires on its own.\n\n"
        "--\n"
        "IndieDex is the street-dog field guide built by Namma Indies\n"
        "in Bangalore. https://nammaindies.org\n"
    )
    html = (
        '<div style="font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;'
        'color:#2e2016;max-width:520px">'
        "<p>Someone asked to sign in to <strong>IndieDex</strong> with this email "
        "address. If that was you, here is your link:</p>"
        f'<p><a href="{link}" style="background:#c15f3c;color:#fff;padding:12px 20px;'
        'border-radius:10px;text-decoration:none;display:inline-block">Sign in to '
        "IndieDex</a></p>"
        '<p style="color:#6b5844;font-size:14px">Or paste this into your browser:<br>'
        f'<span style="word-break:break-all">{link}</span></p>'
        '<p style="color:#6b5844;font-size:14px">It works once and expires in 30 '
        "minutes. If it wasn't you, nothing has happened and you can ignore this "
        "&mdash; the link expires on its own.</p>"
        '<hr style="border:0;border-top:1px solid #d8c4a6;margin:20px 0">'
        '<p style="color:#6b5844;font-size:13px">IndieDex is the street-dog field '
        "guide built by Namma Indies in Bangalore. "
        '<a href="https://nammaindies.org" style="color:#c15f3c">nammaindies.org</a></p>'
        "</div>"
    )
    return text, html


class LoginEmail(Protocol):
    async def send(self, to: str, link: str) -> None: ...


class ConsoleSender:
    """The default. Prints the link instead of sending, so the whole sign-in
    flow is exercisable in dev and in tests without touching SES -- and so a
    misconfigured deploy fails loudly-but-harmlessly rather than mailing people."""

    async def send(self, to: str, link: str) -> None:
        print(f"[login-email] to={to} link={link}", flush=True)


class SesSender:
    """Amazon SES v2. Credentials come from the ambient AWS chain (env vars on
    the box), not from settings -- the same NI-account IAM user that already
    holds the S3 photo-bucket keys, with `ses:SendEmail` added."""

    async def send(self, to: str, link: str) -> None:
        text, html = _bodies(link)
        session = aioboto3.Session()
        async with session.client("sesv2", region_name=settings.ses_region) as client:
            await client.send_email(
                FromEmailAddress=settings.email_from,
                Destination={"ToAddresses": [to]},
                Content={
                    "Simple": {
                        "Subject": {"Data": SUBJECT},
                        "Body": {
                            "Text": {"Data": text},
                            "Html": {"Data": html},
                        },
                    }
                },
            )


def get_sender() -> LoginEmail:
    """Anything other than an explicit "ses" means console. A typo in
    EMAIL_SENDER must not resolve to "send real mail to real people"."""
    return SesSender() if settings.email_sender == "ses" else ConsoleSender()
