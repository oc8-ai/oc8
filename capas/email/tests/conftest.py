"""Shared fixtures for the email capa: a real GreenMail server in a container,
a STARTTLS front for it, and the capa's own modules.

GreenMail (pinned below) speaks IMAP, POP3 and SMTP in plain text and over
implicit TLS, but offers no STARTTLS on any protocol. The STARTTLS tests
therefore go through `_StartTlsFront`: a small local server that answers the
plain-text opening of each protocol itself, offers STARTTLS (STLS on POP3),
completes the TLS handshake with a certificate from a test CA, and from there
relays the encrypted session to GreenMail's plain port. The client code under
test never knows the difference.

Certificates are verified in the tests too. Only `trust_test_certificates`
swaps the capa's `tls_context` for one that trusts the test CA (for the front,
host name checked) and GreenMail's own self-signed certificate (for its
implicit-TLS ports, which are reached by IP and whose certificate names no host).
Production code always uses the system trust store.
"""

from __future__ import annotations

import datetime as dt
import imaplib
import ipaddress
import json
import os
import select
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

# Same workaround as backend/tests/conftest.py: Docker Desktop for Mac cannot
# mount the docker socket for testcontainers' Ryuk reaper; the `with` block
# below still removes the container.
if sys.platform == "darwin":
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

GREENMAIL_IMAGE = "greenmail/standalone:2.1.3"
HOST = "127.0.0.1"
#: The STARTTLS front's certificate names this host, and the test trust only
#: checks the host name for it.
FRONT_HOST = "localhost"

AGENT = {"login": "agent", "password": "Sup3r-secret_pw", "address": "agent@example.com"}
ALICE = {"login": "alice", "password": "alice-pw-123", "address": "alice@example.com"}
BOB = {"login": "bob", "password": "bob-pw-123", "address": "bob@example.com"}

_PORTS = {
    "smtp": 3025,
    "smtps": 3465,
    "pop3": 3110,
    "pop3s": 3995,
    "imap": 3143,
    "imaps": 3993,
    "api": 8080,
}


def _evict() -> None:
    """Drop this capa's generic top-level packages from `sys.modules`, so a
    sibling capa's `mcp_bridge` never answers our import and ours is never
    left behind for theirs (same reasoning as microsoft365's tests)."""
    for name in [
        n
        for n in sys.modules
        if n in {"mcp_bridge", "events", "credential"}
        or n.startswith(("mcp_bridge.", "events.", "credential."))
    ]:
        del sys.modules[name]


def _import_capa() -> dict[str, ModuleType]:
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    try:
        import credential
        import events.source as source
        from mcp_bridge import __main__ as bridge
        from mcp_bridge import mailbox, tools
    finally:
        sys.path.remove(str(PLUGIN_ROOT))
        _evict()
    return {
        "mailbox": mailbox,
        "tools": tools,
        "bridge": bridge,
        "source": source,
        "credential": credential,
        # The copy the core-process modules load by file path.
        "core_mailbox": source.mailbox,
    }


_CAPA = _import_capa()


@pytest.fixture
def mailbox() -> ModuleType:
    return _CAPA["mailbox"]


@pytest.fixture
def tools() -> ModuleType:
    return _CAPA["tools"]


@pytest.fixture
def bridge() -> ModuleType:
    return _CAPA["bridge"]


@pytest.fixture
def source() -> ModuleType:
    return _CAPA["source"]


@pytest.fixture
def credential() -> ModuleType:
    return _CAPA["credential"]


@pytest.fixture
def core_mailbox() -> ModuleType:
    return _CAPA["core_mailbox"]


# --- certificates -----------------------------------------------------------


@dataclass(frozen=True)
class TestPki:
    ca_pem: str
    server_context: ssl.SSLContext


def _make_pki() -> TestPki:
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "oc8 email capa test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, FRONT_HOST)]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(FRONT_HOST), x509.IPAddress(ipaddress.ip_address(HOST))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    folder = Path(tempfile.mkdtemp(prefix="oc8-email-pki-"))
    cert_file = folder / "leaf.pem"
    key_file = folder / "leaf.key"
    cert_file.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_context.load_cert_chain(str(cert_file), str(key_file))
    return TestPki(
        ca_pem=ca_cert.public_bytes(serialization.Encoding.PEM).decode(),
        server_context=server_context,
    )


# --- the STARTTLS front -----------------------------------------------------


