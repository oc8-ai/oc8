"""The one rule for which mailbox a Google login may act as
(mcp_bridge/mailbox_policy.py), shared by the tools and the poller."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "mcp_bridge" / "mailbox_policy.py"
_SPEC = importlib.util.spec_from_file_location("_test_google_mailbox_policy", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
policy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(policy)


def test_the_list_splits_like_cores_setup_list() -> None:
    assert policy.mailbox_list("a@x; b@x") == ["a@x", "b@x"]
    assert policy.mailbox_list(" a@x,\nb@x\tc@x ;;") == ["a@x", "b@x", "c@x"]


def test_ascii_case_matches_and_nothing_else_is_folded() -> None:
    listed = ["strasse@acme.test", "Info@Acme.test"]
    assert (
        policy.authorized_mailbox("INFO@acme.TEST", delegated=listed, personal=None)
        == "INFO@acme.TEST"
    )
    with pytest.raises(policy.MailboxRefused):
        policy.authorized_mailbox("STRAßE@acme.test", delegated=listed, personal=None)
    with pytest.raises(policy.MailboxRefused):
        policy.authorized_mailbox("STRAßE@acme.test", delegated=[], personal="strasse@acme.test")
    assert (
        policy.authorized_mailbox("ME@acme.test", delegated=[], personal="me@acme.test")
        == "me@acme.test"
    )
