"""`host_of` spells a host the way a browser resolves it, IP addresses included
(WHATWG URL host parsing): a guessed link to a self-hosted system addressed by
IP must not slip past the link checks because it spells the address oddly."""

from __future__ import annotations

import pytest

from oc8.records.registry import host_of, normalise_host


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://192.168.1.10/odoo/sale.order/99", "192.168.1.10"),
        ("https://3232235786/odoo/sale.order/99", "192.168.1.10"),
        ("https://0xC0A8010A/odoo/sale.order/99", "192.168.1.10"),
        ("https://0300.0250.1.10/odoo", "192.168.1.10"),
        ("https://192.168.266/odoo", "192.168.1.10"),
        ("https://0xc0.0xa8.0x1.0xa./odoo", "192.168.1.10"),
        ("https://192.168.1.10:8069/odoo", "192.168.1.10"),
        ("https://[0:0:0:0:0:0:0:1]/odoo", "::1"),
        ("https://[2001:DB8:0:0:0:0:0:1]:8443/odoo", "2001:db8::1"),
        # Not IPv4: a name that merely contains numbers stays a name.
        ("https://1.2.example/odoo", "1.2.example"),
        ("https://h.example/odoo", "h.example"),
        ("https://H%2Eexample./odoo", "h.example"),
    ],
)
def test_hosts_are_read_the_way_a_browser_reads_them(url: str, host: str) -> None:
    assert host_of(url) == host


@pytest.mark.parametrize(
    "url",
    [
        "https://256.256.256.256.256/x",
        "https://0x/x",
        "https://[::zz]/x",
        "https://[x",
    ],
)
def test_odd_hosts_never_raise(url: str) -> None:
    assert isinstance(host_of(url), str)


@pytest.mark.parametrize(
    ("host", "normal"),
    [
        ("192.168.1.10", "192.168.1.10"),
        ("[::1]", "::1"),
        ("ODOO.example.com.", "odoo.example.com"),
        ("xn--strae-oqa.example", "xn--strae-oqa.example"),
        ("0xC0A8010A", "192.168.1.10"),
    ],
)
def test_a_browser_reported_host_is_normalised_alike(host: str, normal: str) -> None:
    assert normalise_host(host) == normal
