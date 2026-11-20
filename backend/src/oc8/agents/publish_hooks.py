"""Subscribers to an agent-version publish (versioning design §2.8).

Publishing is the one moment a tenant's edits become what actually runs, so it
is the only useful place to hang a gate: the governance subsystem's compliance
rules and the evals subsystem's suite gate both attach here.

Four properties, each of them load-bearing:

* **Synchronous.** The hook runs inside the caller's `await`, not after it.
* **In the publish transaction.** The `AsyncSession` handed to a hook is the
  publisher's own, so a hook may read the half-written state and a refusal
  leaves nothing behind.
* **Ordered by registration.** A list, not a set or a dict-of-callables: a
  compliance check that must run before an eval gate can say so by registering
  first, and nothing else in this module needs to know why.
* **A hook that raises FAILS the publish.** That is the entire difference
  between a gate and a notification. Deliberately not Redis pub/sub: an
  asynchronous notification cannot veto.

The corollary is that a hook must NOT do long-running work. An eval suite is
minutes; an operator's publish request is seconds. The evals design handles
this by having its hook enqueue a run and mark the version pending, not by
blocking -- and this module deliberately provides no timeout, because a
timeout would turn a gate that is merely slow into a gate that fails open.

Core registers nothing. Keeping the registry empty is what keeps the seam
domain-neutral: a hook shipped here would give every publish in the product,
and in every test, an opinion about compliance before either subsystem exists.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:  # pragma: no cover - typing only
    from oc8.models import AgentVersion

#: What a subscriber looks like. It receives the publisher's own session and
#: the version row as it will be committed, and returns nothing -- a hook that
#: wants to influence the outcome refuses by raising, it does not return a
#: verdict object nobody would be obliged to read.
PublishHook = Callable[[AsyncSession, "AgentVersion"], Awaitable[None]]


class PublishHookFailed(Exception):
    """A registered hook refused, or broke, and the publish must not proceed.

    Wraps EVERY exception a hook raises, including programming errors, and that
    is deliberate: a gate that fails open when its own code is wrong is not a
    gate. The wrapper exists so the route layer can name which hook refused in
    its response -- "publish refused" with no attribution is a support ticket.
    """

    def __init__(self, hook_name: str, cause: BaseException) -> None:
        super().__init__(f"publish hook {hook_name!r} refused the publish: {cause}")
        self.hook_name = hook_name
        self.cause = cause


_HOOKS: list[tuple[str, PublishHook]] = []


def register_publish_hook(name: str, hook: PublishHook) -> None:
    """Add `hook`, to run after everything already registered.

    A duplicate name is refused rather than replaced or appended. Both of the
    alternatives are worse in the same way: a module imported twice would
    either silently run its compliance check twice (appended) or silently
    replace a hook somebody else registered under the same name (replaced),
    and neither is visible at the point of failure.
    """
    if any(existing == name for existing, _ in _HOOKS):
        raise ValueError(f"a publish hook named {name!r} is already registered")
    _HOOKS.append((name, hook))


def registered_publish_hooks() -> tuple[str, ...]:
    """The registered names, in run order. For diagnostics and for the test
    that pins core to registering none."""
    return tuple(name for name, _ in _HOOKS)


async def run_publish_hooks(db: AsyncSession, version: AgentVersion) -> None:
    """Run every hook in registration order. The first refusal wins.

    Iterates a copy: a hook that registers another hook would otherwise mutate
    the list being walked, and "does the new one run in this publish?" is a
    question nobody should have to answer by reading this loop.
    """
    for name, hook in list(_HOOKS):
        try:
            await hook(db, version)
        except PublishHookFailed:
            # Already attributed by an inner run (a hook that itself publishes
            # something). Re-wrapping would rename the culprit.
            raise
        except Exception as exc:
            raise PublishHookFailed(name, exc) from exc


def _reset_publish_hooks_for_tests() -> None:
    """Empty the registry.

    TEST ONLY. Underscore-prefixed rather than public because the registry is
    a process-lifetime global in production -- editions register their hooks at
    import time, and a runtime caller clearing them would silently disable
    every compliance gate the deployment paid for.
    """
    _HOOKS.clear()
