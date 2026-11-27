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
adapter's) and the model; when the provider reports no tokens, the audio
seconds (rounded up) or the characters are its platform units. Audio is never
stored.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final, Protocol

from sqlalchemy import ColumnElement, func
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
        self, audio: bytes, *, model: str, content_type: str, language: str | None
    ) -> Transcript: ...

    async def open_speech(
        self, text: str, *, model: str, extra: dict[str, Any] | None
    ) -> AsyncIterator[bytes]: ...


class SpeechUnavailable(RuntimeError):
    """The speech model cannot be called right now; the message says why."""


class SpeechNotConfigured(SpeechUnavailable):
    """The tenant has picked no speech model of this kind (or the pick is no
    longer a speech model of that kind)."""


@dataclass(frozen=True)
class SpeechText:
    text: str
    seconds: float
    provider: str
    model: str
    usage: Usage | None


@dataclass(frozen=True)
class SpeechAudio:
    chunks: AsyncIterator[bytes]
    provider: str
    model: str
    characters: int
    media_type: str = "audio/mpeg"


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
                platform_units=0 if usage else platform_units,
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


async def transcribe(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    audio: bytes,
    content_type: str,
    language: str | None,
    member_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
) -> SpeechText:
    """The person's own words, as text. Dictation is like typing, so it is
    allowed whatever the chat holds; only the text comes back."""
    cfg = await _model_for(db, tenant_id=tenant_id, capability=SPEECH_TO_TEXT)
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
        platform_units=math.ceil(result.seconds),
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


async def synthesize(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    text: str,
    restricted: bool = False,
    member_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
) -> SpeechAudio:
    """`text` read aloud, as an MP3 stream. A restricted text only reaches a
    local model (the chat's `ClassificationViolation`, raised before any call).

    The first piece is requested here, so a refusal raises while the caller
    can still answer with an error; the rest follow as the stream is read.
    Usage is recorded once the provider accepted the first piece: it bills
    the characters it was sent, read to the end or not."""
    cfg = await _model_for(db, tenant_id=tenant_id, capability=TEXT_TO_SPEECH)
    if restricted and cfg.locality != "local":
        raise ClassificationViolation(
            "this answer is restricted and may only be read aloud by a local speech model"
        )
    provider, adapter = await _admit(db, tenant_id=tenant_id, cfg=cfg, department_id=department_id)
    pieces = split_for_speech(text)
    if not pieces:
        raise ValueError("there is no text to read aloud")
    extra = (cfg.params or {}).get("extra")
    extra = extra if isinstance(extra, dict) else None
    first = await adapter.open_speech(pieces[0], model=cfg.model, extra=extra)
    characters = sum(len(p) for p in pieces)
    await _record(
        db,
        tenant_id=tenant_id,
        provider=provider,
        model=cfg.model,
        usage=None,
        platform_units=characters,
        member_id=member_id,
        agent_id=agent_id,
        department_id=department_id,
    )

    async def chunks() -> AsyncIterator[bytes]:
        async for chunk in first:
            yield chunk
        for piece in pieces[1:]:
            async for chunk in await adapter.open_speech(piece, model=cfg.model, extra=extra):
                yield chunk

    return SpeechAudio(chunks=chunks(), provider=provider, model=cfg.model, characters=characters)
