from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    names = {"connector", "mcp_bridge"}
    prefixes = ("connector.", "mcp_bridge.")
    for stale in [n for n in sys.modules if n in names or n.startswith(prefixes)]:
        del sys.modules[stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))
from mcp_bridge import google_api  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()


@pytest.fixture
def personal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC8_OAUTH_GRANT", "authorization_code")
    monkeypatch.setenv("OC8_OAUTH_ACCOUNT", "Anna@Acme.de")
    monkeypatch.setenv("GOOGLE_TOKEN", "annas-token")
    monkeypatch.delenv("GOOGLE_DELEGATED_MAILBOXES", raising=False)


def test_a_personal_connection_acts_as_its_own_account(personal: None) -> None:
    assert google_api.resolve_mailbox_address(None) == "Anna@Acme.de"
    assert google_api.resolve_mailbox_address("anna@acme.de") == "Anna@Acme.de"
    assert google_api.resolve_mailbox_token(None) == "annas-token"


def test_a_personal_connection_refuses_another_mailbox(personal: None) -> None:
    with pytest.raises(RuntimeError, match=r"Anna@Acme\.de only"):
        google_api.resolve_mailbox_token("boss@acme.de")
    with pytest.raises(RuntimeError, match=r"Anna@Acme\.de only"):
        google_api.resolve_mailbox_address("boss@acme.de")
