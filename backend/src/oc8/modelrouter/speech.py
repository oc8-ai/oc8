"""Speech models: dictation (speech to text) and reading answers aloud (text to
speech), through the model router (spec 2026-11-27 §5.4, §9).

A model config declares what it is for in `params["capability"]`: absent or
`chat` is a chat model, `speech_to_text` / `text_to_speech` a speech model. A
speech model is never a chat model -- not offered to a chat, never the
Copilot's model, an agent's model or an auto tier -- and only a provider whose
adapter speaks OpenAI's audio endpoints can be one. The tenant picks one of
each in `Organization.settings["speech"]`.

A call goes to exactly the picked model: no fallback to another provider or a
local default, since the person asked for that model to hear or speak. Before
it leaves, the same checks a cloud embedding passes apply (entitlement, strict
BYOK, a key, the provider's monthly limit, the hard budget), and a restricted
text only reaches a local model. Usage rows name the person, the provider the
router resolved (a plugin provider's own name, not its adapter's) and the
model, with the audio seconds (rounded up) or the characters as platform units
-- also when the provider bills tokens, since the per-member daily allowance
counts them. Rows are append-only: a call first reserves its estimate as a
usage row (`_reserve`), and dictation appends what the provider reports beyond
it (and its tokens); nothing is refunded. Audio is never stored.

Residual risk, accepted: for dictation the allowance bounds upload BYTES per
day (cap minutes x 16 kbit/s); real audio time can exceed it by the ratio
16 kbit/s : actual bitrate -- about 2.7x for constant low-bitrate Opus, up to
~50x for mostly silent VBR/DTX audio. Admins can lower the daily cap or switch
dictation off (0). A provider-reported duration far above the reservation is
audited (`chat.audio.allowance_overshoot`, `OVERSHOOT_FACTOR`).

Speech costs show 0 until pricing reads platform units, so the budgets that
sum tokens do not see it; what bounds the spend is the per-member daily
allowance (`_reserve`): minutes of dictation and characters read
aloud per member per day on the organisation's clock, set per tenant in the
speech settings (0 switches the feature off for members).
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import math
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.config import get_settings
from oc8.modelrouter.adapters._openai_audio import Transcript
from oc8.modelrouter.keys import resolve_model_base_url, resolve_model_key
from oc8.modelrouter.registry import resolve_provider
from oc8.modelrouter.router import ClassificationViolation
from oc8.modelrouter.types import Usage

logger = logging.getLogger(__name__)

CAPABILITY_KEY: Final = "capability"
CHAT: Final = "chat"
SPEECH_TO_TEXT: Final = "speech_to_text"
TEXT_TO_SPEECH: Final = "text_to_speech"
CAPABILITIES: Final = (CHAT, SPEECH_TO_TEXT, TEXT_TO_SPEECH)
SPEECH_CAPABILITIES: Final = frozenset({SPEECH_TO_TEXT, TEXT_TO_SPEECH})

#: Where the tenant's two picks live in `Organization.settings`.
SETTINGS_KEY: Final = "speech"
SETTING_FOR: Final = {
    SPEECH_TO_TEXT: "speech_to_text_model_id",
    TEXT_TO_SPEECH: "text_to_speech_model_id",
}

#: OpenAI's `/audio/speech` takes at most 4096 characters per call.
SPEECH_PIECE_CHARS: Final = 4000
#: The most one answer may be read aloud at once (after markdown is stripped).
MAX_SPOKEN_CHARS: Final = 20_000

#: Per member per day, overridable per tenant in `settings["speech"]`.
DAILY_LIMIT_FOR: Final = {
    SPEECH_TO_TEXT: "daily_transcription_minutes",
    TEXT_TO_SPEECH: "daily_read_aloud_characters",
}
DEFAULT_DAILY_LIMIT: Final = {SPEECH_TO_TEXT: 60, TEXT_TO_SPEECH: 200_000}
#: The setting's unit -> the platform units a usage row holds (seconds, characters).
_UNITS_PER: Final = {SPEECH_TO_TEXT: 60, TEXT_TO_SPEECH: 1}

#: A recording's length by its size, at 16 kbit/s (a 12 MB upload counts as
#: about 100 minutes). The chat records at this rate, so for it the estimate
#: is about the truth; a higher-bitrate upload counts longer than it is. It
#: is not a bound on real audio time: a lower bitrate -- constant low-bitrate
#: Opus (~2.7x) or mostly silent VBR/DTX audio (up to ~50x) -- holds more
#: seconds per byte, so the allowance bounds upload bytes, not minutes.
_ESTIMATED_BYTES_PER_SECOND: Final = 2_000
#: A reported duration this many times the reservation is audited.
OVERSHOOT_FACTOR: Final = 10


def capability_of(cfg: m.ModelConfig | None) -> str:
    raw = (cfg.params or {}).get(CAPABILITY_KEY) if cfg is not None else None
    return raw if raw in CAPABILITIES else CHAT


def is_speech_model(cfg: m.ModelConfig | None) -> bool:
    return capability_of(cfg) in SPEECH_CAPABILITIES


def chat_capable() -> ColumnElement[bool]:
    """SQL twin of `not is_speech_model(cfg)`, for the queries that list the
    models a chat or the Copilot may run on."""
    return func.coalesce(m.ModelConfig.params[CAPABILITY_KEY].astext, CHAT).notin_(
        tuple(SPEECH_CAPABILITIES)
    )


def speaks_openai_audio(adapter: object) -> bool:
    """Whether a provider's adapter can transcribe and speak. A plugin
    provider built on the OpenAI-compatible adapter can; Anthropic, Ollama
    and the ChatGPT subscription cannot."""
    return callable(getattr(adapter, "transcribe", None)) and callable(
        getattr(adapter, "open_speech", None)
    )


class _SpeechAdapter(Protocol):
    async def transcribe(
        self, audio: bytes | io.BytesIO, *, model: str, content_type: str, language: str | None
    ) -> Transcript: ...

    async def open_speech(
        self, text: str, *, model: str, extra: dict[str, Any] | None
    ) -> AsyncIterator[bytes]: ...


class SpeechUnavailable(RuntimeError):
    """The speech model cannot be called right now; the message says why."""


class SpeechNotConfigured(SpeechUnavailable):
    """The tenant has picked no speech model of this kind (or the pick is no
    longer a speech model of that kind)."""


class SpeechAllowanceUsedUp(RuntimeError):
    """The member's daily allowance for this kind of speech is used up (or
    the tenant switched it off for members)."""


class NotAChatModel(RuntimeError):
    """A completion was asked of a speech model. Raised by the router's
    fallback layer before any call, whatever path pinned the model."""


def refuse_speech_model(cfg: m.ModelConfig) -> None:
    if is_speech_model(cfg):
        raise NotAChatModel(
            f"model {cfg.provider}/{cfg.model} is a {capability_of(cfg).replace('_', '-')} "
            "model and cannot answer a chat"
        )


@dataclass(frozen=True)
class SpeechText:
    text: str
    seconds: float
    provider: str
    model: str
    usage: Usage | None
    #: The seconds reserved for the call (the upload at 16 kbit/s).
    reserved_seconds: int = 0

    @property
    def overshoots(self) -> bool:
        """The provider reported far more audio than the upload's size
        suggested: very low-bitrate or mostly silent audio (audited)."""
        return self.seconds > OVERSHOOT_FACTOR * self.reserved_seconds


@dataclass
class SpeechAudio:
    chunks: AsyncIterator[bytes]
    provider: str
    model: str
    characters: int
    media_type: str = "audio/mpeg"
    #: Closes the provider stream that is open, if any. Idempotent; the route
    #: calls it after the response, on an error and on a client disconnect.
    _close: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)

    async def aclose(self) -> None:
        if self._close is not None:
            await self._close()


async def configured_speech_model(
    db: AsyncSession, *, tenant_id: uuid.UUID, capability: str
) -> m.ModelConfig | None:
    """The tenant's pick for `capability`, read live: None when nothing is
    picked, the model is gone, or it no longer has that capability."""
    organization = await db.get(m.Organization, tenant_id)
    picks = (organization.settings or {}).get(SETTINGS_KEY) if organization else None
    raw = picks.get(SETTING_FOR[capability]) if isinstance(picks, dict) else None
    if not raw:
        return None
    try:
        cfg = await db.get(m.ModelConfig, uuid.UUID(str(raw)))
    except ValueError:
        return None
    if cfg is None or cfg.tenant_id != tenant_id or capability_of(cfg) != capability:
        return None
    return cfg


async def _model_for(db: AsyncSession, *, tenant_id: uuid.UUID, capability: str) -> m.ModelConfig:
    cfg = await configured_speech_model(db, tenant_id=tenant_id, capability=capability)
    if cfg is None:
        raise SpeechNotConfigured(f"no {capability.replace('_', '-')} model is configured")
    return cfg


def daily_limit(organization: m.Organization | None, capability: str) -> int:
    """The tenant's per-member daily allowance in the setting's unit
    (minutes / characters); the default when unset."""
    picks = (organization.settings or {}).get(SETTINGS_KEY) if organization else None
    raw = picks.get(DAILY_LIMIT_FOR[capability]) if isinstance(picks, dict) else None
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return DEFAULT_DAILY_LIMIT[capability]


async def used_today(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID, capability: str
) -> int:
    """Platform units (seconds / characters) the member used today on the
    organisation's clock, on the tenant's speech models of `capability`.
    Filters tenant + `ts` first, the shape `ix_token_usage_record_tenant_ts`
    serves."""
    from oc8.triggers.zones import organisation_timezone

    zone = await organisation_timezone(db, tenant_id)
    local = dt.datetime.now(tz=dt.UTC).astimezone(ZoneInfo(zone))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.UTC)
    names = select(m.ModelConfig.model).where(
        m.ModelConfig.tenant_id == tenant_id,
        m.ModelConfig.params[CAPABILITY_KEY].astext == capability,
    )
    rec = m.TokenUsageRecord
    total = await db.scalar(
        select(func.coalesce(func.sum(rec.platform_units), 0)).where(
            rec.tenant_id == tenant_id,
            rec.ts >= midnight,
            rec.member_id == member_id,
            rec.model.in_(names.scalar_subquery()),
        )
    )
    return int(total or 0)


def _lock_key(tenant_id: uuid.UUID, member_id: uuid.UUID, capability: str) -> str:
    """The advisory-lock key for one member's allowance of one kind, hashed in
    SQL with `hashtextextended(key, 0)` like the repo's other locks."""
    return f"speech:{tenant_id}:{member_id}:{capability}"


