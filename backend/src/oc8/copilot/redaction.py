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

1. `key = value` (also `:`, `->`, `=>`, quoted JSON keys, `<key>v</key>`)
   under a credential key, compound ones included (`OPENAI_API_KEY`,
   `client_secret`, `refresh_token`), with a plausible value (`_plausible`);
2. a well-known key shape on its own (`sk-...`, `ghp_...`, a JWT, a PEM
   private key header, ...);
3. an Authorization header or a bare `Bearer <token>`;
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

#: Key words whose value is always a secret (any non-placeholder value counts).
_STRONG_WORDS = ("password", "passwort", "kennwort", "passwd", "secret", "apikey", "privatekey")
#: Exact keys that are short spellings of a password.
_STRONG_KEYS = {"pw", "pwd"}
#: Key words that also name ordinary things (usage tokens, "credentials page"):
#: the value must look like a credential.
_WEAK_WORDS = ("token", "credential", "accesskey")

#: Values that are not credentials: placeholders, references, filler words and
#: what follows "my password" in a request about it ("reset my password please").
_STOP_VALUES = frozenset(
    """
    none null nil empty required optional see per tbd todo n/a na unset missing
    hidden masked vault here below above that this the a an not no yes true false
    is ist lautet was are set changed expired abgelaufen erforderlich leer keins
    keine nicht das der die ein eine dass for of to in on und and or oder field
    feld xxx xxxx *** ... … <redacted> secret password token please
    bitte zurück reset policy richtlinie rules regeln manager change ändern
    vergessen forgot forgotten works funktioniert does did has hat will wird
    should soll must muss für again wieder now jetzt too auch also still noch
    length länge handling rotation reset-link
    """.split()
)
#: A model name is a value of `model`/`token=`-style configuration, not a secret.
_MODEL_NAME = re.compile(
    r"(?i)^(?:gpt|o\d|claude|llama|mistral|mixtral|gemini|gemma|qwen|phi|deepseek)[\w.:-]*$"
)

_KEY = r"[A-Za-z][\w.-]*|api\s+key"
_ASSIGNMENT = re.compile(
    rf"""(?ix)
    (?<![\w-])(?P<q>["']?)(?P<key>{_KEY})(?P=q)
    \s*(?::|=>|->|=)\s*
    (?:"(?P<dq>[^"\n]*)"|'(?P<sq>[^'\n]*)'|(?P<bare>[^\s,;"'}})\]]+))
    """
)
_XML = re.compile(r"<(?P<key>[A-Za-z][\w.-]*)>(?P<value>[^<\n]+)</(?P=key)>")

#: Well-known credential shapes, refused wherever they stand.
_SHAPES = re.compile(
    r"""(?x)
      (?<![\w-])sk-(?:ant-)?[A-Za-z0-9_-]{16,}
    | (?<![\w-])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}
    | (?<![\w-])github_pat_\w{20,}
    | (?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])
    | (?<![\w-])xox[abposr]-[A-Za-z0-9-]{10,}
    | (?<![\w-])glpat-[\w-]{20,}
    | (?<![\w-])AIza[\w-]{35}
    | (?<![\w-])eyJ[\w-]{8,}\.[\w-]{2,}\.[\w-]{2,}
    | -----BEGIN\ (?:[A-Z]+\ )*PRIVATE\ KEY-----
    """
)
_AUTH_HEADER = re.compile(
    r"(?i)\bauthorization\b\s*[:=]\s*(?P<scheme>bearer|basic|token|digest)\s+(?P<value>[^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+(?P<value>[A-Za-z0-9._~+/=-]+)")
