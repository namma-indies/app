import pytest

from app.email.sender import ConsoleSender, SesSender, get_sender


def test_get_sender_is_console_by_default(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "email_sender", "console")
    assert isinstance(get_sender(), ConsoleSender)


def test_get_sender_is_ses_when_switched_on(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "email_sender", "ses")
    assert isinstance(get_sender(), SesSender)


def test_unknown_sender_name_falls_back_to_console(monkeypatch):
    """A typo in EMAIL_SENDER must not silently mean 'send real mail'."""
    from app.config import settings
    monkeypatch.setattr(settings, "email_sender", "sess")
    assert isinstance(get_sender(), ConsoleSender)


@pytest.mark.asyncio
async def test_console_sender_prints_link_and_does_not_raise(capsys):
    await ConsoleSender().send("a@dognosis.tech", "https://x/consume?token=abc")
    out = capsys.readouterr().out
    assert "a@dognosis.tech" in out
    assert "https://x/consume?token=abc" in out


class _FakeClient:
    def __init__(self, captured):
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send_email(self, **kwargs):
        self._captured.update(kwargs)
        return {"MessageId": "fake-message-id"}


class _FakeSession:
    def __init__(self, captured):
        self._captured = captured

    def client(self, service, **kwargs):
        self._captured["service"] = service
        self._captured["client_kwargs"] = kwargs
        return _FakeClient(self._captured)


@pytest.fixture
def ses_capture(monkeypatch):
    captured: dict = {}
    from app.config import settings
    monkeypatch.setattr(settings, "email_from", "IndieDex <hello@nammaindies.org>")
    monkeypatch.setattr(settings, "ses_region", "ap-south-1")

    import app.email.sender as sender_mod

    class FakeAioboto3:
        @staticmethod
        def Session():
            return _FakeSession(captured)

    monkeypatch.setattr(sender_mod, "aioboto3", FakeAioboto3)
    return captured


@pytest.mark.asyncio
async def test_ses_sender_calls_sesv2_with_expected_payload(ses_capture):
    await SesSender().send("a@dognosis.tech", "https://x/consume?token=abc")

    assert ses_capture["service"] == "sesv2"
    assert ses_capture["client_kwargs"]["region_name"] == "ap-south-1"
    assert ses_capture["FromEmailAddress"] == "IndieDex <hello@nammaindies.org>"
    assert ses_capture["Destination"] == {"ToAddresses": ["a@dognosis.tech"]}

    simple = ses_capture["Content"]["Simple"]
    assert simple["Subject"]["Data"]
    assert "https://x/consume?token=abc" in simple["Body"]["Text"]["Data"]
    assert "https://x/consume?token=abc" in simple["Body"]["Html"]["Data"]


@pytest.mark.asyncio
async def test_ses_sender_sends_to_exactly_one_recipient(ses_capture):
    """A login link is personal -- never CC/BCC, never a batch."""
    await SesSender().send("a@dognosis.tech", "https://x/consume?token=abc")
    assert list(ses_capture["Destination"].keys()) == ["ToAddresses"]
    assert len(ses_capture["Destination"]["ToAddresses"]) == 1


# --- copy shape: reduce the phishing silhouette ----------------------------
#
# A short message whose only content is a prominent link, from a domain with no
# reputation, is exactly what credential-harvesting mail looks like -- and it
# got us filed as spam by Google Workspace on first contact. The copy names the
# project and the organisation, says why the mail arrived, and shows the
# destination URL as text rather than hiding it behind a button alone.

def test_copy_identifies_the_project_and_organisation():
    from app.email.sender import _bodies
    text, html = _bodies("https://app.nammaindies.org/x?token=abc")
    for body in (text, html):
        assert "Namma Indies" in body
        assert "IndieDex" in body


def test_copy_explains_why_the_mail_arrived():
    from app.email.sender import _bodies
    text, html = _bodies("https://app.nammaindies.org/x?token=abc")
    for body in (text, html):
        assert "asked to sign in" in body


def test_html_shows_the_url_as_text_not_only_as_a_button():
    """A visible destination is a trust signal; a bare button hides it."""
    from app.email.sender import _bodies
    _, html = _bodies("https://app.nammaindies.org/x?token=abc")
    # present both as href and as readable text
    assert html.count("https://app.nammaindies.org/x?token=abc") >= 2


def test_copy_reassures_when_it_was_not_you():
    from app.email.sender import _bodies
    text, html = _bodies("https://app.nammaindies.org/x?token=abc")
    for body in (text, html):
        assert "wasn't you" in body or "was not you" in body
