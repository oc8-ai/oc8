"""One-way credential detection and redaction.

`carries_secret_value` is the tenant Assistant's pre-model secret gate: chat
send (`oc8.chat.service`), a queued turn (`oc8.chat.carry_over`), a steering
input (`oc8.runtime.run_inputs`) and a clarification answer
(`oc8.runtime.clarification`) -- every door checks what the PERSON wrote --
and the check on stored configuration (project instructions, builder
proposals). `redact_text` removes exactly the values the gate refuses, so
anything the gate would refuse is never redaction-blind (titles, context
objects, the stored refused turn).

The rule: a credential VALUE is refused, a word about one is not. "How many
tokens did I use?" is the Cost Center's question, "reset my password" a
request, "token: 5000 per month" a quota. Five forms count as a value:

1. `key = value` (also `:`, `->`, `=>`, quoted JSON keys, `<key>v</key>`,
   `--key value`, curl's `-u user:pass`) under a credential key, compound
   ones included (`OPENAI_API_KEY`, `client_secret`, `refresh_token`). A
   strong key (password, secret, API key, credential, ...) refuses any value
   but a placeholder; a weak one (token) only a value that looks like one;
2. a well-known key shape on its own (`sk-...`, `ghp_...`, a JWT, a PEM
   private key header, ...);
3. an Authorization header (any value) or a bare `Bearer <token>` in prose;
4. a sentence naming the value ("my password is hunter2", "mein Passwort
   lautet Sommer!");
5. a password in a URL (`scheme://user:pass@host`).
"""

from __future__ import annotations

import re
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(?:secret|password|passwort|kennwort|credential|token|api[_-]?key|authorization)",
    re.IGNORECASE,
)

#: Key words whose value is always a secret: any value but a clear
#: placeholder counts (`password=gpt-4o`, `secret = manager` are values).
_STRONG_WORDS = (
    "password", "passwort", "kennwort", "passwd", "secret", "apikey", "accesskey",
    "privatekey", "credential",
)  # fmt: skip
#: Exact keys that are short spellings of a password.
_STRONG_KEYS = {"pw", "pwd"}
#: Key words that also name ordinary things (usage tokens, quotas, model
#: names): only a value that looks like a credential counts.
_WEAK_WORDS = ("token",)

#: Values that stand for no credential under any key: a placeholder or a
#: reference to where the real one lives. `changeme` is a real default
#: password, so it is not one of them.
_PLACEHOLDERS = frozenset(
    """
    none null nil empty leer required optional erforderlich n/a na tbd todo unset
    missing hidden masked redacted see siehe - bearer basic digest negotiate
    """.split()
)
_PLACEHOLDER_SHAPE = re.compile(r"^(?:<.*>|\*+|x{3,}|\.{3,}|…|\[.*|\$\{\w+\})$", re.IGNORECASE)
#: Words that follow a weak key or a sentence about a credential without being
#: one: fillers, and what follows "my password" in a request about it ("reset
#: my password please"). Never consulted for a strong key's assignment.
_STOP_WORDS = frozenset(
    """
    per here below above that this the a an not no yes true false is ist lautet
    was are set changed expired abgelaufen keins keine nicht das der die ein eine
    dass for of to in on und and or oder field feld vault please bitte zurück reset
    policy richtlinie rules regeln manager change ändern vergessen forgot forgotten
    works funktioniert does did has hat will wird should soll must muss für again
    wieder now jetzt too auch also still noch length länge handling rotation
    """.split()
)
#: A model name is a value of `model`/`token=`-style configuration, not a secret.
_MODEL_NAME = re.compile(
    r"(?i)^(?:gpt|o\d|claude|llama|mistral|mixtral|gemini|gemma|qwen|phi|deepseek)[\w.:-]*$"
)

# Every repeat is bounded, so no form backtracks quadratically; `_windows`
# bounds the text each pattern reads.
_KEY = r"[A-Za-z][\w.-]{0,63}|api\s{1,3}key"
_ASSIGNMENT = re.compile(
    rf"""(?ix)
    (?<![\w.-])(?P<q>["']?)(?P<key>{_KEY})(?P=q)
    [ \t]{{0,8}}(?::|=>|->|=)[ \t]{{0,8}}
    (?:"(?P<dq>[^"\n]{{0,512}})"|'(?P<sq>[^'\n]{{0,512}})'|(?P<angle><[^<>\n]{{1,64}}>)|(?P<bare>[^\s,;"'}})\]]{{1,512}}))
    """
)
_XML = re.compile(r"<(?P<key>[A-Za-z][\w.-]{0,63})>(?P<value>[^<\n]{1,512})</(?P=key)>")
#: `--password x`, `--api-key=x`, `--token x`.
_FLAG = re.compile(r"(?<![\w-])--(?P<key>[A-Za-z][\w-]{0,63})(?:=|[ \t]{1,8})(?P<value>\S{1,512})")
#: curl's `-u user:pass` / `--user user:pass`.
_USER_FLAG = re.compile(
    r"""(?<![\w-])(?:-u|--user)[ \t]{1,8}["']?[^\s:"']{1,256}:(?P<value>[^\s"']{1,512})"""
)