_PROSE = re.compile(
    r"""(?ix)
    \b(?P<key>passwor[dt]s?|kennwort|pw|token|secret|api[\s_-]?key|credential)
    (?:\s+(?:is|ist|lautet|was|war)\s*:?|\s*:)\s+
    (?P<value>[^\s,;]+)
    """
)
#: "my password hunter" -- a possessive straight before the value.
_POSSESSIVE = re.compile(
    r"(?ix)\b(?:my|our|mein|meine|unser|unsere)\s+(?P<key>passwor[dt]|kennwort|pw)\s+"
    r"(?P<value>[^\s,;]+)"
)
_URL_USERINFO = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:(?P<value>[^\s/@]+)@")


def _key_strength(key: str) -> str | None:
    """'strong', 'weak' or None (not a credential key)."""
    flat = re.sub(r"[\s_.-]", "", key.lower())
    if flat in _STRONG_KEYS or (flat.endswith(("pw", "pwd")) and len(flat) <= 6):
        return "strong"
    if any(word in flat for word in _STRONG_WORDS):
        return "strong"
    if any(word in flat for word in _WEAK_WORDS) or flat == "auth":
        return "weak"
    return None


def _plausible(value: str, *, strength: str, quoted: bool = False, after: str = "") -> bool:
    """Whether `value` is a credential rather than a placeholder, a reference,
    a number with a unit or a model name. `after` is the text that follows."""
    value = value.strip().rstrip(".!?:…")
    if not value or value.lower() in _STOP_VALUES or value.startswith("["):
        return False
    if _SHAPES.fullmatch(value):
        return True
    if _MODEL_NAME.match(value):
        return False
    if re.fullmatch(r"[\d.,]+", value):
        # "5000 per month", "8 characters long": a quantity, never a password.
        if re.match(r"\s+[A-Za-zÄÖÜäöüß]", after):
            return False
        return strength == "strong" and len(value) >= 4
    if strength == "strong" or quoted:
        return True
    return len(value) >= 6


def _assignment_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for match in _ASSIGNMENT.finditer(text):
        strength = _key_strength(match.group("key"))
        if strength is None:
            continue
        group = next(g for g in ("dq", "sq", "bare") if match.group(g) is not None)
        if _plausible(
            match.group(group),
            strength=strength,
            quoted=group != "bare",
            after=text[match.end() :],
        ):
            spans.append(match.span(group))
    for match in _XML.finditer(text):
        strength = _key_strength(match.group("key"))
        if strength and _plausible(match.group("value"), strength=strength, quoted=True):
            spans.append(match.span("value"))
    return spans


def _header_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for match in _AUTH_HEADER.finditer(text):
        value = match.group("value")
        if match.group("scheme").lower() == "bearer":
            ok = _bearer_token(value)
        else:
            ok = len(value) >= 8
        if ok:
            spans.append(match.span("value"))
    for match in _BEARER.finditer(text):
        if _bearer_token(match.group("value")):
            spans.append(match.span("value"))
    return spans


def _bearer_token(value: str) -> bool:
    """A bearer token: long with a digit, or dotted like a JWT -- never "of
    bad news" or "bonds"."""
    value = value.rstrip(".")
    if len(value) >= 16 and re.search(r"\d", value):
        return True
    return bool(re.fullmatch(r"[\w-]+(?:\.[\w-]+)+", value))


def _prose_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for pattern in (_PROSE, _POSSESSIVE):
        for match in pattern.finditer(text):
            strength = _key_strength(match.group("key")) or "weak"
            value = match.group("value")
            if _plausible(value, strength=strength, after=text[match.end() :]):
                start = match.start("value")
                spans.append((start, start + len(value.rstrip(".!?:…"))))
    return spans


def secret_value_spans(text: str) -> list[tuple[int, int]]:
    """Where `text` holds a credential value (module docstring for the forms),
    as merged, sorted (start, end) spans of the value alone."""
    spans = [
        *_assignment_spans(text),
        *_header_spans(text),
        *_prose_spans(text),
        *(m.span() for m in _SHAPES.finditer(text)),
        *(m.span("value") for m in _URL_USERINFO.finditer(text)),
    ]
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
