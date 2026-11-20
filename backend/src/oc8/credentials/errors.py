"""The one error a credential validator raises on purpose for a person."""

from __future__ import annotations


class CredentialCheckFailed(ValueError):
    """A live check did not pass.

    `detail` is text that is safe to show a person; only a validator (or the
    service itself) that wrote it deliberately sets it. It is None whenever
    the failure came from anywhere else -- a transport error, an HTTP error,
    a parse error, a plain ValueError: that text can carry an upstream
    response body or an internal address, so it is only logged. `reason`
    ("auth" | "unreachable" | "invalid") is the class of failure when known;
    `internal` is for classifying and logging only.
    """

    def __init__(
        self, detail: str | None, *, reason: str | None = None, internal: str = ""
    ) -> None:
        super().__init__(detail or "The check failed.")
        self.detail = detail
        self.reason = reason
        self.internal = internal or detail or ""
