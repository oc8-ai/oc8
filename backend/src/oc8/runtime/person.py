"""Who, if anybody, a run's work is for (Ruling 18).

`originating_operator` is the person a run ACTS for -- its authority. Some runs
continue a person's work without acting as them: a lead's wake-up after a
delegation, a decision follow-up. Those carry `for_person` (the person's
subject) instead, which grants nothing; it only says whose run it is, so the
Live timeline never shows it as the agent's own autonomous run.

Not `on_behalf_of` (`oc8.connections.acting`): that one is a member id and DOES
grant something -- the account a tool "in the user's context" acts through --
so it is withheld where this one is kept (an approver's own instruction), and
set where this one is not (a schedule's chain acts for its creator). Each key
answers its own question.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

FOR_PERSON: Final = "for_person"
#: `for_person` for work that continues SEVERAL people's runs (a task whose
#: runs name different people): somebody's, so never autonomous, but nobody's
#: own -- a colleague's run for everyone. Not a subject anyone can hold: the
#: `oc8:` prefix is never issued as an identity subject.
SEVERAL_PEOPLE: Final = "oc8:several-people"
#: The context keys that name a person behind a run, strongest first.
PERSON_KEYS: Final = ("originating_operator", FOR_PERSON)


def present(value: object) -> bool:
    """A context value that counts: anything but null and the empty string.
    The same rule as the SQL twin in `oc8.chat.live` (`->>` gives NULL for
    a JSON null and the text of anything else)."""
    return value is not None and value != ""


def person_behind(context: Mapping[str, Any] | None) -> str | None:
    """The subject of the person the run's work is for, or `None`."""
    ctx = context or {}
    for key in PERSON_KEYS:
        value = ctx.get(key)
        if present(value):
            return str(value)
    return None


def is_own(person: str | None, subject: str) -> bool:
    """Is `person` (from `person_behind`) the member with `subject`? Never for
    `SEVERAL_PEOPLE`, whatever subject a member happens to hold."""
    return person is not None and person != SEVERAL_PEOPLE and person == subject
