"""Keep place queries and map centres out of Uvicorn's request log."""

import logging


class PlaceSearchAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn supplies (client, method, path-with-query, version, status).
        # Filtering at the logger covers rejected requests and older clients
        # too; coarsening in the route is too late to protect those log lines.
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path, separator, _ = args[2].partition("?")
            if separator and path.rstrip("/").endswith("/places"):
                record.args = (*args[:2], f"{path}?[redacted]", *args[3:])
        return True
