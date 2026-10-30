"""Static serving for the built frontend, with the cache headers a PWA needs.

Starlette's StaticFiles sends only ETag/Last-Modified. With no Cache-Control a
browser or proxy may reuse a file without asking (heuristic freshness), and for
the files that decide *which build a client runs* that is how a client gets
stuck on an old version after a deploy.
"""

from pathlib import Path, PurePosixPath

from starlette.staticfiles import StaticFiles

# Entry points: they name the current build (index.html -> hashed assets,
# sw.js -> precache list), so they are revalidated on every use. no-cache means
# "store it, but ask first" -- cheap, because ETag makes the answer a 304.
_REVALIDATE = "no-cache"
_REVALIDATE_FILES = {"index.html", "sw.js", "registerSW.js", "manifest.webmanifest"}

# Vite content-hashes everything under /assets, so a URL never changes meaning.
_IMMUTABLE = "public, max-age=31536000, immutable"


def cache_control_for(path: str) -> str | None:
    """Cache-Control value for a path relative to the dist root, or None to leave it."""
    parts = PurePosixPath(path).parts
    if not parts:
        return _REVALIDATE  # "/" resolves to index.html
    if parts[0] == "assets":
        return _IMMUTABLE
    if parts[-1] in _REVALIDATE_FILES:
        return _REVALIDATE
    # workbox-<hash>.js is imported by sw.js; the hash changes with its content.
    if len(parts) == 1 and parts[0].startswith("workbox-") and parts[0].endswith(".js"):
        return _IMMUTABLE
    return None


class FrontendStaticFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        # Judge by the file served, not the request path: "/" and "/foo/"
        # resolve to index.html under html=True.
        rel = Path(full_path).relative_to(Path(self.directory)).as_posix()
        value = cache_control_for(rel)
        if value is not None:
            response.headers["Cache-Control"] = value
        return response
