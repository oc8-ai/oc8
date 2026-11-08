from __future__ import annotations

import base64

import pytest

from oc8 import config


@pytest.fixture(autouse=True)
def _connection_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    s = config.get_settings()
    monkeypatch.setattr(s, "secret_kek", base64.b64encode(bytes(range(32))).decode(), raising=False)
    monkeypatch.setattr(s, "frontend_base_url", "https://app.example.com", raising=False)
    monkeypatch.setattr(s, "oauth_redirect_base_url", "https://api.example.com", raising=False)
