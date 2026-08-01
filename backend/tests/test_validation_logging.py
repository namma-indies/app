import io
import logging

import pytest
from PIL import Image


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (40, 30), (100, 140, 90)).save(b, "JPEG")
    return b.getvalue()


@pytest.mark.asyncio
async def test_validation_failure_is_logged_with_field_detail(authed_client, caplog):
    """A 422 must leave a server-side record of *which* field was rejected.

    Without this, a client sending a malformed body looks identical in the
    access log to any other 422 -- which is exactly how a field-level bug in
    the field went undiagnosable.
    """
    client, _ = authed_client
    with caplog.at_level(logging.WARNING):
        r = await client.post(
            "/sighting",
            files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
            data={"geo_source": "device_gps"},  # captured_at missing
        )
    assert r.status_code == 422
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "captured_at" in logged
    assert "/sighting" in logged
