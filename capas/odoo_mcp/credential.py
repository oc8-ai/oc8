"""Credential-type test for odoo_login (unified credentials framework).

The cheapest real proof the login works: Odoo's `common.authenticate`
RPC (called here over the JSON-RPC 2 transport at /jsonrpc, the
JSON-RPC-shaped equivalent of the XML-RPC `xmlrpc/2/common` endpoint) --
the SAME auth call `mcp-server-odoo` (the stdio bridge this plugin
launches, see plugin.toml) makes for its API-key auth path (see
ODOO_API_KEY in tool_pack.toml). Without this, the Credentials page's
"Test" button had no `validate_entry_point` to run at all --
`test_credential()` (oc8.credentials.service) short-circuits to
`last_test_ok = True` for a type that declares none, so it reported "all
good" unconditionally regardless of whether the login actually worked.

Deliberately NOT `/web/session/authenticate` (the interactive web-session
login): Odoo refuses an API key there by design -- it accepts a real
password only -- so a validate function built on it reports "Access
Denied" for every API-key credential even a fully working one, as
happened live 2026-08-26 immediately after this file's first version
shipped. `common.authenticate` is what both the API-key AND the
password auth paths this plugin supports actually succeed or fail
against, so it is the one call whose verdict matches real usage.
"""

from __future__ import annotations

import asyncio

import httpx

# This plugin ships with the core and runs in its process, so it uses the
# core's outbound guard rather than carrying a second copy of the block-list.
from oc8.net.outbound import OutboundBlocked, PinnedTransport, guard_outbound_url

_TIMEOUT = 15.0


async def validate_odoo_login(values: dict[str, str]) -> None:
    url = (values.get("url") or "").rstrip("/")
    database = values.get("database") or ""
    username = values.get("username") or ""
    password = values.get("password") or ""
    if not url:
        raise ValueError("url is required")
    if not database:
        raise ValueError("database is required")
    if not username:
        raise ValueError("username is required")
    if not password:
        raise ValueError("password is required")

    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "common",
            "method": "authenticate",
            "args": [database, username, password, {}],
        },
    }
    # Resolve once, refuse internal addresses, then connect to exactly that
    # address (Host header and TLS name unchanged): a second lookup at connect
    # time could answer differently (DNS rebinding).
    try:
        ip = await asyncio.to_thread(guard_outbound_url, url)
    except OutboundBlocked as exc:
        raise ValueError("The Odoo address is not reachable from here") from exc
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, transport=PinnedTransport(ip), follow_redirects=False
    ) as client:
        try:
            resp = await client.post(f"{url}/jsonrpc", json=payload)
        except httpx.HTTPError as exc:
            raise ValueError("could not reach the Odoo server") from exc
    # Error text below is fixed wording: an upstream body or URL is never
    # copied into a message a person will read.
    if resp.status_code != 200:
        raise ValueError(f"Odoo answered with HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ValueError("Odoo did not answer with a JSON-RPC response") from exc
    if not isinstance(data, dict):
        raise ValueError("Odoo did not answer with a JSON-RPC response")
    error = data.get("error")
    if error is not None:
        detail = error.get("data", {}).get("message") if isinstance(error, dict) else None
        if isinstance(detail, str) and "access denied" in detail.lower():
            raise ValueError("Odoo rejected this login: Access Denied")
        raise ValueError("Odoo rejected this login")
    # common.authenticate returns the uid (truthy int) on success, or the
    # bare literal `false` on bad credentials -- never an error envelope.
    if not data.get("result"):
        raise ValueError("Odoo rejected this login: invalid username, password, or API key")
