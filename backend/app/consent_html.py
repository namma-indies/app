"""The same explicit agreement in the server-rendered sign-up forms."""
from html import escape

from app.config import settings

CONSENT_STYLE = """
  .consent-fields { margin:16px 0; padding:12px; border:1px solid var(--line); border-radius:10px; }
  .consent-fields legend { font-size:.85rem; font-weight:600; }
  .consent-fields label { display:flex; gap:8px; align-items:flex-start; margin:10px 0; font-weight:400; }
  .consent-fields input[type=checkbox] { width:auto; margin:3px 0 0; flex-shrink:0; }
  .consent-fields a { color:var(--terra-dark); }
"""


def consent_fields(prefix: str) -> str:
    if not settings.consent_enabled:
        return ""
    hidden = "".join(
        f'<input type="hidden" name="{name}" value="{escape(value)}">'
        for name, value in (
            ("terms_version", settings.terms_version),
            ("privacy_version", settings.privacy_version),
            ("community_guidelines_version", settings.community_guidelines_version),
        )
    )
    age = ""
    if settings.consent_minimum_age is not None:
        age = f"""<input type="hidden" name="minimum_age" value="{settings.consent_minimum_age}">
        <label for="{prefix}-age"><input id="{prefix}-age" type="checkbox" name="age_confirmed" value="true" required>
          <span>I am at least {settings.consent_minimum_age} years old.</span></label>"""
    return f"""<fieldset class="consent-fields"><legend>Before creating your account</legend>
      {hidden}
      <label for="{prefix}-terms"><input id="{prefix}-terms" type="checkbox" name="agree_terms_and_guidelines" value="true" required>
        <span>I agree to the <a href="/policies/terms" target="_blank" rel="noreferrer">Terms of Service</a>
          and <a href="/policies/community-guidelines" target="_blank" rel="noreferrer">Community Guidelines</a>.</span></label>
      <label for="{prefix}-privacy"><input id="{prefix}-privacy" type="checkbox" name="acknowledge_privacy" value="true" required>
        <span>I acknowledge the <a href="/policies/privacy" target="_blank" rel="noreferrer">Privacy Policy</a>.</span></label>
      {age}
    </fieldset>"""


def email_signup_fields(signup: bool) -> str:
    if not settings.consent_enabled:
        return ""
    if signup:
        return ('<input type="hidden" name="create_account" value="true">' + consent_fields("email")
                + '<p><a href="/join">Sign in with an existing account</a></p>')
    return ('<input type="hidden" name="create_account" value="false">'
            '<p>New here? <a href="/join?signup=1">Create an account</a>.</p>')