#: Well-known credential shapes, refused wherever they stand.
_SHAPES = re.compile(
    r"""(?x)
      (?<![\w-])sk-(?:ant-)?[A-Za-z0-9_-]{16,512}
    | (?<![\w-])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,255}
    | (?<![\w-])github_pat_\w{20,255}
    | (?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])
    | (?<![\w-])xox[abposr]-[A-Za-z0-9-]{10,255}
    | (?<![\w-])glpat-[\w-]{20,255}
    | (?<![\w-])AIza[\w-]{35}
    | (?<![\w-])eyJ[\w-]{8,2048}\.[\w-]{2,2048}\.[\w-]{2,2048}
    | -----BEGIN\ (?:[A-Z]{1,16}\ ){0,4}PRIVATE\ KEY-----
    """
)
#: With the header's name there, any value but a placeholder is a credential.
_AUTH_HEADER = re.compile(
    r"(?i)\bauthorization\b[ \t]{0,8}[:=][ \t]{0,8}"
    r"(?:(?:bearer|basic|token|digest|negotiate)[ \t]{1,8})?(?P<value>[^\s,;]{1,2048})"
)
#: A bare "Bearer <token>" in prose: the token's shape decides (`_bearer_token`).
_BEARER = re.compile(r"(?i)\bbearer[ \t]{1,8}(?P<value>[A-Za-z0-9._~+/=-]{1,2048})")
_PROSE = re.compile(
    r"""(?ix)
    \b(?P<key>passwor[dt]s?|kennwort|pw|token|secret|api[\s_-]?key|credential)
    [ \t]{1,8}(?:is|ist|lautet|was|war)[ \t]{0,8}:?[ \t]{1,8}
    (?P<value>[^\s,;]{1,512})
    """
)
#: "my password hunter" -- a possessive straight before the value.
_POSSESSIVE = re.compile(
    r"(?ix)\b(?:my|our|mein|meine|unser|unsere)[ \t]{1,8}(?P<key>passwor[dt]|kennwort|pw)"
    r"[ \t]{1,8}(?P<value>[^\s,;]{1,512})"
)
_URL_USERINFO = re.compile(
    r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}://[^\s/:@]{1,256}:(?P<value>[^\s/@]{1,512})@"
)

#: Literal starts of the shapes: the pattern runs only when one occurs.
_SHAPE_MARKERS = (
    "sk-",
    "gh",
    "github_pat_",
    "AKIA",
    "ASIA",
    "xox",
    "glpat-",
    "AIza",
    "eyJ",
    "-----BEGIN",
)

#: Each pattern reads at most this much at a time, overlapping by more than
#: any form is long, so a long message costs linear time and misses nothing.
_WINDOW = 16384
_OVERLAP = 2560


def _key_strength(key: str) -> str | None:
    """'strong', 'weak' or None (not a credential key)."""
    flat = re.sub(r"[\s_.-]", "", key.lower())
    if flat in _STRONG_KEYS or (flat.endswith(("pw", "pwd")) and len(flat) <= 6):
        return "strong"
    if any(word in flat for word in _STRONG_WORDS):
        return "strong"
    if any(word in flat for word in _WEAK_WORDS):
        return "weak"
    return None


def _trim(value: str) -> str:
    return value.strip().rstrip(".!?:…")


def _placeholder(value: str) -> bool:
    value = _trim(value)
    return not value or value.lower() in _PLACEHOLDERS or bool(_PLACEHOLDER_SHAPE.match(value))


def _quantity(value: str, after: str) -> bool:
    """ "5000 per month", "8 characters long": a number with a unit."""
    return bool(re.fullmatch(r"[\d.,]+", value)) and bool(re.match(r"\s+[A-Za-zÄÖÜäöüß]", after))


