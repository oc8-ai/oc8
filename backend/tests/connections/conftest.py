from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

import pytest

from oc8 import config

_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


@pytest.fixture(autouse=True)
def _connection_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(_PLUGINS_DIR))
    config.get_settings.cache_clear()
    s = config.get_settings()
    monkeypatch.setattr(s, "secret_kek", base64.b64encode(bytes(range(32))).decode(), raising=False)
    monkeypatch.setattr(s, "frontend_base_url", "https://app.example.com", raising=False)
    monkeypatch.setattr(s, "oauth_redirect_base_url", "https://api.example.com", raising=False)
    yield
    config.get_settings.cache_clear()
