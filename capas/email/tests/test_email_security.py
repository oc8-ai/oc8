"""Encryption is required and verified, logins fail cleanly, and the
connection test logs in to both servers without sending anything."""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Any

import pytest

from oc8.credentials.errors import CredentialCheckFailed


async def call(tools: ModuleType, name: str, **args: Any) -> Any:
    return await tools.CALL_HANDLERS[name](args)


@pytest.mark.parametrize("protocol", ["imap", "pop3"])
async def test_starttls_works_for_incoming_and_outgoing(
    tools: ModuleType,
    greenmail: Any,
    mail_env: Callable[..., dict[str, str]],
    make_mail: Any,
    protocol: str,
) -> None:
    mail_env(protocol=protocol, security="starttls", smtp_security="starttls")
    greenmail.append(make_mail(subject="Over STARTTLS"))
    found = await call(tools, "mail_search")
    assert [m["subject"] for m in found] == ["Over STARTTLS"]
    await call(tools, "mail_send", to=["alice@example.com"], subject="via starttls", body="b")
    assert len(greenmail.messages(user={"login": "alice", "password": "alice-pw-123"})) == 1


@pytest.mark.parametrize("security", ["none", "plain", "", "tls"])
def test_there_is_no_plain_text_option(mailbox: ModuleType, greenmail: Any, security: str) -> None:
    for key in ("incoming_security", "smtp_security"):
        with pytest.raises(mailbox.MailError, match="ssl or starttls"):
            mailbox.Account.from_fields(greenmail.fields(**{key: security}))


@pytest.fixture
def no_starttls_imap() -> Iterator[tuple[int, list[bytes]]]:
    """A plain-text IMAP server that does not offer STARTTLS, recording every
    line it receives."""
    received: list[bytes] = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)

    def serve() -> None:
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return
            with client:
                client.sendall(b"* OK plain only\r\n")
                buf = b""
                while True:
                    try:
                        chunk = client.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\r\n" in buf:
                        line, buf = buf.split(b"\r\n", 1)
                        received.append(line)
                        tag, _, command = line.partition(b" ")
                        if command.upper() == b"CAPABILITY":
                            client.sendall(
                                b"* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n" + tag + b" OK done\r\n"
                            )
                        else:
                            client.sendall(tag + b" OK done\r\n")

    threading.Thread(target=serve, daemon=True).start()
    yield int(server.getsockname()[1]), received
    server.close()


async def test_starttls_is_never_skipped_when_the_server_does_not_offer_it(
    tools: ModuleType,
    greenmail: Any,
    mail_env: Callable[..., dict[str, str]],
    no_starttls_imap: tuple[int, list[bytes]],
) -> None:
    port, received = no_starttls_imap
    mail_env(security="starttls", incoming_host="127.0.0.1", incoming_port=str(port))
    with pytest.raises(Exception, match="does not offer STARTTLS"):
        await call(tools, "mail_search")
    assert not any(b"LOGIN" in line.upper() or b"AUTH" in line.upper() for line in received)
    assert not any(b"Sup3r-secret_pw" in line for line in received)


async def test_smtp_starttls_is_never_skipped(
    tools: ModuleType, greenmail: Any, mail_env: Callable[..., dict[str, str]]
) -> None:
    # GreenMail's plain SMTP port offers no STARTTLS.
    mail_env(
        smtp_security="starttls", smtp_host="127.0.0.1", smtp_port=str(greenmail.ports["smtp"])
    )
    with pytest.raises(Exception, match="does not offer STARTTLS"):
        await call(tools, "mail_send", to=["alice@example.com"], subject="s", body="b")
    assert greenmail.messages(user={"login": "alice", "password": "alice-pw-123"}) == []


async def test_an_untrusted_certificate_is_refused(mailbox: ModuleType, greenmail: Any) -> None:
    # No test trust here: the system trust store, as in production.
    account = mailbox.Account.from_fields(greenmail.fields())
    with pytest.raises(mailbox.MailError, match="certificate"):
        mailbox.search(account, folder="INBOX")


@pytest.mark.parametrize(
    ("protocol", "tool", "args"),
    [
        ("imap", "mail_search", {}),
        ("pop3", "mail_search", {}),
        ("imap", "mail_send", {"to": ["alice@example.com"], "subject": "s", "body": "b"}),
    ],
)
async def test_a_wrong_password_is_a_clean_error_without_the_secret(
    tools: ModuleType,
    greenmail: Any,
    mail_env: Callable[..., dict[str, str]],
    protocol: str,
    tool: str,
    args: dict[str, Any],
) -> None:
    secret = "wr0ng-pässwörd-%&?"
    if tool == "mail_send":
        mail_env(protocol=protocol, smtp_password=secret, smtp_username="agent")
    else:
        mail_env(protocol=protocol, password=secret)
    with pytest.raises(Exception, match="rejected the username or password") as caught:
        await call(tools, tool, **args)
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    # Raised without its cause, so a logged traceback shows nothing more.
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


async def test_the_connection_test_logs_in_to_both_servers_and_sends_nothing(
    credential: ModuleType,
    greenmail: Any,
    trust_test_certificates: Any,
    allow_loopback: None,
) -> None:
    await credential.validate_mail_account(greenmail.fields())
    await credential.validate_mail_account(
        greenmail.fields(protocol="pop3", security="starttls", smtp_security="starttls")
    )
    for user in ("agent", "alice"):
        password = {"agent": "Sup3r-secret_pw", "alice": "alice-pw-123"}[user]
        assert greenmail.messages(user={"login": user, "password": password}) == []


@pytest.mark.parametrize("which", ["password", "smtp_password"])
async def test_the_connection_test_reports_a_wrong_password_as_auth(
    credential: ModuleType,
    greenmail: Any,
    trust_test_certificates: Any,
    allow_loopback: None,
    which: str,
) -> None:
    secret = "n0pe-secret-value"
    fields = greenmail.fields(**{which: secret})
    if which == "smtp_password":
        fields["smtp_username"] = "agent"
    with pytest.raises(CredentialCheckFailed) as caught:
        await credential.validate_mail_account(fields)
    assert caught.value.reason == "auth"
    assert caught.value.detail and "username or password" in caught.value.detail
    assert secret not in str(caught.value) and secret not in caught.value.internal


async def test_the_connection_test_refuses_internal_addresses(
    credential: ModuleType, greenmail: Any, trust_test_certificates: Any
) -> None:
    with pytest.raises(CredentialCheckFailed) as caught:
        await credential.validate_mail_account(greenmail.fields())
    assert caught.value.reason == "unreachable"


async def test_the_connection_test_names_a_bad_setting(credential: ModuleType) -> None:
    with pytest.raises(CredentialCheckFailed) as caught:
        await credential.validate_mail_account({"incoming_protocol": "imap"})
    assert caught.value.reason == "invalid"
