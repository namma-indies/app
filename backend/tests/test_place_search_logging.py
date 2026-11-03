import logging

import pytest

from app.logprivacy import PlaceSearchAccessFilter
from app.main import configure_logging


@pytest.mark.parametrize("path,status", [("/places", 200), ("/places", 401), ("/places", 422),
                                       ("/api/places", 200), ("/places/", 307)])
def test_place_access_logs_redact_search_values(path, status, caplog, monkeypatch):
    configure_logging()
    configure_logging()
    logger = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, PlaceSearchAccessFilter) for f in logger.filters) == 1
    monkeypatch.setattr(logger, "propagate", True)
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logger.info('%s - "%s %s HTTP/%s" %d', "client", "GET",
                    f"{path}?q=Hidden+query&lat=12.345678&lng=67.891234", "1.1", status)
    record = next(r for r in caplog.records if r.name == "uvicorn.access")
    assert record.args[2] == f"{path}?[redacted]"
    assert "Hidden" not in caplog.text
    assert "12.345678" not in caplog.text
    assert "67.891234" not in caplog.text
    assert str(status) in record.getMessage()


def test_other_access_logs_keep_their_request_details(caplog, monkeypatch):
    logger = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(logger, "propagate", True)
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logger.info('%s - "%s %s HTTP/%s" %d', "client", "GET", "/health?probe=ready", "1.1", 200)
    assert "/health?probe=ready" in caplog.text
