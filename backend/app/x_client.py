"""Posting a photo to @nammaindies through the X API.

Two calls, upload then create, signed with OAuth 1.0a (`oauth1_header`) for
the account's own user token (the four values in the box .env). Endpoints checked against
docs.x.com on 2026-10-24: `POST /2/media/upload` takes multipart `media` plus
`media_category`, and `POST /2/tweets` takes `{"text", "media": {"media_ids"}}`;
both answer with the new id at `data.id`.

The one subtlety is which failures are safe to retry. Nothing is public until
`create_post` succeeds, so any failure up to and including the upload is
`XRejected`: retry tomorrow. A 4xx refusal from `create_post` is also
`XRejected`. But a timeout, a dropped connection or a 5xx *while creating* is
`XUncertain` -- X may
have made the post and the answer was lost, and retrying would post the same
dog twice. The daily run stops on that until a person has checked.
"""

import base64
import hashlib
import hmac
import io
import secrets
import time
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import httpx
from PIL import Image

from app.config import settings

API = "https://api.x.com"
MAX_EDGE = 4096
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_S = 30.0


class XRejected(Exception):
    """X refused, or never received the request. Nothing was posted."""


class XUncertain(Exception):
    """The post may exist on X. Do not retry without a person checking."""


def prepare_image(original: bytes) -> bytes:
    """A JPEG X will accept, with nothing in it but pixels.

    Re-encoding from decoded pixels drops EXIF, XMP and ICC by construction --
    stored originals already have none, but a post is public and a GPS tag in
    one would place the dog more precisely than the caption's area does."""
    img = Image.open(io.BytesIO(original))
    img = img.convert("RGB")
    if max(img.size) > MAX_EDGE:
        img.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    for quality in (90, 80, 70, 60, 50):
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality, optimize=True)
        if out.tell() <= MAX_BYTES:
            return out.getvalue()
    raise ValueError("image does not fit X's size limit even at quality 50")


def _pct(value: str) -> str:
    # RFC 5849 3.6: unreserved characters only, everything else %XX.
    return quote(str(value), safe="-._~")


def oauth1_header(method: str, url: str, *, consumer_key: str, consumer_secret: str,
                  token: str, token_secret: str, body_params: dict | None = None,
                  nonce: str | None = None, timestamp: str | None = None) -> str:
    """An OAuth 1.0a HMAC-SHA1 Authorization header (RFC 5849).

    Signed by hand rather than through a library: Authlib's httpx adapter is
    deprecated and rebuilds the request, which dropped the multipart body of
    the media upload. Only query parameters and form-encoded bodies enter the
    signature (RFC 5849 3.4.1.3.1); JSON and multipart bodies, which are all
    this client sends, do not -- so `body_params` exists for the test vector.
    """
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    parts = urlsplit(url)
    base_url = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))
    params = [*parse_qsl(parts.query, keep_blank_values=True),
              *(body_params or {}).items(), *oauth.items()]
    encoded = sorted((_pct(k), _pct(v)) for k, v in params)
    param_string = "&".join(f"{k}={v}" for k, v in encoded)
    base = "&".join([method.upper(), _pct(base_url), _pct(param_string)])
    key = f"{_pct(consumer_secret)}&{_pct(token_secret)}"
    digest = hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode()
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


class _OAuth1(httpx.Auth):
    def __init__(self, **credentials):
        self._credentials = credentials

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = oauth1_header(
            request.method, str(request.url), **self._credentials)
        yield request


def _error(response: httpx.Response) -> str:
    return f"{response.status_code} {response.text[:300]}"


class XClient:
    def __init__(self, api_key, api_secret, access_token, access_token_secret, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._auth = _OAuth1(consumer_key=api_key, consumer_secret=api_secret,
                             token=access_token, token_secret=access_token_secret)
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=API, auth=self._auth, transport=self._transport,
                                 timeout=TIMEOUT_S)

    async def upload_image(self, jpeg: bytes) -> str:
        try:
            async with self._http() as http:
                r = await http.post(
                    "/2/media/upload",
                    data={"media_category": "tweet_image"},
                    files={"media": ("indie.jpg", jpeg, "image/jpeg")},
                )
        except httpx.HTTPError as exc:
            raise XRejected(f"media upload did not complete: {exc!r}") from exc
        if r.status_code >= 300:
            raise XRejected(f"media upload refused: {_error(r)}")
        return str(r.json()["data"]["id"])

    async def create_post(self, text: str, media_id: str) -> str:
        try:
            async with self._http() as http:
                r = await http.post(
                    "/2/tweets", json={"text": text, "media": {"media_ids": [media_id]}})
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Never reached X, so nothing can have been created.
            raise XRejected(f"could not reach X: {exc!r}") from exc
        except httpx.HTTPError as exc:
            raise XUncertain(f"no answer while creating the post: {exc!r}") from exc
        if r.status_code >= 500:
            # X's edge answering 5xx says nothing about whether the post was
            # made behind it -- a live post after a 503 is a known pattern.
            raise XUncertain(f"X errored while creating the post: {_error(r)}")
        if r.status_code >= 300:
            raise XRejected(f"post refused: {_error(r)}")
        try:
            return str(r.json()["data"]["id"])
        except (ValueError, KeyError, TypeError) as exc:
            # A 2xx without an id: X says it worked but we can't tell which post.
            raise XUncertain(f"post accepted without an id: {r.text[:300]}") from exc


def x_client_from_settings() -> XClient:
    return XClient(settings.x_api_key, settings.x_api_secret,
                   settings.x_access_token, settings.x_access_token_secret)