async def _reserve(
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID | None,
    capability: str,
    limit: int,
    units: int,
    provider: str,
    model: str,
    agent_id: uuid.UUID | None,
    department_id: uuid.UUID | None,
) -> None:
    """Reserve `units` of the member's day BEFORE the provider is called.

    One short transaction of its own: a per-member advisory lock, today's use
    (every earlier reservation included), the refusal (`SpeechAllowanceUsedUp`)
    when `used + units` passes `limit`, and the reservation itself -- the usage
    row, since usage rows are append-only (migration 0001). Committing releases
    the lock, so neither the lock nor a transaction spans the provider call,
    and a parallel request sees this reservation. Nothing is ever refunded: a
    failed or shorter call keeps its reservation. A call made for nobody has
    no member allowance; its usage row is still written."""
    from oc8.db.session import tenant_session
    from oc8.metering.usage import record_usage

    what = "dictation" if capability == SPEECH_TO_TEXT else "reading aloud"
    unit = "seconds" if capability == SPEECH_TO_TEXT else "characters"
    async with tenant_session(tenant_id) as own:
        if member_id is not None:
            if limit == 0:
                raise SpeechAllowanceUsedUp(
                    f"{what} is switched off for members of this organization"
                )
            await own.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(_lock_key(tenant_id, member_id, capability), 0)
                    )
                )
            )
            used = await used_today(
                own, tenant_id=tenant_id, member_id=member_id, capability=capability
            )
            if used >= limit:
                raise SpeechAllowanceUsedUp(
                    f"today's allowance for {what} is used up; it renews at midnight"
                )
            if used + units > limit:
                raise SpeechAllowanceUsedUp(
                    f"this would pass today's allowance for {what}: "
                    f"{limit - used} of {limit} {unit} are left"
                )
        await record_usage(
            own,
            tenant_id=tenant_id,
            request_id=uuid.uuid4(),
            model=model,
            provider=provider,
            tokens_in=0,
            tokens_out=0,
            platform_units=units,
            agent_id=agent_id,
            department_id=department_id,
            member_id=member_id,
        )


