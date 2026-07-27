import pytest

from app.email.sender import ConsoleSender, ResendSender, get_sender


def test_get_sender_is_console_without_api_key(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "resend_api_key", "")
    assert isinstance(get_sender(), ConsoleSender)


def test_get_sender_is_resend_with_api_key(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    assert isinstance(get_sender(), ResendSender)


@pytest.mark.asyncio
async def test_console_sender_prints_link_and_does_not_raise(capsys):
    await ConsoleSender().send("a@dognosis.tech", "https://x/consume?token=abc")
    out = capsys.readouterr().out
    assert "a@dognosis.tech" in out
    assert "https://x/consume?token=abc" in out


@pytest.mark.asyncio
async def test_resend_sender_posts_expected_payload(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(settings, "email_from", "IndieDex <hello@nammaindies.org>")
    captured = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse()

    import app.email.sender as sender_mod
    monkeypatch.setattr(sender_mod.httpx, "AsyncClient", FakeClient)

    await ResendSender().send("a@dognosis.tech", "https://x/consume?token=abc")

    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer re_test_key"
    assert captured["json"]["to"] == ["a@dognosis.tech"]
    assert captured["json"]["from"] == "IndieDex <hello@nammaindies.org>"
    assert "https://x/consume?token=abc" in captured["json"]["html"]
    assert "https://x/consume?token=abc" in captured["json"]["text"]
