"""Credential-type test for s3_api (unified credentials framework). Lists
the bucket root with the submitted keys -- the cheapest real proof they
work, reusing the exact request-signing code the connector itself uses."""

from __future__ import annotations

import asyncio

from connector.connector import _Client

from oc8.credentials.errors import CredentialCheckFailed
from oc8.net.outbound import OutboundBlocked, PinnedTransport, guard_outbound_url
from oc8.oauth.http import current_override


async def validate_s3(values: dict[str, str]) -> None:
    access_key = values.get("access_key", "")
    secret_key = values.get("secret_key", "")
    if not access_key or not secret_key:
        raise CredentialCheckFailed("access_key and secret_key are both required", reason="invalid")
    # A bucket-less HEAD-style listing isn't meaningful without a bucket
    # (which isn't part of this credential -- design's key structural
    # rule). Testing THIS credential in isolation can only prove the keys
    # are well-formed and the signing succeeds against the given endpoint,
    # not that a specific bucket is reachable -- that proof happens
    # naturally the first time a DataSource using this credential syncs.
    region = values.get("region", "us-east-1")
    endpoint = values.get("endpoint", "")
    # A custom endpoint is a typed-in address: refuse internal targets and
    # connect to exactly the address that was checked (DNS rebinding).
    transport: PinnedTransport | None = None
    if endpoint.strip():
        try:
            ip = await asyncio.to_thread(guard_outbound_url, endpoint)
        except OutboundBlocked as exc:
            raise CredentialCheckFailed(
                "The endpoint is not reachable from here", reason="unreachable"
            ) from exc
        transport = PinnedTransport(ip, inner=current_override())
    client = _Client("", region, endpoint, access_key, secret_key, transport=transport)
    await client.get("/", [("list-type", "2"), ("max-keys", "1")])