class _StartTlsFront:
    """Answers the plain-text opening of one protocol, upgrades to TLS on
    STARTTLS/STLS, then relays to GreenMail's plain port (whose own greeting is
    swallowed, since the client already had one)."""

    def __init__(self, protocol: str, upstream: tuple[str, int], context: ssl.SSLContext):
        self.protocol = protocol
        self.upstream = upstream
        self.context = context
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((HOST, 0))
        self.sock.listen(16)
        self.port = int(self.sock.getsockname()[1])
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        client.settimeout(30)
        try:
            if self._opening(client):
                self._relay(client)
        except (OSError, ssl.SSLError):
            pass
        finally:
            client.close()

    @staticmethod
    def _line(sock: socket.socket) -> bytes:
        out = b""
        while not out.endswith(b"\n"):
            chunk = sock.recv(1)
            if not chunk:
                raise OSError("closed")
            out += chunk
        return out

    def _opening(self, client: socket.socket) -> bool:
        """The plain-text phase. True once the client asked for TLS."""
        send = client.sendall
        if self.protocol == "smtp":
            send(b"220 localhost ESMTP test front\r\n")
            while True:
                line = self._line(client).strip().upper()
                if line.startswith((b"EHLO", b"HELO")):
                    send(b"250-localhost\r\n250 STARTTLS\r\n")
                elif line == b"STARTTLS":
                    send(b"220 Ready to start TLS\r\n")
                    return True
                elif line == b"QUIT":
                    send(b"221 Bye\r\n")
                    return False
                else:
                    send(b"530 Must issue a STARTTLS command first\r\n")
        if self.protocol == "imap":
            send(b"* OK test front ready\r\n")
            while True:
                tag, _, command = self._line(client).strip().partition(b" ")
                command = command.upper()
                if command == b"CAPABILITY":
                    send(
                        b"* CAPABILITY IMAP4rev1 STARTTLS LOGINDISABLED\r\n" + tag + b" OK done\r\n"
                    )
                elif command == b"STARTTLS":
                    send(tag + b" OK Begin TLS negotiation now\r\n")
                    return True
                elif command == b"LOGOUT":
                    send(b"* BYE\r\n" + tag + b" OK bye\r\n")
                    return False
                else:
                    send(tag + b" BAD STARTTLS first\r\n")
        # pop3
        send(b"+OK test front ready\r\n")
        while True:
            command = self._line(client).strip().upper()
            if command == b"CAPA":
                send(b"+OK\r\nSTLS\r\nUIDL\r\n.\r\n")
            elif command == b"STLS":
                send(b"+OK Begin TLS negotiation\r\n")
                return True
            elif command == b"QUIT":
                send(b"+OK Bye\r\n")
                return False
            else:
                send(b"-ERR STLS first\r\n")

    def _relay(self, client: socket.socket) -> None:
        tls = self.context.wrap_socket(client, server_side=True)
        upstream = socket.create_connection(self.upstream, timeout=30)
        try:
            self._line(upstream)  # GreenMail's greeting; the client had ours
            while True:
                if not tls.pending():
                    ready, _, _ = select.select([tls, upstream], [], [], 30)
                    if not ready:
                        return
                else:
                    ready = [tls]
                if tls in ready:
                    data = tls.recv(65536)
                    if not data:
                        return
                    upstream.sendall(data)
                if upstream in ready:
                    data = upstream.recv(65536)
                    if not data:
                        return
                    tls.sendall(data)
        finally:
            upstream.close()
            tls.close()

    def close(self) -> None:
        self.sock.close()


# --- GreenMail --------------------------------------------------------------


