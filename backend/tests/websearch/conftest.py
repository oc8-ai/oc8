"""A fake search server on loopback, shared by the web search tests: no test
ever reaches a real search provider."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest


@dataclass
class FakeSearch:
    base: str
    #: (status, JSON body) per answer, in turn; the last one repeats.
    answers: list[tuple[int, Any]] = field(default_factory=list)
    delay_s: float = 0.0
    seen: list[dict[str, Any]] = field(default_factory=list)


def rows(n: int, snippet_key: str) -> list[dict[str, str]]:
    return [
        {"title": f"Hit {i}", "url": f"https://example.org/{i}", snippet_key: f"About {i}"}
        for i in range(1, n + 1)
    ]


@pytest.fixture
def fake() -> Iterator[FakeSearch]:
    state = FakeSearch(base="")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            state.seen.append(
                {
                    "method": self.command,
                    "path": parts.path,
                    "query": parse_qs(parts.query),
                    "headers": dict(self.headers),
                    "body": json.loads(body) if body else None,
                }
            )
            if state.delay_s:
                time.sleep(state.delay_s)
            index = min(len(state.seen), len(state.answers)) - 1
            status, payload = state.answers[index]
            data = json.dumps(payload).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = _answer
        do_POST = _answer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield state
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def platform_searxng(fake: FakeSearch, monkeypatch: pytest.MonkeyPatch) -> FakeSearch:
    """The operator's default: SearXNG at the fake server (an internal host)."""
    from oc8.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "web_search_provider", "searxng")
    monkeypatch.setattr(settings, "web_search_base_url", fake.base)
    monkeypatch.setattr(settings, "web_search_api_key", "")
    fake.answers = [(200, {"results": rows(3, "content")})]
    return fake
