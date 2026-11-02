"""The two mails about deleting an account (#179): the link that confirms a
request made on the web, and the notice that deletion is scheduled.

Shaped like the sign-in mail (app/email/sender.py): say who we are and why
the mail came, and show any link as readable text.
"""
from dataclasses import dataclass
from datetime import datetime
from html import escape

from app.email.sender import get_sender

FOOTER_TEXT = ("--\nIndieDex is the street-dog field guide built by Namma Indies\n"
               "in Bangalore. https://nammaindies.org\n")
FOOTER_HTML = ('<hr style="border:0;border-top:1px solid #d8c4a6;margin:20px 0">'
               '<p style="color:#6b5844;font-size:13px">IndieDex is the street-dog field guide built by '
               'Namma Indies in Bangalore. <a href="https://nammaindies.org" style="color:#c15f3c">'
               'nammaindies.org</a></p>')
STYLE = 'font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#2e2016;max-width:520px'


@dataclass(frozen=True)
class Message:
    subject: str
    text: str
    html: str


def deletion_link(link: str) -> Message:
    text = ("Someone asked to delete the IndieDex account that uses this email address.\n"
            "If that was you, open this link to confirm:\n\n"
            f"{link}\n\n"
            "It works once and expires in 30 minutes. Nothing is deleted until you confirm.\n\n"
            "If it wasn't you, nothing has happened and you can ignore this.\n\n" + FOOTER_TEXT)
    html = (f'<div style="{STYLE}"><p>Someone asked to delete the <strong>IndieDex</strong> account that '
            "uses this email address. If that was you, open this link to confirm:</p>"
            f'<p style="word-break:break-all"><a href="{escape(link)}" style="color:#c15f3c">{escape(link)}</a></p>'
            '<p style="color:#6b5844;font-size:14px">It works once and expires in 30 minutes. Nothing is '
            "deleted until you confirm. If it wasn't you, nothing has happened and you can ignore this.</p>"
            f"{FOOTER_HTML}</div>")
    return Message("Confirm deleting your IndieDex account", text, html)


def deletion_scheduled(when: datetime) -> Message:
    day = f"{when:%d %B %Y}"
    text = (f"Your IndieDex account is scheduled to be deleted on {day}.\n\n"
            "Until then your sightings are hidden from everyone. After that date your photos, sightings,\n"
            "voice notes, name and email address are deleted for good.\n\n"
            "To keep your account, sign in to the IndieDex app before then and tap \"Keep my account\".\n"
            "If you didn't ask for this, do that, and tell us at nammaindies@gmail.com.\n\n" + FOOTER_TEXT)
    html = (f'<div style="{STYLE}"><p>Your <strong>IndieDex</strong> account is scheduled to be deleted on '
            f"<strong>{day}</strong>.</p><p>Until then your sightings are hidden from everyone. After that date "
            "your photos, sightings, voice notes, name and email address are deleted for good.</p>"
            "<p>To keep your account, sign in to the IndieDex app before then and tap "
            "<strong>Keep my account</strong>. If you didn't ask for this, do that, and tell us at "
            '<a href="mailto:nammaindies@gmail.com" style="color:#c15f3c">nammaindies@gmail.com</a>.</p>'
            f"{FOOTER_HTML}</div>")
    return Message("Your IndieDex account will be deleted", text, html)


async def send(to: str, message: Message) -> None:
    await get_sender().send_message(to, message.subject, message.text, message.html)

