"""The one place that knows how to open a secured SMTP connection, plus the
credential-type test for smtp_server (unified credentials framework).

`smtp_connection` is deliberately the SINGLE source of truth for "how do we
dial an SMTP server": both the admin-facing "Test" button (`validate_smtp`
below, via `test_credential`) and the actual send path
(`oc8.mail.send.send_mail`) go through it. Two independent copies of this
logic would let "Test" and "Send" disagree about whether a given host is
reachable or safe -- an admin would get a green tick from a connection
shaped differently to the one that later carries a real password-reset
link.

Test itself opens a real connection and, if credentials were given,
authenticates -- the cheapest real proof the server is reachable and the
credentials work, without sending an actual message. It mirrors
`validate_s3` (`capas/s3_source/credential.py`): an async callable taking
the resolved field values, raising on any failure. `test_credential`
(`oc8.credentials.service`) awaits it and converts whatever comes out into
a `ValueError` carrying the message, so an `smtplib` exception here reaches
the admin with the server's own wording (e.g. the 535 auth rejection)
rather than a flattened "test failed".
"""

from __future__ import annotations

import asyncio
import contextlib
import smtplib
import ssl
from collections.abc import Iterator

from oc8.credentials.errors import CredentialCheckFailed

# The de-facto implicit-TLS ("SMTPS") port. Unlike 587, a server listening
# here expects the TLS handshake FIRST -- there is no plaintext greeting to
# read and no STARTTLS command to issue, so `smtplib.SMTP` would simply
# block until the timeout. Ports 587/25 take the opposite path: connect in
# plaintext, then upgrade with STARTTLS.
_IMPLICIT_TLS_PORT = 465

_CONNECT_TIMEOUT_SECONDS = 10


def _tls_context() -> ssl.SSLContext:
    """The TLS policy for every SMTP connection this codebase opens.

    `ssl.create_default_context()` means certificates ARE verified against
    the system trust store and the hostname IS checked. smtplib's own
    default is the opposite (`ssl._create_stdlib_context()`:
    check_hostname=False, CERT_NONE), i.e. encryption with no proof of who
    is on the other end -- which is exactly the attacker-in-the-middle a
    password-reset link must not be handed to.

    The deliberate consequence: an internal relay presenting a self-signed
    or private-CA certificate now fails, loudly, at "Test" time rather than
    silently sending reset links into an unauthenticated tunnel. THE FIX FOR
    THAT IS TO TRUST THE CA -- install it in the container's trust store (or
    point `SSL_CERT_FILE`/`SSL_CERT_DIR` at it). If a deployment genuinely
    cannot, the answer is a `verify_tls` escape hatch on the credential type,
    which would be changed HERE and nowhere else; it is out of scope today.

    Setting `use_tls` to "false" is NOT that workaround. It is a different
    decision with a different shape: it means "this relay is anonymous and
    the network between us is trusted", which is the same deployment that
    makes `username`/`password` `required=False` on this credential type. For
    an AUTHENTICATED relay it is strictly worse than the problem it appears
    to solve -- it moves the AUTH password from encrypted-but-unverified to
    cleartext on the wire. Never reach for it to silence a certificate error.
    """
    return ssl.create_default_context()


@contextlib.contextmanager
def smtp_connection(
    *,
    host: str,
    port: int,
    username: str = "",
    password: str = "",
    use_tls: bool = True,
) -> Iterator[smtplib.SMTP]:
    """Blocking: connect, secure, optionally authenticate, yield the client.

    smtplib is synchronous, so every caller runs this inside
    `asyncio.to_thread` rather than stalling the event loop on a slow or
    unreachable host. Raises whatever smtplib raises -- `validate_smtp`
    wants those verbatim for the admin, `send_mail` swallows them.
    """
    implicit_tls = use_tls and port == _IMPLICIT_TLS_PORT
    factory: type[smtplib.SMTP] = smtplib.SMTP_SSL if implicit_tls else smtplib.SMTP
    # Only SMTP_SSL takes a context up front; plain SMTP gets one at STARTTLS.
    extra = {"context": _tls_context()} if implicit_tls else {}
    with factory(host, port, timeout=_CONNECT_TIMEOUT_SECONDS, **extra) as client:  # type: ignore[arg-type]
        client.ehlo()
        if use_tls and not implicit_tls:
            # Raises SMTPNotSupportedError if the server never advertised
            # STARTTLS -- a genuine misconfiguration worth surfacing rather
            # than silently continuing in plaintext with a password.
            client.starttls(context=_tls_context())
            # A second EHLO is mandatory after STARTTLS: the pre-TLS
            # capability list (crucially AUTH) is discarded on upgrade, and
            # without it `login` may not see the server's auth mechanisms.
            client.ehlo()
        if username:
            client.login(username, password)
        yield client


def _check(host: str, port: int, username: str, password: str, use_tls: bool) -> None:
    """Blocking connect/handshake/auth. Run via `asyncio.to_thread`.

    Opening the connection IS the whole test -- nothing is sent, and the
    connection is torn down on the way out of the context manager."""
    with smtp_connection(
        host=host, port=port, username=username, password=password, use_tls=use_tls
    ):
        pass


async def validate_smtp(values: dict[str, str]) -> None:
    host = values.get("host", "")
    if not host:
        raise CredentialCheckFailed("host is required", reason="invalid")
    port_raw = values.get("port", "587")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise CredentialCheckFailed(
            f"port must be a number, got {port_raw!r}", reason="invalid"
        ) from exc
    if not 1 <= port <= 65535:
        raise CredentialCheckFailed(
            f"port must be between 1 and 65535, got {port}", reason="invalid"
        )
    username = values.get("username", "")
    password = values.get("password", "")
    # Anything other than an explicit "false" means "use TLS" -- an empty or
    # missing value falls back to the field's own declared default ("true"),
    # so the secure path is what a half-filled form gets.
    use_tls = values.get("use_tls", "true").strip().lower() != "false"
    # smtplib is blocking; run it off the event loop so a slow/unreachable
    # SMTP host doesn't stall every other request this process is serving.
    await asyncio.to_thread(_check, host, port, username, password, use_tls)
