"""Where a passkey's relying party comes from: the configured public URL,
and only one a browser will create a passkey for."""

from __future__ import annotations

import pytest

from oc8.auth.passkey import RelyingParty, relying_party


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://oc8.example.com",
            RelyingParty(id="oc8.example.com", origin="https://oc8.example.com"),
        ),
        (
            "https://oc8.example.com:8443/app/",
            RelyingParty(id="oc8.example.com", origin="https://oc8.example.com:8443"),
        ),
        (
            "https://OC8.Example.com/",
            RelyingParty(id="oc8.example.com", origin="https://oc8.example.com"),
        ),
        ("http://localhost:8080", RelyingParty(id="localhost", origin="http://localhost:8080")),
        ("https://localhost", RelyingParty(id="localhost", origin="https://localhost")),
    ],
)
def test_a_usable_public_url_names_the_relying_party(url: str, expected: RelyingParty) -> None:
    assert relying_party(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        "http://oc8.example.com",  # plain HTTP off localhost
        "ftp://oc8.example.com",
        "https://127.0.0.1",  # an address is no relying-party id
        "https://[::1]:8080",
        "http://127.0.0.1:8080",
    ],
)
def test_no_relying_party_without_a_usable_public_url(url: str) -> None:
    assert relying_party(url) is None


def test_the_configured_public_url_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(config.get_settings(), "frontend_base_url", "https://app.example.org")
    assert relying_party() == RelyingParty(id="app.example.org", origin="https://app.example.org")
