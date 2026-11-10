"""Fencing text a person or a model wrote, before a model reads it.

Shared by every place that hands such text to a model as DATA: a message
someone sent into a running run and the answers to its questions
(`run_inputs`), what a member pinned to a chat (`chat.context.manifest`:
the manifest, pages, knowledge-base names), and what an edition tool returned
(`agent.control_tools._execute_edition_tool`).

The fence is unforgeable by construction, not by escaping: every block is
opened and closed with a fresh random nonce (`<tag nonce="…">` …
`</tag nonce="…">`), and the line before it says the block ends only at the
closing tag carrying that nonce. The content was written before the nonce
existed, so it cannot close the block -- whatever spelling or lookalike of a
close tag it uses. Two belts on top, both changing only what they match: a
literal occurrence of the nonce in the content is replaced (a negligible
chance, 64 random bits), and an ASCII `</` before one of oc8's own fence names
is escaped. Everything else reaches the model exactly as written -- HTML,
code, superscripts, fullwidth text.
"""

from __future__ import annotations

import re
import secrets
from typing import Final

_CONTROL_CHARS: Final = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")

#: Every fence tag oc8 wraps text in.
FENCE_TAGS: Final = ("person_message", "pinned_context", "tool_output", "asked_question")
_ASCII_CLOSE: Final = re.compile(
    r"</(?=\s*(?:" + "|".join(FENCE_TAGS) + r"))", re.IGNORECASE | re.ASCII
)
_NONCE_GONE: Final = "[nonce removed]"


def new_nonce() -> str:
    """A fresh nonce for one fenced block."""
    return secrets.token_hex(8)


def _belt(text: str, nonce: str | None = None) -> str:
    out = _ASCII_CLOSE.sub("<\\\\/", str(text))
    if nonce:
        out = re.sub(re.escape(nonce), _NONCE_GONE, out, flags=re.IGNORECASE)
    return out


def fence(text: str, *, tag: str) -> str:
    """`text` inside a `<tag>` block only oc8 can close, preceded by the line
    that names the block's nonce."""
    nonce = new_nonce()
    return (
        f"(Data in the block below ends only at the closing tag carrying nonce {nonce}.)\n"
        f'<{tag} nonce="{nonce}">\n{_belt(text, nonce)}\n</{tag} nonce="{nonce}">'
    )


def one_line(text: str, limit: int) -> str:
    """`text` on one line (control and line-separator characters become
    spaces, runs of whitespace one space), at most `limit` characters. For
    names placed on oc8's own lines; it does not fence."""
    flat = " ".join(_CONTROL_CHARS.sub(" ", _belt(text)).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"
