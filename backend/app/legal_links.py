"""Shared navigation only; the legal documents live on the public website."""

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