async def _limit(db: AsyncSession, *, tenant_id: uuid.UUID, capability: str) -> int:
    """The member allowance in platform units (seconds / characters)."""
    organization = await db.get(m.Organization, tenant_id)
    return daily_limit(organization, capability) * _UNITS_PER[capability]


async def _admit(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    cfg: m.ModelConfig,
    department_id: uuid.UUID | None,
) -> tuple[str, _SpeechAdapter]:
    """Every check a cloud embedding passes before content leaves, then the
    model's own adapter. Returns (resolved provider, adapter)."""
    from oc8.metering.budget import check_budget
    from oc8.metering.provider_budget import check_provider_budget

    settings = get_settings()
    label = f"{cfg.provider}/{cfg.model}"
    entry = await resolve_provider(db, tenant_id=tenant_id, name=cfg.provider)
    if entry is None:
        raise SpeechUnavailable(f"speech model {label!r}: provider is not enabled for this tenant")
    canonical = entry.canonical
    api_key = await resolve_model_key(
        db, tenant_id=tenant_id, provider=canonical, credential_id=cfg.credential_id
    )
    if settings.require_tenant_model_key and not api_key:
        raise SpeechUnavailable(
            f"speech model {label!r} requires a per-tenant API key "
            f"(credential type '{canonical}_api_key'); none is configured"
        )
    if not entry.available(settings, api_key):
        raise SpeechUnavailable(f"speech model {label!r} has no API key configured")
    if (await check_provider_budget(db, tenant_id=tenant_id, provider=canonical)).exhausted:
        raise SpeechUnavailable(f"speech model {label!r}: the provider's monthly limit is used up")
    if (await check_budget(db, tenant_id=tenant_id, department_id=department_id)).hard_exceeded:
        raise SpeechUnavailable(f"speech model {label!r}: the hard token budget is exceeded")
    base_url = (cfg.params or {}).get("base_url") or await resolve_model_base_url(
        db, tenant_id=tenant_id, provider=canonical, credential_id=cfg.credential_id
    )
    adapter = entry.factory(settings, base_url, api_key)
    if not speaks_openai_audio(adapter):
        raise SpeechUnavailable(f"speech model {label!r}: the provider has no audio endpoints")
    return canonical, adapter  # type: ignore[return-value]


