"""Basic request safety checks shared by public reservation site adapters."""
from urllib.parse import urlsplit

from .base import CollectionError

USER_AGENT = "TennisWatchAI/0.1"


class RequestGuard:
    """Restrict requests to the configured host; pacing is enforced by each adapter."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.minimum_interval_seconds = 0.0

    def check(self, url: str) -> None:
        expected = urlsplit(self.base_url)
        actual = urlsplit(url)
        if actual.scheme != "https" or actual.netloc != expected.netloc:
            raise CollectionError("UNEXPECTED_ORIGIN", "Collector URL is outside the configured HTTPS host.")
