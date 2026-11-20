"""A software WebAuthn authenticator for tests.

Produces real registration (`none` attestation) and assertion responses,
signed with an ES256 key it holds, in the JSON shape a browser's
`PublicKeyCredential.toJSON()` gives. Each value it puts in a response comes
from the options the server sent, unless a test overrides it to prove the
server refuses it (another origin, another relying-party id, a sign count
that does not increase).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import encode_cbor

_UP = 0x01
_UV = 0x04
_AT = 0x40


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _client_data(kind: str, challenge: str, origin: str) -> bytes:
    return json.dumps(
        {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}
    ).encode()


@dataclass
class SoftAuthenticator:
    origin: str = "http://localhost:8080"
    rp_id: str = "localhost"
    sign_count: int = 0
    credential_id: bytes = field(default_factory=lambda: os.urandom(32))
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    @property
    def id(self) -> str:
        return b64url(self.credential_id)

    def _cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return encode_cbor(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def register(
        self,
        options: dict[str, Any],
        *,
        origin: str | None = None,
        rp_id: str | None = None,
    ) -> dict[str, Any]:
        rp = rp_id or options["rp"]["id"]
        auth_data = (
            hashlib.sha256(rp.encode()).digest()
            + bytes([_UP | _UV | _AT])
            + struct.pack(">I", self.sign_count)
            + bytes(16)  # aaguid
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self._cose_key()
        )
        attestation = encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data = _client_data("webauthn.create", options["challenge"], origin or self.origin)
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "attestationObject": b64url(attestation),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
        }

    def assert_(
        self,
        options: dict[str, Any],
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        sign_count: int | None = None,
    ) -> dict[str, Any]:
        """Answer an authentication challenge. Each call counts once more,
        unless `sign_count` pins the value sent."""
        if sign_count is None:
            self.sign_count += 1
            sign_count = self.sign_count
        rp = rp_id or options.get("rpId") or self.rp_id
        auth_data = (
            hashlib.sha256(rp.encode()).digest()
            + bytes([_UP | _UV])
            + struct.pack(">I", sign_count)
        )
        client_data = _client_data("webauthn.get", options["challenge"], origin or self.origin)
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }
