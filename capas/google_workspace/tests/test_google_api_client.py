from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    """Drop every cached `connector`/`mcp_bridge` module from sys.modules.

    Both names are generic -- after the package restructure every plugin ships
    a package called one of them (design §5.2) -- and sys.modules is keyed by
    NAME, not by path. Called SYMMETRICALLY on fixture setup AND teardown:
    before, so a sibling plugin's cached copy cannot answer our import; after,
    so nothing generic is left cached for anyone else. The teardown half is
    the load-bearing one -- `loader.import_entry_point` (Task 6's collision
    fix) only evicts modules IT ITSELF introduced, so a name left cached here
    makes a later `find_plugin`/`load_plugin` for gdrive_source or
    microsoft365 silently hand back THIS plugin's code. Verified live.
    """
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge", "events"}
        or n.startswith(("connector.", "mcp_bridge.", "events."))
    ]:
        del sys.modules[_stale]


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    """Make THIS plugin's packages the ones that resolve, for each test.

    Function-scoped and autouse, per Task 7's convention: every plugin import
    in this file sits INSIDE a test body and resolves at execution time, long
    after any collection-time module-top prelude would have run. The real
    runtime never imports the bridge in-process at all -- it launches it as a
    separate stdio process via `python -m mcp_bridge` -- so a test that wants
    the module needs the path insertion done by hand.
    """
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


def test_resolve_mailbox_token_uses_the_named_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_bridge import google_api

    monkeypatch.setenv("GOOGLE_DELEGATED_MAILBOXES", "a@company.com,b@company.com")
    monkeypatch.setenv("GOOGLE_DELEGATED_TOKEN_0", "token-a")
    monkeypatch.setenv("GOOGLE_DELEGATED_TOKEN_1", "token-b")
    assert google_api.resolve_mailbox_token("b@company.com") == "token-b"


def test_resolve_mailbox_token_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_bridge import google_api

    monkeypatch.setenv("GOOGLE_DELEGATED_MAILBOXES", "a@company.com")
    monkeypatch.setenv("GOOGLE_DELEGATED_TOKEN_0", "token-a")
    monkeypatch.setenv("GOOGLE_DEFAULT_MAILBOX", "a@company.com")
    assert google_api.resolve_mailbox_token(None) == "token-a"


def test_resolve_mailbox_token_rejects_an_unauthorized_mailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api

    monkeypatch.setenv("GOOGLE_DELEGATED_MAILBOXES", "a@company.com")
    monkeypatch.setenv("GOOGLE_DELEGATED_TOKEN_0", "token-a")
    with pytest.raises(RuntimeError, match="not one of this connection's authorized"):
        google_api.resolve_mailbox_token("evil@outside.com")


def test_resolve_mailbox_token_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that emits Agents@company.com for a mailbox configured as
    agents@company.com must still resolve -- email local-parts are
    case-insensitive in practice and Google treats them so."""
    from mcp_bridge import google_api

    monkeypatch.setenv("GOOGLE_DELEGATED_MAILBOXES", "agents@company.com")
    monkeypatch.setenv("GOOGLE_DELEGATED_TOKEN_0", "token-agents")
    assert google_api.resolve_mailbox_token("Agents@Company.com") == "token-agents"
    assert google_api.resolve_mailbox_address("Agents@Company.com") == "Agents@Company.com"


def test_resolve_mailbox_token_with_nothing_configured_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api

    monkeypatch.delenv("GOOGLE_DELEGATED_MAILBOXES", raising=False)
    monkeypatch.delenv("GOOGLE_DEFAULT_MAILBOX", raising=False)
    with pytest.raises(RuntimeError, match="no mailbox given"):
        google_api.resolve_mailbox_token(None)
