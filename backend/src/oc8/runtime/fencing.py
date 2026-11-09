"""Fencing text a person or a model wrote, before a model reads it.

Shared by every place that hands such text to a model as DATA: a message
someone sent into a running run (`run_inputs`), the answers to its questions,
and what a member pinned to a chat (`chat.context.manifest`). The text sits in
a block it cannot close early, and a name stays on its own line, so neither
can pass itself off as system or tool text.

Closing is refused broadly, not only for the block's own tag. After Unicode
compatibility normalisation (NFKC folds the fullwidth less-than and solidus,
U+FF1C and U+FF0F, into `</`) and with invisible format characters removed
(category Cf: zero-width spaces and joiners, U+FEFF, bidi controls), EVERY
`<` followed by `/` is escaped. Matching the tag name instead would miss a
lookalike spelling (Cyrillic er, U+0440, for `p`) that a model may still read
as the close of the block."""

from __future__ import annotations

import re
import unicodedata
from typing import Final

_CONTROL_CHARS: Final = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
_CLOSE_LIKE: Final = re.compile(r"<\s*/")


def _normalised(text: str) -> str:
    folded = unicodedata.normalize("NFKC", str(text))
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


def neutralise(text: str) -> str:
    """`text` with nothing left in it that could close a fence."""
    return _CLOSE_LIKE.sub("<\\\\/", _normalised(text))


def fence(text: str, *, tag: str) -> str:
    """`text` inside a `<tag>` block it cannot close early."""
    return f"<{tag}>\n{neutralise(text)}\n</{tag}>"


def one_line(text: str, limit: int) -> str:
    """`text` on one line (control and line-separator characters become
    spaces, runs of whitespace one space), at most `limit` characters, and
    unable to close a fence."""
    flat = " ".join(_CONTROL_CHARS.sub(" ", neutralise(text)).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"