async def _record(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider: str,
    model: str,
    usage: Usage | None,
    platform_units: int,
    member_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    department_id: uuid.UUID | None,
) -> None:
    """One usage row, in a SAVEPOINT: a failed metering write never fails the
    person's dictation or reading (as for embeddings)."""
    from oc8.metering.usage import record_usage

    try:
        async with db.begin_nested():
            await record_usage(
                db,
                tenant_id=tenant_id,
                request_id=uuid.uuid4(),
                model=model,
                provider=provider,
                tokens_in=usage.tokens_in if usage else 0,
                tokens_out=usage.tokens_out if usage else 0,
                # Always: the daily allowance counts these, tokens or not.
                platform_units=platform_units,
                agent_id=agent_id,
                department_id=department_id,
                member_id=member_id,
            )
    except Exception:
        logger.warning(
            "could not record speech usage for provider %s model %s",
            provider,
            model,
            exc_info=True,
        )


def _size(audio: bytes | io.BytesIO) -> int:
    return len(audio) if isinstance(audio, bytes) else audio.getbuffer().nbytes


def estimated_seconds(size: int) -> int:
    """A recording's length by its size at 16 kbit/s, rounded up (at least 1)."""
    return max(1, math.ceil(size / _ESTIMATED_BYTES_PER_SECOND))


