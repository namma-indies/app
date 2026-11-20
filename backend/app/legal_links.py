"""Shared navigation only; the legal documents live on the public website."""
from app.config import settings

LEGAL_LINKS = (
    ("/policies/terms", "Terms of Service"),
    ("/policies/community-guidelines", "Community Guidelines"),
    ("/policies/privacy", "Privacy Policy"),
    ("/policies/grievance", "Grievances"),
    ("/support", "Support"),
)

LEGAL_LINKS_STYLE = """
  .policy-links { display:flex; flex-wrap:wrap; gap:8px 14px; margin-top:24px;
                  padding-top:16px; border-top:1px solid var(--line); font-size:.82rem; }
  .policy-links a { color:var(--terra-dark); }
"""


def legal_links_html() -> str:
    links = " ".join(
        f'<a href="{path}" target="_blank" rel="noreferrer">{label}</a>'
        for path, label in LEGAL_LINKS
    )
    return f'<nav class="policy-links" aria-label="Policies and support">{links}</nav>'


def support_contact_phrase() -> str:
    """How to reach support, in plain text for an error message: the
    configured inbox, or the public /support page when none is set."""
    if settings.support_contact_email:
        return f"write to {settings.support_contact_email}"
    return "contact us through the Support page"
