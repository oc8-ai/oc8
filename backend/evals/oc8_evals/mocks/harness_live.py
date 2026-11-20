"""Two small MCP servers for the harness probe.

`OC8_HARNESS_ROLE=desk` lists store_word, need_word, one resource and one prompt.
`OC8_HARNESS_ROLE=archive` lists archive_note only. need_word returns an
elicitation until the call carries the operator's answer.
"""

from __future__ import annotations

import os
from typing import Any

import mcp.types as types

from oc8_evals.mocks._server import Handler, serve
from oc8_evals.mocks._store import Store

ROLE = os.environ.get("OC8_HARNESS_ROLE", "desk")
HANDBOOK = "office://handbook"
HANDBOOK_BODY = "The handbook says the word is READY."

DESK_TOOLS = [
    types.Tool(
        name="store_word",
        description="Store one word from the handbook.",
        inputSchema={
            "type": "object",
            "properties": {"word": {"type": "string"}},
            "required": ["word"],
        },
    ),
    types.Tool(
        name="need_word",
        description="Store the word once the operator has supplied it.",
        inputSchema={
            "type": "object",
            "properties": {
                "word": {"type": "string"},
                "elicitation_answer": {"type": "string"},
            },
        },
    ),
]
ARCHIVE_TOOLS = [
    types.Tool(
        name="archive_note",
        description="File a note in the archive. Not part of the desk skill.",
        inputSchema={
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
    ),
]


def handlers(store: Store) -> dict[str, Handler]:
    async def store_word(args: dict[str, Any]) -> str:
        word = str(args.get("word", ""))

        def _do(state: dict[str, Any]) -> str:
            state["word"] = word
            return word

        store.update(_do)
        return f"stored {word}"

    async def need_word(args: dict[str, Any]) -> str | dict[str, str]:
        word = str(args.get("word") or args.get("elicitation_answer") or "").strip()
        if not word:
            return {"_elicitation": "Which word should be stored?"}

        def _do(state: dict[str, Any]) -> str:
            state["word"] = word
            return word

        store.update(_do)
        return f"stored {word}"

    async def archive_note(args: dict[str, Any]) -> str:
        return f"archived {args.get('note', '')}"

    if ROLE == "archive":
        return {"archive_note": archive_note}
    return {"store_word": store_word, "need_word": need_word}


def main() -> None:
    store = Store.from_env()
    if ROLE == "archive":
        serve("harness-archive", ARCHIVE_TOOLS, handlers(store))
        return

    async def _read(uri: str) -> str:
        if uri == HANDBOOK:
            return HANDBOOK_BODY
        return "ERROR: unknown resource"

    serve(
        "harness-desk",
        DESK_TOOLS,
        handlers(store),
        resources=[
            types.Resource(
                uri=HANDBOOK,
                name="handbook",
                description="The office handbook",
            )
        ],
        prompts=[types.Prompt(name="desk-brief", description="How the desk works")],
        read_resource=_read,
    )


if __name__ == "__main__":
    main()