async def transcribe(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    audio: bytes | io.BytesIO,
    content_type: str,
    language: str | None,
    member_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
) -> SpeechText:
    """The person's own words, as text. Dictation is like typing, so it is
    allowed whatever the chat holds; only the text comes back.

    Counted so it never undercounts: the upload's size at 16 kbit/s is
    reserved before the call; when the provider reports more seconds, the
    difference is appended after it. A provider reporting fewer seconds
    refunds nothing, so a recording above 16 kbit/s counts by its size.
    Residual risk: the allowance bounds upload BYTES per day (cap minutes x
    16 kbit/s); real audio time can exceed it by the ratio 16 kbit/s : actual
    bitrate -- about 2.7x for constant low-bitrate Opus, up to ~50x for mostly
    silent VBR/DTX audio. Admins can lower the daily cap or switch dictation
    off (0)."""
    cfg = await _model_for(db, tenant_id=tenant_id, capability=SPEECH_TO_TEXT)
    provider, adapter = await _admit(db, tenant_id=tenant_id, cfg=cfg, department_id=department_id)
    reserved = estimated_seconds(_size(audio))
    await _reserve(
        tenant_id=tenant_id,
        member_id=member_id,
        capability=SPEECH_TO_TEXT,
        limit=await _limit(db, tenant_id=tenant_id, capability=SPEECH_TO_TEXT),
        units=reserved,
        provider=provider,
        model=cfg.model,
        agent_id=agent_id,
        department_id=department_id,
    )
    result = await adapter.transcribe(
        audio, model=cfg.model, content_type=content_type, language=language
    )
    beyond = max(0, math.ceil(result.seconds) - reserved)
    if result.usage is not None or beyond:
        await _record(
            db,
            tenant_id=tenant_id,
            provider=provider,
            model=cfg.model,
            usage=result.usage,
            platform_units=beyond,
            member_id=member_id,
            agent_id=agent_id,
            department_id=department_id,
        )
    return SpeechText(
        text=result.text,
        seconds=result.seconds,
        provider=provider,
        model=cfg.model,
        usage=result.usage,
        reserved_seconds=reserved,
    )


_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+|\n+")


