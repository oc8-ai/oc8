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
   `--key value`, `--key=value`, curl's `-u user:pass`) under a credential
   key, compound ones included (`OPENAI_API_KEY`, `client_secret`,
   `refresh_token`). A strong key (password, secret, API key, credential,
   ...) refuses any value but a placeholder; a weak one (token, auth) only a
   value that looks like one. An Authorization key refuses any value but a
   placeholder, after an optional scheme (`Bearer`, `Basic`, ...);
2. a well-known key shape on its own (`sk-...`, `ghp_...`, a JWT, a PEM
   private key);
3. a bare `Bearer <token>` in prose, where the token's shape decides;
4. a sentence naming the value ("my password is hunter2", "mein Passwort
   lautet Sommer!");
5. a password in a URL (`scheme://user:pass@host`).

Linear time on any input: assignments are found by scanning from each
credential WORD (one alternation, `re.finditer`) with plain index arithmetic,
never by a regex with an open-ended key in front of a separator; every other
pattern is a literal anchor followed by single character-class runs. Nothing
is capped or windowed, so a long key or value is read to its delimiter.
"""

from __future__ import annotations

import re
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(?:secret|password|passwort|kennwort|credential|token|api[_-]?key|authorization)",
    re.IGNORECASE,
)

#: Where an assignment can be: every word a credential key is built around.
_CREDENTIAL_WORD = re.compile(
    r"(?i)password|passwort|kennwort|passwd|passphrase|secret|api[ _-]?key|access[ _-]?key"
    r"|private[ _-]?key|credential|token|authorization|auth|pwd|pw"
)
#: Characters a key is made of (`DB_PASSWORD`, `x-api-key`, `config.secret`).
_KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
#: Characters that end a bare value.
_VALUE_END = frozenset(" \t\r\n,;\"'})]")
_SEPARATORS = ("=>", "->", ":", "=")
_QUOTES = "\"'"
#: How far a `<...>` placeholder may reach; a longer one reads as a value.
_PLACEHOLDER_SPAN = 128

#: Key words whose value is always a secret: any value but a clear
#: placeholder counts (`password=gpt-4o`, `secret = manager` are values).
_STRONG_WORDS = (
    "password", "passwort", "kennwort", "passwd", "passphrase", "secret", "apikey", "accesskey",
    "privatekey", "credential",
)  # fmt: skip
#: Exact keys that are short spellings of a password.
_STRONG_KEYS = frozenset({"pw", "pwd"})
#: Schemes an Authorization value may start with.
_AUTH_SCHEMES = frozenset({"bearer", "basic", "token", "digest", "negotiate"})

#: Values that stand for no credential under any key: a placeholder or a
#: reference to where the real one lives. `changeme` is a real default
#: password, so it is not one of them.
_PLACEHOLDERS = frozenset(
    """
    none null nil empty leer required optional erforderlich n/a na tbd todo unset
    missing hidden masked redacted see siehe -
    """.split()
)
#: `<your password>` / `[password]` are placeholders: words in brackets, no
#: digits (`<hunter2>` is a value); `[redacted]` is what `redact_text` wrote.
_PLACEHOLDER_SHAPE = re.compile(
    r"^(?:<[A-Za-zÄÖÜäöüß _-]{1,64}>|\[[A-Za-zÄÖÜäöüß _-]{1,64}\]?|\*+|x{3,}|\.{3,}|…"
    # Where the secret lives, not the secret: `${OPENAI_API_KEY}`,
    # `$OPENAI_API_KEY` (an upper-case name; `$ecret1` is a value),
    # `%API_KEY%`, `{{ api_key }}`.
    r"|\$\{\w+\}|\$[A-Z_][A-Z0-9_]*|%[A-Za-z_]\w*%|\{\{\s*[\w.]+\s*\}\})$",
    re.IGNORECASE,
)
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
    enabled disabled on off aktiv inaktiv
    """.split()
)
#: A model name is a value of `model`/`token=`-style configuration, not a secret.
_MODEL_NAME = re.compile(
    r"(?i)^(?:gpt|o\d|claude|llama|mistral|mixtral|gemini|gemma|qwen|phi|deepseek)[\w.:-]*$"
)
_FLAG_TOKEN = re.compile(r"--?[A-Za-z][\w-]*")

