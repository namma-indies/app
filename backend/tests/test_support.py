"""Store support and policy links are public pages, independent of account access."""
import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.config import Settings, settings
from app.routes.account import router as account_router
from app.routes.join import router as join_router
from app.routes.support import DOCUMENTS, router


@pytest.fixture
async def public_client():
    app = FastAPI()
    app.include_router(router)
    app.include_router(join_router)
    app.include_router(account_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


def assert_links(html):
    for path in ("/policies/terms", "/policies/privacy", "/policies/community-guidelines",
                 "/policies/grievance", "/support"):
        assert f'href="{path}"' in html


@pytest.mark.parametrize("path", ["/support", "/join", "/account/delete"])
async def test_pages_are_public_and_link_all_documents(public_client, path):
    response = await public_client.get(path)
    assert response.status_code == 200
    assert_links(response.text)


async def test_support_has_honest_placeholders_and_account_help(public_client, monkeypatch):
    for key in ("support_contact_name", "support_contact_email", "support_contact_details"):
        monkeypatch.setattr(settings, key, "")
    response = await public_client.get("/support")
    assert "not been configured yet" in response.text
    assert 'href="/account/delete"' in response.text
    assert "mailto:" not in response.text
    assert response.headers["cache-control"] == "no-store"


async def test_support_escapes_configured_contact_details(public_client, monkeypatch):
    monkeypatch.setattr(settings, "support_contact_name", '<script>alert("name")</script>')
    monkeypatch.setattr(settings, "support_contact_email", "support@example.invalid")
    monkeypatch.setattr(settings, "support_contact_details", "First line\n<b>Second line</b>")
    response = await public_client.get("/support")
    assert '<script>' not in response.text and '<b>Second line</b>' not in response.text
    assert '&lt;script&gt;' in response.text and '&lt;b&gt;Second line&lt;/b&gt;' in response.text
    assert 'href="mailto:support@example.invalid"' in response.text
    assert "not been configured yet" not in response.text


@pytest.mark.parametrize("document", list(DOCUMENTS))
async def test_unconfigured_document_does_not_publish_a_draft(public_client, monkeypatch, document):
    title, key = DOCUMENTS[document]
    monkeypatch.setattr(settings, key, "")
    response = await public_client.get(f"/policies/{document}")
    assert response.status_code == 503
    assert title in response.text and "not been configured yet" in response.text
    assert_links(response.text)


@pytest.mark.parametrize("document", list(DOCUMENTS))
async def test_policy_links_redirect_to_the_current_configuration(public_client, monkeypatch, document):
    _, key = DOCUMENTS[document]
    monkeypatch.setattr(settings, key, "/published/first?version=one")
    first = await public_client.get(f"/policies/{document}")
    assert first.status_code == 302
    assert first.headers["location"] == "/published/first?version=one"
    monkeypatch.setattr(settings, key, "/published/second?version=two")
    second = await public_client.get(f"/policies/{document}")
    assert second.headers["location"] == "/published/second?version=two"
    assert second.headers["cache-control"] == "no-store"


async def test_unknown_document_is_not_an_open_redirect(public_client):
    assert (await public_client.get("/policies/unknown?url=/other")).status_code == 404


@pytest.mark.parametrize("url", ["javascript:alert(1)", "//other.invalid/terms", "/\\other.invalid",
                                 "/terms\nLocation:/other", "/policies/privacy"])
def test_unsafe_policy_configuration_is_rejected(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, terms_url=url)


def test_https_policy_and_local_grievance_configuration():
    configured = Settings(_env_file=None, terms_url="https://example.invalid/terms", grievance_url="/grievances")
    assert configured.terms_url == "https://example.invalid/terms"
    assert configured.grievance_url == "/grievances"


@pytest.mark.parametrize("email", ["not-an-address", "a@example.invalid?subject=other", "a@example.invalid\r\nBcc:x"])
def test_support_email_cannot_add_mail_headers(email):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, support_contact_email=email)


async def test_expired_deletion_page_keeps_policy_links(app_client):
    response = await app_client.get("/account/delete/confirm?token=invalid")
    assert response.status_code == 401
    assert_links(response.text)