def split_for_speech(text: str, *, limit: int = SPEECH_PIECE_CHARS) -> list[str]:
    """`text` in pieces of at most `limit` characters, cut after a sentence
    where one fits, else at a space, else hard."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        sentence = sentence.strip()
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if not sentence:
            continue
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        pieces.append(current)
    return pieces


async def _aclose(stream: AsyncIterator[bytes]) -> None:
    close = getattr(stream, "aclose", None)
    if close is not None:
        await close()


async def synthesize(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    text: str,
    restricted: bool = False,
    member_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    on_error: Callable[[Exception], Awaitable[None]] | None = None,
) -> SpeechAudio:
    """`text` read aloud, as an MP3 stream. A restricted text only reaches a
    local model (the chat's `ClassificationViolation`, raised before any call).

    Its characters are reserved from the member's day before the first call
    and are its usage: rows are append-only, so stopping early refunds
    nothing. The first piece is requested here, so a refusal raises while the
    caller can still answer with an error; the rest follow as the stream is
    read. A later piece that fails ends the stream cleanly (the audio so far
    stays playable) and is handed to `on_error`."""
    cfg = await _model_for(db, tenant_id=tenant_id, capability=TEXT_TO_SPEECH)
    if restricted and cfg.locality != "local":
        raise ClassificationViolation(
            "this answer is restricted and may only be read aloud by a local speech model"
        )
    pieces = split_for_speech(text)
    if not pieces:
        raise ValueError("there is no text to read aloud")
    characters = sum(len(p) for p in pieces)
    provider, adapter = await _admit(db, tenant_id=tenant_id, cfg=cfg, department_id=department_id)
    await _reserve(
        tenant_id=tenant_id,
        member_id=member_id,
        capability=TEXT_TO_SPEECH,
        limit=await _limit(db, tenant_id=tenant_id, capability=TEXT_TO_SPEECH),
        units=characters,
        provider=provider,
        model=cfg.model,
        agent_id=agent_id,
        department_id=department_id,
    )
    extra = (cfg.params or {}).get("extra")
    extra = extra if isinstance(extra, dict) else None
    first = await adapter.open_speech(pieces[0], model=cfg.model, extra=extra)
    open_streams: list[AsyncIterator[bytes]] = [first]

    async def close() -> None:
        while open_streams:
            await _aclose(open_streams.pop())

    async def chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in first:
                yield chunk
            await close()
            for piece in pieces[1:]:
                stream = await adapter.open_speech(piece, model=cfg.model, extra=extra)
                open_streams.append(stream)
                async for chunk in stream:
                    yield chunk
                await close()
        except Exception as exc:
            logger.warning(
                "reading aloud with %s/%s stopped: %s", provider, cfg.model, type(exc).__name__
            )
            if on_error is not None:
                await on_error(exc)
        finally:
            await close()

    return SpeechAudio(
        chunks=chunks(),
        provider=provider,
        model=cfg.model,
        characters=characters,
        _close=close,
    )


# Markdown as a listener should hear it. Line by line and character by
# character on purpose: no regex here may backtrack on an answer's text.

_FENCES: Final = ("```", "~~~")
_RULE_CHARS: Final = frozenset("|-: \t")


def _without_links(line: str) -> str:
    """`[text](url)` and `![alt](url)` as their text, in one pass: every
    search starts where the last one ended, and a missing `]` or `)` ends
    the search for the line."""
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        start = line.find("[", i)
        if start == -1:
            out.append(line[i:])
            break
        bang = start > i and line[start - 1] == "!"
        out.append(line[i : start - 1 if bang else start])
        close = line.find("]", start + 1)
        if close == -1:
            out.append(line[start:])
            break
        if close + 1 < n and line[close + 1] == "(":
            end = line.find(")", close + 2)
            if end == -1:
                out.append(line[start:])
                break
            out.append(line[start + 1 : close])
            i = end + 1
        else:
            out.append(line[start : close + 1])
            i = close + 1
    return "".join(out)


def _without_markers(line: str) -> str:
    """`*` and backticks dropped; `_` dropped where it opens or closes a word
    (`_kursiv_`), kept inside one (`snake_case`)."""
    out: list[str] = []
    n = len(line)
    for i, c in enumerate(line):
        if c in "*`":
            continue
        if c == "_":
            before = line[i - 1] if i > 0 else " "
            after = line[i + 1] if i + 1 < n else " "
            if not (before.isalnum() and after.isalnum()):
                continue
        out.append(c)
    return "".join(out)


def _without_line_marker(line: str) -> str:
    """A quote's `>`s, then a heading's `#`s, then a list bullet."""
    k, n = 0, len(line)
    if k < n and line[k] == ">":
        while k < n and line[k] in "> \t":
            k += 1
    hashes = k
    while hashes < n and line[hashes] == "#":
        hashes += 1
    if 1 <= hashes - k <= 6 and hashes < n and line[hashes] in " \t":
        k = hashes + 1
    if line[k : k + 2] in ("- ", "* ", "+ "):
        k += 2
    return line[k:].strip()


def speakable(text: str) -> str:
    """Markdown as the words a listener should hear: fenced code dropped,
    table rows as their cells (rule lines dropped), links and images as their
    text, and the `#`, `*`, `_`, `>`, backtick and list markers gone.
    Linear in the text's length."""
    lines: list[str] = []
    fence: str | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith(_FENCES):
            fence = stripped[:3]
            continue
        if stripped and "-" in stripped and set(stripped) <= _RULE_CHARS:
            continue
        if stripped.startswith("|") and stripped.count("|") >= 2:
            cells = (_without_markers(_without_links(c)).strip() for c in stripped.split("|"))
            line = ", ".join(c for c in cells if c)
        else:
            line = _without_markers(_without_links(_without_line_marker(stripped)))
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()
