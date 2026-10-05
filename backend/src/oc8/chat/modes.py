"""Slash commands as MODE SWITCHES, not prompt macros (§5.2 of the AI
workplace design).

The distinction is the whole point. A prompt macro expands into text and hopes
the model complies; a mode changes what the run may DO. `/ask` does not ask the
model nicely to avoid tools -- it has none. `/plan` does not request restraint
-- every non-read call is denied by the PEP, audited as a denial, and comes
back to the model as a readable refusal naming `/do` as the way forward.

Three rules this module exists to keep:

1. **A mode can only ever remove authority.** There is no field here that
   grants anything and no branch that returns "allowed". `mode_refusal`
   returns a refusal or None, and None means "this module has no opinion" --
   the department frame and the agent's narrowing still decide, unchanged.
2. **One parser.** The web composer, the Telegram door and any future channel
   all send ordinary message text; the command is recognised HERE, so the two
   surfaces cannot disagree about what `/plan` means or which commands exist.
3. **An unknown command is a message.** Somebody typing "/etc/passwd is world
   readable?" gets their question asked, not a mode switch and not an error.

`/handoff` and `/review` from the design's table are deliberately not here:
one converts a room outcome and the other needs a second participant. `/budget`
is not here either -- the frontend already holds that number (GET
/budgets/status) and modelling it as an agent turn would spend a run to print
a figure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from oc8.authz.pdp import required_right


@dataclass(frozen=True)
class ChatMode:
    """One mode a chat turn can be in.

    `allows_tools=False` means no tool at all -- the cheap path. `allows_writes`
    is only consulted when tools are allowed at all.
    """

    key: str
    #: One line, English, shown in the composer's picker. The frontend maps a
    #: known key to a translated string and falls back to this text for a key
    #: it does not know, so a mode added later is still readable without a
    #: frontend release.
    summary: str
    #: Appended to the run's task text. English, like every other backend
    #: string; the model is not a UI.
    instruction: str
    allows_tools: bool = True
    allows_writes: bool = True


ASK = ChatMode(
    key="ask",
    summary="Answer from what you already know. No tools, no systems touched.",
    instruction=(
        "Answer this question directly from the conversation and your own knowledge. "
        "No tool is available to you for this turn -- if you cannot answer without "
        "one, say exactly what you would need to look up and stop there."
    ),
    allows_tools=False,
    allows_writes=False,
)

PLAN = ChatMode(
    key="plan",
    summary="Work out the steps and show them. Changes nothing.",
    instruction=(
        "Produce a plan: the steps you would take, in order, with what each one "
        "needs and what could go wrong. You may look things up, but every tool "
        "that would change anything is withheld for this turn -- do not attempt "
        "one, and do not hand the work to another agent. End with the single "
        "question a human has to answer before this can proceed."
    ),
    allows_tools=True,
    allows_writes=False,
)

DO = ChatMode(
    key="do",
    summary="Carry out what was agreed, under the usual guardrails.",
    instruction=(
        "Carry out what has been agreed in this conversation. Your department's "
        "guardrails and approval thresholds apply exactly as they always do: if "
        "something needs a human, ask for it rather than working around it."
    ),
)

SUMMARISE = ChatMode(
    key="summarise",
    summary="Condense this conversation into a decision record.",
    instruction=(
        "Condense this conversation into a decision record: what was decided, by "
        "whom, what is still open, and what happens next. Use only what is in the "
        "conversation above -- no tool is available for this turn, so do not go "
        "and check anything, and do not invent a decision nobody made."
    ),
    allows_tools=False,
    allows_writes=False,
)

#: Keyed by command word. Insertion order is picker order.
MODES: dict[str, ChatMode] = {m.key: m for m in (ASK, PLAN, DO, SUMMARISE)}

#: Core-owned tools that CHANGE something, and therefore have no business in a
#: read-only mode. The connection tools are classified by the pack's own
#: `scopes` and resolved through `required_right`; these are not on any
#: connection, so they are named here.
#:
#: `delegate_task` is in the list and that is the point: "executes nothing"
#: has to include "does not get somebody else to execute it". So are
#: `run_shell`/`run_program`: executing a command is the opposite of "changes
#: nothing". `render_component`, `todo_write`, `search_*`, `fetch_url` and the
#: read_* family are absent -- they show, list or read and change nothing
#: outside the run.
#:
#: Every OTHER core tool counts as a read in a read-only mode (see
#: `mode_refusal`): core tools carry no connection scopes, so `required_right`
#: would otherwise fail them closed to "modify" and /plan could not look
#: anything up.
WRITING_CONTROL_TOOLS: frozenset[str] = frozenset(
    {
        "memory_write",
        "delegate_task",
        "write_output_file",
        "propose_change",
        "decide_approval",
        "request_decision",
        "ask_user",
        "run_shell",
        "run_program",
        "responsibility_open",
        "responsibility_update",
        "responsibility_close",
        "schedule_followup",
        "cancel_followup",
    }
)

#: A leading command word, lowercase, followed by whitespace and a body.
#:
#: The `\s+` is what keeps "/etc/passwd is world readable?" a question: after
#: "etc" comes a slash, not whitespace, so the whole pattern fails and the text
#: is returned untouched. DOTALL so a multi-line body is one group.
_LEADING = re.compile(r"^/([a-z][a-z0-9-]*)\s+(.*)$", re.DOTALL)


def parse_command(text: str) -> tuple[ChatMode | None, str]:
    """`(mode, message)` -- the mode this turn is in and the text to actually send.

    Returns `(None, text)` unchanged for anything that is not a known command
    with a body: an unknown command, a path, an uppercase word, a command that
    is not at the very start, and a bare `/ask` with nothing after it. That
    last one matters: there is nothing to ask yet, so it is a literal message,
    and this is how "a message that legitimately begins with `/` must still be
    sendable" stays true even for the degenerate case.
    """
    match = _LEADING.match(text)
    if match is None:
        return None, text
    mode = MODES.get(match.group(1))
    if mode is None:
        return None, text
    body = match.group(2).strip()
    if not body:
        return None, text
    return mode, body


def mode_from_context(context: Mapping[str, Any] | None) -> ChatMode | None:
    """The mode a run was enqueued in, read back off `AgentRun.context`.

    An unknown key resolves to None -- fail OPEN, deliberately: a run enqueued
    by a newer release whose mode this process does not know must still execute
    with its ordinary authority rather than have every tool denied by a rule
    nobody can read.
    """
    if not context:
        return None
    return MODES.get(str(context.get("chat_mode") or ""))


def mode_directive(mode: ChatMode) -> str:
    """The line appended to the run's task text, so the model is told the same
    thing the PEP is about to enforce. Belt and braces, in that order: the
    enforcement is `mode_refusal`, this is only the courtesy of saying so."""
    return f"[Mode: {mode.key}] {mode.instruction}"


def mode_refusal(
    mode: ChatMode | None, tool_name: str, *, tool_scopes: Mapping[str, Any] | None
) -> str | None:
    """Why this mode will not let this tool be called, or None.

    None means "no opinion here" -- never "allowed". The frame, the narrowing
    and the value threshold all still have their say afterwards; this only ever
    subtracts.
    """
    if mode is None:
        return None
    if not mode.allows_tools:
        return (
            f"/{mode.key} answers without tools -- {tool_name} is not available for this "
            "turn. Answer from the conversation, or say what you would need to look up."
        )
    if mode.allows_writes:
        return None
    # Deferred: control_tools imports this module at load time.
    from oc8.agent.control_tools import CONTROL_TOOL_NAMES

    if tool_name in WRITING_CONTROL_TOOLS:
        writes = True
    elif tool_name in CONTROL_TOOL_NAMES:
        # A core tool is classified by name, not by scopes -- it sits on no
        # connection, and the gateway asks with `tool_scopes=None`.
        writes = False
    else:
        writes = required_right(tool_name, tool_scopes) != "read"
    if writes:
        return (
            f"/{mode.key} changes nothing -- {tool_name} is withheld for this turn. "
            "Describe what you would do with it instead, and the operator can send "
            "the same request again with /do to carry it out."
        )
    return None