#: Well-known credential shapes, refused wherever they stand: a literal
#: prefix and one unbounded run of token characters.
_SHAPES = re.compile(
    r"""(?x)
      (?<![\w-])sk-(?:ant-)?[A-Za-z0-9_-]{16,}
    | (?<![\w-])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}
    | (?<![\w-])github_pat_\w{20,}
    | (?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])
    | (?<![\w-])xox[abposr]-[A-Za-z0-9-]{10,}
    | (?<![\w-])glpat-[\w-]{20,}
    | (?<![\w-])AIza[\w-]{35}
    | (?<![\w-])eyJ[\w-]+\.[\w-]+\.[\w-]+
    """
)
_PEM_BEGIN = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END (?:[A-Z]+ )*PRIVATE KEY-----")
#: A bare "Bearer <token>" in prose: the token's shape decides (`_bearer_token`).
_BEARER = re.compile(r"(?i)\bbearer[ \t]+(?P<value>[A-Za-z0-9._~+/=-]+)")
_PROSE = re.compile(
    r"""(?ix)
    \b(?P<key>passwor[dt]s?|kennwort|pw|token|secret|api[\s_-]?key|credential)
    [ \t]++(?:is|ist|lautet|was|war)\b(?:[ \t]*+:)?[ \t]++
    (?P<value>[^\s,;]++)
    """
)
#: "my password hunter" -- a possessive straight before the value.
_POSSESSIVE = re.compile(
    r"(?ix)\b(?:my|our|mein|meine|unser|unsere)[ \t]++(?P<key>passwor[dt]|kennwort|pw)"
    r"[ \t]++(?P<value>[^\s,;]++)"
)
_XML = re.compile(r"<(?P<key>[A-Za-z][\w.-]*)>(?P<value>[^<\n]+)</(?P=key)>")
_URL_USERINFO = re.compile(
    r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://[^\s/:@]+:(?P<value>[^\s/@]+)@"
)
#: curl's `-u user:pass` / `--user user:pass`.
_USER_FLAG = re.compile(r"""(?<![\w-])(?:-u|--user)[ \t]+["']?[^\s:"']+:(?P<value>[^\s"']+)""")


def _key_strength(key: str) -> str | None:
    """'strong', 'weak', 'authorization' or None (not a credential key)."""
    flat = re.sub(r"[\s_.-]", "", key.lower())
    if flat.endswith("authorization"):
        return "authorization"
    parts = {part.lower() for part in re.split(r"[\s_.-]+|(?<=[a-z])(?=[A-Z])", key)}
    if parts & _STRONG_KEYS:
        return "strong"
    if any(word in flat for word in _STRONG_WORDS):
        return "strong"
    if "token" in flat or "auth" in parts or flat.endswith("auth"):
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


def _weak_value(value: str, *, quoted: bool = False) -> bool:
    """Whether a weak key's value is a credential, not a placeholder, a word,
    a number or a model name."""
    value = _trim(value)
    if _placeholder(value) or value.lower() in _STOP_WORDS:
        return False
    if _SHAPES.fullmatch(value):
        return True
    if _MODEL_NAME.match(value) or re.fullmatch(r"[\d.,]+", value):
        return False
    return quoted or len(value) >= 6


def _skip_blank(text: str, i: int, *, newline: bool = True) -> tuple[int, bool]:
    """Past spaces and tabs, and at most one newline; whether one was crossed."""
    n = len(text)
    while i < n and text[i] in " \t\r":
        i += 1
    crossed = False
    if newline and i < n and text[i] == "\n":
        crossed = True
        i += 1
        while i < n and text[i] in " \t\r":
            i += 1
    return i, crossed


def _read_value(text: str, i: int) -> tuple[int, int, bool]:
    """The value starting at `i`: (start, end, quoted). A quoted value runs to
    its closing quote (or the end of the line), a bare one to a delimiter."""
    n = len(text)
    if i < n and text[i] in _QUOTES:
        # The newline search stops at the closing quote: each character is
        # read once, and the caller resumes past the value.
        close = text.find(text[i], i + 1)
        stop = n if close == -1 else close
        line_end = text.find("\n", i + 1, stop)
        return i + 1, stop if line_end == -1 else line_end, True
    # A template reference reads through its closing brace (`${VAR}`,
    # `{{ name }}`) so the placeholder check sees all of it.
    for opener, closer in (("${", "}"), ("{{", "}}")):
        if text.startswith(opener, i):
            close = text.find(closer, i, min(n, i + _PLACEHOLDER_SPAN))
            if close != -1 and text.find("\n", i, close) == -1:
                return i, close + len(closer), True
    if i < n and text[i] == "<":
        # A placeholder (`<your password>`) is short: looked for within
        # `_PLACEHOLDER_SPAN` characters, else the value is read bare.
        close = text.find(">", i, min(n, i + _PLACEHOLDER_SPAN))
        if close != -1 and text.find("\n", i, close) == -1:
            return i, close + 1, True
    end = i
    while end < n and text[end] not in _VALUE_END:
        end += 1
    return i, end, False


