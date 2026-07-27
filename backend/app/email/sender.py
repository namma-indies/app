from typing import Protocol

import aioboto3

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
