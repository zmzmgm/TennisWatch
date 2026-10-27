"""Conservative robots check before a collector starts a browser or fetches data."""
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .base import CollectionError

USER_AGENT = "TennisWatchAI/0.1"


class RobotsGuard:
    def __init__(self, base_url: str, *, transport=None):
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self._parser: RobotFileParser | None = None
        self.minimum_interval_seconds = 0.0

    def check(self, url: str) -> None:
        if urlsplit(url).netloc != urlsplit(self.base_url).netloc:
            raise CollectionError("UNEXPECTED_ORIGIN", "Collector URL is outside the configured site.")
        if self._parser is None:
            try:
                with httpx.Client(transport=self.transport, timeout=10, follow_redirects=False,
                                  headers={"User-Agent": USER_AGENT}) as client:
                    response = client.get(f"{self.base_url}/robots.txt")
            except httpx.TimeoutException as exc:
                raise CollectionError("ROBOTS_TIMEOUT", "robots.txt request timed out.") from exc
            except httpx.ConnectError as exc:
                raise CollectionError("ROBOTS_CONNECTION_FAILED", "robots.txt connection failed.") from exc
            except httpx.HTTPError as exc:
                raise CollectionError("ROBOTS_REQUEST_FAILED", "robots.txt request failed.") from exc
            parser = RobotFileParser()
            if response.status_code in (404, 410):
                parser.parse([])
            elif response.status_code == 200:
                if len(response.content) > 512_000 or "<html" in response.text[:1000].lower():
                    raise CollectionError("ROBOTS_UNEXPECTED_CONTENT", "robots.txt returned an unexpected document.")
                parser.parse(response.text.splitlines())
            else:
                raise CollectionError(f"ROBOTS_HTTP_{response.status_code}", "robots.txt did not return a usable response.")
            self._parser = parser
            delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 0
            rate = parser.request_rate(USER_AGENT) or parser.request_rate("*")
            self.minimum_interval_seconds = max(float(delay), rate.seconds / rate.requests if rate and rate.requests > 0 else 0)
        if not self._parser.can_fetch(USER_AGENT, url):
            raise CollectionError("ROBOTS_DISALLOWED", "robots.txt does not allow this collection URL.")
