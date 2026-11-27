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
text only reaches a local model. Each call writes one usage row naming the
person, the provider the router resolved (a plugin provider's own name, not its
adapter's) and the model, with the audio seconds (rounded up) or the
characters as its platform units -- also when the provider bills tokens, since
the per-member daily allowance counts them. Audio is never stored.

Speech costs show 0 until pricing reads platform units, so the budgets that
sum tokens do not see it; what bounds the spend is the per-member daily
allowance (`check_allowance`): minutes of dictation and characters read
aloud per member per day on the organisation's clock, set per tenant in the
speech settings (0 switches the feature off for members).
"""

from __future__ import annotations

import asyncio
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

#: A recording's length when the provider names none: MediaRecorder's default
#: Opus bitrate (128 kbit/s) -- a low-bitrate recording counts short.
_ESTIMATED_BYTES_PER_SECOND: Final = 16_000


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


async def check_allowance(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID | None,
    capability: str,
    wanted: int = 0,
) -> None:
    """Refuse (`SpeechAllowanceUsedUp`) before the provider call when the
    member's day is used up, or would be by `wanted` more units. A call made
    for nobody has no member allowance; the tenant budgets still apply."""
    if member_id is None:
        return
    organization = await db.get(m.Organization, tenant_id)
    limit = daily_limit(organization, capability) * _UNITS_PER[capability]
    what = "dictation" if capability == SPEECH_TO_TEXT else "reading aloud"
    unit = "seconds" if capability == SPEECH_TO_TEXT else "characters"
    if limit == 0:
        raise SpeechAllowanceUsedUp(f"{what} is switched off for members of this organization")
    used = await used_today(db, tenant_id=tenant_id, member_id=member_id, capability=capability)
    if used >= limit:
        raise SpeechAllowanceUsedUp(
            f"today's allowance for {what} is used up; it renews at midnight"
        )
    if used + wanted > limit:
        raise SpeechAllowanceUsedUp(
            f"this would pass today's allowance for {what}: "
            f"{limit - used} of {limit} {unit} are left"
        )


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


#: Detached usage writes still running; held so they are not collected.
_PENDING: set[asyncio.Task[None]] = set()


async def _record_detached(**kwargs: Any) -> None:
    """`_record` on a session of its own, as a detached task awaited through a
    shield (the LLM gateway's `_bill`): it runs while the answer streams,
    after the request's transaction committed, and must survive the client
    disconnecting mid-answer."""
    from oc8.db.session import tenant_session

    async def write() -> None:
        async with tenant_session(kwargs["tenant_id"]) as own:
            await _record(own, **kwargs)

    task = asyncio.ensure_future(write())
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    await asyncio.shield(task)


def _size(audio: bytes | io.BytesIO) -> int:
    return len(audio) if isinstance(audio, bytes) else audio.getbuffer().nbytes


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
    allowed whatever the chat holds; only the text comes back."""
    cfg = await _model_for(db, tenant_id=tenant_id, capability=SPEECH_TO_TEXT)
    await check_allowance(db, tenant_id=tenant_id, member_id=member_id, capability=SPEECH_TO_TEXT)
    provider, adapter = await _admit(db, tenant_id=tenant_id, cfg=cfg, department_id=department_id)
    result = await adapter.transcribe(
        audio, model=cfg.model, content_type=content_type, language=language
    )
    await _record(
        db,
        tenant_id=tenant_id,
        provider=provider,
        model=cfg.model,
        usage=result.usage,
        platform_units=math.ceil(result.seconds)
        or max(1, math.ceil(_size(audio) / _ESTIMATED_BYTES_PER_SECOND)),
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

    The first piece is requested here, so a refusal raises while the caller
    can still answer with an error; the rest follow as the stream is read.
    Each piece is billed once the provider accepted it, so stopping early
    bills only what was sent. A later piece that fails ends the stream
    cleanly (the audio so far stays playable) and is handed to `on_error`."""
    cfg = await _model_for(db, tenant_id=tenant_id, capability=TEXT_TO_SPEECH)
    if restricted and cfg.locality != "local":
        raise ClassificationViolation(
            "this answer is restricted and may only be read aloud by a local speech model"
        )
    pieces = split_for_speech(text)
    if not pieces:
        raise ValueError("there is no text to read aloud")
    characters = sum(len(p) for p in pieces)
    await check_allowance(
        db,
        tenant_id=tenant_id,
        member_id=member_id,
        capability=TEXT_TO_SPEECH,
        wanted=characters,
    )
    provider, adapter = await _admit(db, tenant_id=tenant_id, cfg=cfg, department_id=department_id)
    extra = (cfg.params or {}).get("extra")
    extra = extra if isinstance(extra, dict) else None
    billing: dict[str, Any] = {
        "tenant_id": tenant_id,
        "provider": provider,
        "model": cfg.model,
        "usage": None,
        "member_id": member_id,
        "agent_id": agent_id,
        "department_id": department_id,
    }
    first = await adapter.open_speech(pieces[0], model=cfg.model, extra=extra)
    open_streams: list[AsyncIterator[bytes]] = [first]

    async def close() -> None:
        while open_streams:
            await _aclose(open_streams.pop())

    try:
        await _record(db, platform_units=len(pieces[0]), **billing)
    except BaseException:
        await close()
        raise

    async def chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in first:
                yield chunk
            await close()
            for piece in pieces[1:]:
                stream = await adapter.open_speech(piece, model=cfg.model, extra=extra)
                open_streams.append(stream)
                await _record_detached(platform_units=len(piece), **billing)
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


_FENCE = re.compile(r"^[ \t]*(```|~~~)[^\n]*\n.*?^[ \t]*\1[^\n]*$", re.MULTILINE | re.DOTALL)
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", re.MULTILINE)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1")
_LINE_MARKER = re.compile(r"^[ \t]*(#{1,6}[ \t]+|>+[ \t]?|[-*+][ \t]+)", re.MULTILINE)


def speakable(text: str) -> str:
    """Markdown as the words a listener should hear: fenced code dropped,
    table rows as their cells, links and images as their text, and the
    `#`, `*`, `_`, `>` and list markers gone."""
    text = _FENCE.sub("", text)
    text = _TABLE_RULE.sub("", text)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            line = ", ".join(c for c in cells if c)
        lines.append(line)
    text = "\n".join(lines)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _EMPHASIS.sub(r"\2", text)
    text = _LINE_MARKER.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