def _refused(value: str, strength: str, *, quoted: bool, crossed: bool) -> bool:
    if strength == "weak":
        return _weak_value(value, quoted=quoted)
    if _placeholder(value):
        return False
    # A heading ("Password:") followed by a sentence on the next line.
    return not (crossed and _trim(value).lower() in _STOP_WORDS)


def _assignment_spans(text: str) -> list[tuple[int, int]]:
    """Scan from each credential word: widen it to its key, then read an
    optional closing quote, blanks, a separator (or a flag's blank), blanks
    and the value. `done` keeps every character to a bounded number of
    visits: a word inside a key or value already read is skipped."""
    spans: list[tuple[int, int]] = []
    n = len(text)
    done = 0
    for word in _CREDENTIAL_WORD.finditer(text):
        if word.start() < done:
            continue
        ks = word.start()
        while ks > done and text[ks - 1] in _KEY_CHARS:
            ks -= 1
        ke = word.end()
        while ke < n and text[ke] in _KEY_CHARS:
            ke += 1
        done = ke
        raw_key = text[ks:ke]
        flag = raw_key.startswith("--")
        strength = _key_strength(raw_key.lstrip("-"))
        if strength is None:
            continue
        i = ke
        if ks > 0 and text[ks - 1] in _QUOTES and i < n and text[i] == text[ks - 1]:
            i += 1
        i, _ = _skip_blank(text, i)
        separator = next((s for s in _SEPARATORS if text.startswith(s, i)), None)
        crossed = False
        if separator is not None:
            i, crossed = _skip_blank(text, i + len(separator))
            start, end, quoted = _read_value(text, i)
        elif flag and ke < i < n and "\n" not in text[ke:i]:
            # `--key value`: the next token, unless it is the next flag.
            start, end, quoted = _read_value(text, i)
            if _FLAG_TOKEN.fullmatch(text[start:end]):
                continue
        else:
            continue
        if strength == "authorization" and not quoted and text[start:end].lower() in _AUTH_SCHEMES:
            j, _ = _skip_blank(text, end, newline=False)
            if not end < j < n:
                continue  # "Authorization: Bearer" and nothing after it
            start, end, quoted = _read_value(text, j)
        done = max(done, end)
        value = text[start:end]
        if _refused(value, strength, quoted=quoted, crossed=crossed):
            spans.append((start, end if quoted else start + len(_trim(value))))
    return spans


def _bearer_token(value: str) -> bool:
    """A bearer token in prose: long with a digit, or dotted like a JWT --
    never "of bad news" or "bonds"."""
    value = value.rstrip(".")
    if len(value) >= 16 and re.search(r"\d", value):
        return True
    return bool(re.fullmatch(r"[\w-]+(?:\.[\w-]+)+", value))


def _other_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in _XML.finditer(text):
        strength = _key_strength(match.group("key"))
        value = match.group("value")
        if strength is None or _placeholder(value):
            continue
        if strength == "weak" and not _weak_value(value, quoted=True):
            continue
        spans.append(match.span("value"))
    spans += [match.span() for match in _SHAPES.finditer(text)]
    # One forward pass for the END lines: a BEGIN reuses the END found for an
    # earlier one while it lies ahead, and once none is left none is sought.
    end: re.Match[str] | None = None
    exhausted = False
    for begin in _PEM_BEGIN.finditer(text):
        if not exhausted and (end is None or end.start() < begin.end()):
            end = _PEM_END.search(text, begin.end())
            exhausted = end is None
        spans.append(
            # No END line: the key runs to the end of the text, all of it.
            (begin.start(), end.end() if end is not None and not exhausted else len(text))
        )
    spans += [m.span("value") for m in _BEARER.finditer(text) if _bearer_token(m.group("value"))]
    spans += [
        m.span("value") for m in _URL_USERINFO.finditer(text) if not _placeholder(m.group("value"))
    ]
    spans += [
        m.span("value") for m in _USER_FLAG.finditer(text) if not _placeholder(m.group("value"))
    ]
    return spans


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


def secret_value_spans(text: str) -> list[tuple[int, int]]:
    """Where `text` holds a credential value (module docstring for the forms),
    as merged, sorted (start, end) spans of the value alone."""
    spans = [*_assignment_spans(text), *_other_spans(text), *_prose_spans(text)]
    merged: list[tuple[int, int]] = []
    for start, end in sorted(span for span in spans if span[1] > span[0]):
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