def _weak_value(value: str, *, quoted: bool = False, after: str = "") -> bool:
    """Whether a weak key's value is a credential, not a placeholder, a word,
    a quantity or a model name."""
    value = _trim(value)
    if _placeholder(value) or value.lower() in _STOP_WORDS:
        return False
    if _SHAPES.fullmatch(value):
        return True
    if _MODEL_NAME.match(value) or re.fullmatch(r"[\d.,]+", value):
        return False
    return quoted or len(value) >= 6


def _value(value: str, strength: str, *, quoted: bool = False, after: str = "") -> bool:
    if strength == "strong":
        return not _placeholder(value)
    return _weak_value(value, quoted=quoted, after=after)


def _assignment_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for match in _ASSIGNMENT.finditer(text):
        strength = _key_strength(match.group("key"))
        if strength is None:
            continue
        group = next(g for g in ("dq", "sq", "angle", "bare") if match.group(g) is not None)
        if _value(match.group(group), strength, quoted=group != "bare"):
            spans.append(match.span(group))
    for match in _XML.finditer(text):
        strength = _key_strength(match.group("key"))
        if strength and _value(match.group("value"), strength, quoted=True):
            spans.append(match.span("value"))
    for match in _FLAG.finditer(text):
        strength = _key_strength(match.group("key"))
        value = match.group("value")
        if strength and not value.startswith("-") and _value(value, strength):
            spans.append(match.span("value"))
    for match in _USER_FLAG.finditer(text):
        if not _placeholder(match.group("value")):
            spans.append(match.span("value"))
    return spans


def _header_spans(text: str) -> list[tuple[int, int]]:
    spans = [
        match.span("value")
        for match in _AUTH_HEADER.finditer(text)
        if not _placeholder(match.group("value"))
    ]
    spans += [
        match.span("value")
        for match in _BEARER.finditer(text)
        if _bearer_token(match.group("value"))
    ]
    return spans


def _bearer_token(value: str) -> bool:
    """A bearer token in prose: long with a digit, or dotted like a JWT --
    never "of bad news" or "bonds"."""
    value = value.rstrip(".")
    if len(value) >= 16 and re.search(r"\d", value):
        return True
    return bool(re.fullmatch(r"[\w-]{1,2048}(?:\.[\w-]{1,2048}){1,8}", value))


def _prose_spans(text: str) -> list[tuple[int, int]]:
    """A sentence is read with the stop words for any key: "the password is
    required", "my password please". A model name is no exemption for a
    strong key."""
    spans = []
    for pattern in (_PROSE, _POSSESSIVE):
        for match in pattern.finditer(text):
            strength = _key_strength(match.group("key")) or "weak"
            value = _trim(match.group("value"))
            after = text[match.end() : match.end() + 32]
            if _placeholder(value) or value.lower() in _STOP_WORDS or _quantity(value, after):
                continue
            if strength == "weak" and not _weak_value(value):
                continue
            start = match.start("value")
            spans.append((start, start + len(value)))
    return spans


def _shape_spans(text: str) -> list[tuple[int, int]]:
    if not any(marker in text for marker in _SHAPE_MARKERS):
        return []
    return [match.span() for match in _SHAPES.finditer(text)]


def _windows(text: str) -> list[tuple[int, str]]:
    if len(text) <= _WINDOW:
        return [(0, text)]
    step = _WINDOW - _OVERLAP
    return [(start, text[start : start + _WINDOW]) for start in range(0, len(text), step)]


def secret_value_spans(text: str) -> list[tuple[int, int]]:
    """Where `text` holds a credential value (module docstring for the forms),
    as merged, sorted (start, end) spans of the value alone."""
    spans: list[tuple[int, int]] = []
    for offset, window in _windows(text):
        found = [
            *_assignment_spans(window),
            *_header_spans(window),
            *_prose_spans(window),
            *_shape_spans(window),
            *(m.span("value") for m in _URL_USERINFO.finditer(window)),
        ]
        spans += [(offset + start, offset + end) for start, end in found]
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def carries_secret_value(text: str) -> bool:
    """A credential VALUE in the text, not just a word about one. The tenant
    Assistant never accepts or forwards a credential value, so this is its
    gate on what a person writes and on stored configuration."""
    return bool(secret_value_spans(text))


def redact_text(value: str) -> str:
    """Replace every value `carries_secret_value` finds with `[redacted]`,
    keeping the words around it (`token=[redacted]`)."""
    out, last = [], 0
    for start, end in secret_value_spans(value):
        out.append(value[last:start])
        out.append("[redacted]")
        last = end
    out.append(value[last:])
    return "".join(out)


def redact_value(value: Any) -> Any:
    """Recursively remove values under sensitive keys before serialization."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[redacted]" if _SENSITIVE_KEY.search(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    return value
