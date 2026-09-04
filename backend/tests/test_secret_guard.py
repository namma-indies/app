"""The app must not serve a public deployment on secrets published in this repo.

Every secret in config.py has a working dev default, which is what makes a
fresh clone run. The failure this pins is the other side of that convenience: a
line missing from the box's `.env` does not crash, it silently falls back to a
value anyone can read on GitHub. `session_secret` is the worst of them -- the
session cookie is a signed observer id and nothing else, so the default lets
anyone mint a session for any observer in the database.

The check is deliberately keyed on `public_base_url` rather than an `APP_ENV`
flag. A flag you must remember to set reproduces the original bug one level up.
"""

import pytest

from app.config import (
    InsecureDefaultSecret,
    Settings,
    check_secrets,
    is_public_deployment,
)

PROD = "https://app.nammaindies.org"

REAL = dict(
    session_secret="s-real",
    magic_link_secret="m-real",
    phone_hash_secret="p-real",
    join_passcode="real-code",
)


def _settings(**over) -> Settings:
    # _env_file=None so a developer's own backend/.env cannot decide whether
    # these tests pass; the point is to control every input explicitly.
    return Settings(_env_file=None, **{**REAL, "public_base_url": PROD, **over})


def test_public_deployment_with_real_secrets_starts():
    check_secrets(_settings())


def test_localhost_keeps_the_dev_defaults_working():
    """A fresh clone must still run `uvicorn app.main:app` with no .env at all."""
    check_secrets(Settings(_env_file=None))


@pytest.mark.parametrize(
    "name,dev_value",
    [
        ("session_secret", "dev-session-secret"),
        ("magic_link_secret", "dev-magic-secret"),
        ("phone_hash_secret", "dev-phone-secret"),
        ("join_passcode", "dev-join"),
    ],
)
def test_each_default_secret_refuses_to_serve_publicly(name, dev_value):
    with pytest.raises(InsecureDefaultSecret) as e:
        check_secrets(_settings(**{name: dev_value}))
    # The message has to name the variable, or an operator reading a crashed
    # container log knows only that something is wrong.
    assert name.upper() in str(e.value)


def test_every_listed_default_matches_the_real_field_default():
    """_DEV_SECRETS is a hand-written copy of the defaults above it. If someone
    changes a default and not this map, the guard silently stops guarding that
    field -- which looks exactly like a passing test suite."""
    from app.config import _DEV_SECRETS

    fresh = Settings(_env_file=None)
    for name, dev_value in _DEV_SECRETS.items():
        assert getattr(fresh, name) == dev_value, (
            f"_DEV_SECRETS[{name!r}] is stale: the field default is now "
            f"{getattr(fresh, name)!r}, so the guard would never fire for it"
        )


def test_all_four_are_reported_at_once():
    """One redeploy per missing secret is a bad way to find out you missed four."""
    with pytest.raises(InsecureDefaultSecret) as e:
        check_secrets(Settings(_env_file=None, public_base_url=PROD))
    for name in ("SESSION_SECRET", "MAGIC_LINK_SECRET", "PHONE_HASH_SECRET", "JOIN_PASSCODE"):
        assert name in str(e.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "https://192.168.1.42:5174",   # README's phone-testing flow
        "https://10.0.0.5:5174",
        "http://box.local:8000",
        "",                             # unset
        "not a url",
    ],
)
def test_local_and_lan_urls_are_not_public(url):
    assert is_public_deployment(url) is False


@pytest.mark.parametrize(
    "url",
    ["https://app.nammaindies.org", "https://staging.nammaindies.org", "http://15.206.249.84"],
)
def test_real_deployments_are_public(url):
    assert is_public_deployment(url) is True