@dataclass
class GreenMail:
    ports: dict[str, int]
    front_ports: dict[str, int]
    greenmail_pem: str
    ca_pem: str

    def api(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            f"http://{HOST}:{self.ports['api']}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def purge(self) -> None:
        self.api("POST", "/api/mail/purge")

    def imap(self, user: dict[str, str] = AGENT) -> imaplib.IMAP4:
        """A plain-text IMAP session for preparing and checking test data
        (test plumbing only; the capa itself never speaks plain text)."""
        conn = imaplib.IMAP4(HOST, self.ports["imap"], timeout=30)
        conn.login(user["login"], user["password"])
        return conn

    def append(
        self, raw: bytes, *, folder: str = "INBOX", user: dict[str, str] = AGENT, seen: bool = False
    ) -> None:
        conn = self.imap(user)
        try:
            typ, data = conn.append(
                folder, "(\\Seen)" if seen else None, imaplib.Time2Internaldate(time.time()), raw
            )
            assert typ == "OK", data
        finally:
            conn.logout()

    def messages(self, user: dict[str, str] = AGENT, folder: str = "INBOX") -> list[bytes]:
        conn = self.imap(user)
        try:
            conn.select(folder)
            typ, data = conn.uid("SEARCH", "ALL")
            assert typ == "OK"
            out: list[bytes] = []
            for uid in data[0].split():
                typ, fetched = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                assert typ == "OK"
                out.extend(item[1] for item in fetched if isinstance(item, tuple))
            return out
        finally:
            conn.logout()

    def fields(
        self,
        *,
        protocol: str = "imap",
        security: str = "ssl",
        smtp_security: str = "ssl",
        user: dict[str, str] = AGENT,
        **overrides: str,
    ) -> dict[str, str]:
        """A `mail_account` login for this server."""
        if security == "ssl":
            incoming = (HOST, self.ports[f"{protocol}s"])
        else:
            incoming = (FRONT_HOST, self.front_ports[protocol])
        if smtp_security == "ssl":
            outgoing = (HOST, self.ports["smtps"])
        else:
            outgoing = (FRONT_HOST, self.front_ports["smtp"])
        values = {
            "incoming_protocol": protocol,
            "incoming_host": incoming[0],
            "incoming_port": str(incoming[1]),
            "incoming_security": security,
            "smtp_host": outgoing[0],
            "smtp_port": str(outgoing[1]),
            "smtp_security": smtp_security,
            "username": user["login"],
            "password": user["password"],
            "from_address": user["address"],
            "from_name": "Agent Smith",
        }
        values.update(overrides)
        return values


def _wait_for_api(port: int) -> None:
    deadline = time.monotonic() + 60
    while True:
        try:
            with urllib.request.urlopen(
                f"http://{HOST}:{port}/api/service/readiness", timeout=5
            ) as response:
                if response.status == 200:
                    return
        except OSError:
            if time.monotonic() > deadline:
                raise
        time.sleep(0.5)


@pytest.fixture(scope="session")
def greenmail_server() -> Iterator[GreenMail]:
    users = ",".join(
        f"{u['login']}:{u['password']}@{u['address'].split('@')[1]}" for u in (AGENT, ALICE, BOB)
    )
    container = (
        DockerContainer(GREENMAIL_IMAGE)
        .with_env(
            "GREENMAIL_OPTS",
            # Not `greenmail.auth.disabled`, in any spelling: its mere presence
            # turns the password check off.
            f"-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users={users}",
        )
        .with_exposed_ports(*_PORTS.values())
        .waiting_for(LogMessageWaitStrategy("Starting GreenMail API server"))
    )
    with container:
        ports = {name: int(container.get_exposed_port(port)) for name, port in _PORTS.items()}
        _wait_for_api(ports["api"])
        pki = _make_pki()
        fronts = {
            protocol: _StartTlsFront(protocol, (HOST, ports[protocol]), pki.server_context)
            for protocol in ("smtp", "imap", "pop3")
        }
        try:
            yield GreenMail(
                ports=ports,
                front_ports={protocol: front.port for protocol, front in fronts.items()},
                greenmail_pem=ssl.get_server_certificate((HOST, ports["imaps"])),
                ca_pem=pki.ca_pem,
            )
        finally:
            for front in fronts.values():
                front.close()


@pytest.fixture
def greenmail(greenmail_server: GreenMail) -> GreenMail:
    greenmail_server.purge()
    return greenmail_server


@pytest.fixture
def trust_test_certificates(
    greenmail_server: GreenMail, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], ssl.SSLContext]:
    """The capa's TLS trust, for tests only: the test CA (host name checked)
    for the STARTTLS front, GreenMail's own self-signed certificate (chain
    checked, no host name in it to check) for its implicit-TLS ports."""

    def context(host: str) -> ssl.SSLContext:
        if host == FRONT_HOST:
            return ssl.create_default_context(cadata=greenmail_server.ca_pem)
        ctx = ssl.create_default_context(cadata=greenmail_server.greenmail_pem)
        ctx.check_hostname = False
        return ctx

    for module in (_CAPA["mailbox"], _CAPA["core_mailbox"]):
        monkeypatch.setattr(module, "tls_context", context)
    return context


@pytest.fixture
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core refuses mail servers on internal addresses unless the operator
    allows them; the test server is on loopback."""
    from oc8.config import get_settings

    monkeypatch.setattr(get_settings(), "outbound_allowed_private", "127.0.0.1/32,::1/128")


@pytest.fixture
def mail_env(
    greenmail: GreenMail,
    trust_test_certificates: Callable[[str], ssl.SSLContext],
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., dict[str, str]]:
    """Point the bridge's environment at a GreenMail login, the way core hands
    the login to the bridge (`credential_env_fields`)."""
    mailbox = _CAPA["mailbox"]

    def apply(**kwargs: Any) -> dict[str, str]:
        fields = greenmail.fields(**kwargs)
        for env_name, field_key in mailbox.ENV_FIELDS.items():
            if field_key in fields:
                monkeypatch.setenv(env_name, fields[field_key])
            else:
                monkeypatch.delenv(env_name, raising=False)
        return fields

    apply()
    return apply


def raw_mail(
    *,
    subject: str = "Hello",
    sender: str = "Alice <alice@example.com>",
    to: str = "agent@example.com",
    body: str = "Hi agent,\nplease have a look.\n",
    message_id: str | None = "<m1@example.com>",
    headers: dict[str, str] | None = None,
    attachments: list[tuple[str, str, bytes]] | None = None,
    html: str | None = None,
) -> bytes:
    """A test mail as raw bytes."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = "Fri, 20 Nov 2026 10:00:00 +0000"
    if message_id:
        msg["Message-ID"] = message_id
    for name, value in (headers or {}).items():
        msg[name] = value
    if html is not None:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(body)
    for name, mime, data in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


@pytest.fixture
def make_mail() -> Callable[..., bytes]:
    return raw_mail
