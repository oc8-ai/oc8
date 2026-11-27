"""Voice in the chat (spec 2026-11-27 §5.4, §9): dictation and reading an
answer aloud, through the tenant's speech models (`oc8.modelrouter.speech`).

- `GET /chat/audio/capabilities`: which voice buttons the chat shows.
- `POST /chat/audio/transcribe`: multipart `file` (one recording, at most
  12 MB), `session_id` (required) and optional `language` -> `{"text": ...}`.
  The text is put into the composer for the person to review; nothing is sent.
- `POST /chat/sessions/{sid}/messages/{mid}/speech`: the answer as an
  `audio/mpeg` stream, its markdown read as plain words, at most 20 000
  characters. A restricted answer needs a local speech model (409).

Both calls are the chat owner's alone (another member gets 404), metered for
the person and the chat's agent, bounded by the member's daily allowance
(429), and audited with duration / characters and the model -- never the
words. Audio is never stored: the upload is parsed as it arrives into one
size-checked in-memory buffer (not a spooled temp file) and the spoken answer
is streamed through.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.background import BackgroundTask

from oc8 import models as m
from oc8.api.deps import DbSession, require_departmental
from oc8.api.v1._chat_common import session_owned_by_caller
from oc8.audit import append_event
from oc8.authz.permissions import AGENT, COPILOT_USE, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.modelrouter.adapters._openai_audio import UnexpectedAudioResponse
from oc8.modelrouter.router import ClassificationViolation
from oc8.modelrouter.speech import (
    MAX_SPOKEN_CHARS,
    SPEECH_TO_TEXT,
    TEXT_TO_SPEECH,
    SpeechAllowanceUsedUp,
    SpeechNotConfigured,
    SpeechUnavailable,
    configured_speech_model,
    daily_limit,
    speakable,
    synthesize,
    transcribe,
)
from oc8.schemas.base import CamelModel

router = APIRouter()

#: The same admission as every other chat route (`api/v1/chat.py`).
_ChatActor = Annotated[
    HumanActor, Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE))
]

#: Below OpenAI's 25 MB: 12 MB reserves about 100 minutes at the 16 kbit/s
#: the allowance assumes (it bounds upload bytes; see `oc8.modelrouter.speech`).
MAX_AUDIO_BYTES = 12 * 1024 * 1024
#: The multipart envelope around the audio: boundaries, headers, two fields.
_MAX_ENVELOPE_BYTES = 64 * 1024
_MAX_BODY_BYTES = MAX_AUDIO_BYTES + _MAX_ENVELOPE_BYTES

#: What a browser's MediaRecorder (or an uploaded voice memo) records.
AUDIO_TYPES = frozenset({"audio/webm", "audio/mp4", "audio/mpeg", "audio/wav", "audio/ogg"})

_LANGUAGE = re.compile(r"^[a-z]{2,3}$")

#: An answer longer than this is refused before its markdown is stripped.
_MAX_RAW_CHARS = 4 * MAX_SPOKEN_CHARS


def _too_long() -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        f"this answer is too long to read aloud (at most {MAX_SPOKEN_CHARS} characters)",
    )


class AudioCapabilities(CamelModel):
    speech_to_text: bool
    text_to_speech: bool
    #: Whether the text-to-speech model is local, so a restricted answer may be read.
    text_to_speech_local: bool


class Transcription(CamelModel):
    text: str


def _too_large() -> HTTPException:
    return HTTPException(
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        f"a recording may be at most {MAX_AUDIO_BYTES // (1024 * 1024)} MB",
    )


@dataclass
class _Part:
    headers: dict[bytes, bytes] = field(default_factory=dict)
    #: The part's bytes. For the recording this is the one buffer: it is
    #: handed to the provider call as is, not copied into `bytes`.
    data: io.BytesIO = field(default_factory=io.BytesIO)


async def _form_parts(request: Request) -> dict[str, _Part]:
    """`multipart/form-data` parsed in memory as it arrives, by field name
    (first wins). Refused (413) as soon as it passes the cap: the declared
    length before reading, the bytes read while reading (a chunked upload
    has no length to declare)."""
    kind, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if kind != b"multipart/form-data" or not boundary:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "expected multipart/form-data with a file"
        )
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
        raise _too_large()
    parts: list[_Part] = []
    header_field = bytearray()
    header_value = bytearray()

    def on_part_begin() -> None:
        parts.append(_Part())

    def on_part_data(data: bytes, start: int, end: int) -> None:
        part = parts[-1]
        part.data.write(data[start:end])
        if part.data.tell() > MAX_AUDIO_BYTES:
            raise _too_large()

    def on_header_field(data: bytes, start: int, end: int) -> None:
        header_field.extend(data[start:end])

    def on_header_value(data: bytes, start: int, end: int) -> None:
        header_value.extend(data[start:end])

    def on_header_end() -> None:
        parts[-1].headers[bytes(header_field).lower()] = bytes(header_value)
        header_field.clear()
        header_value.clear()

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": on_part_begin,
            "on_part_data": on_part_data,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
        },
    )
    read = 0
    try:
        async for chunk in request.stream():
            read += len(chunk)
            if read > _MAX_BODY_BYTES:
                raise _too_large()
            parser.write(chunk)
        parser.finalize()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "the upload is not valid multipart/form-data"
        ) from exc
    named: dict[str, _Part] = {}
    for part in parts:
        _disposition, params = parse_options_header(part.headers.get(b"content-disposition", b""))
        name = params.get(b"name", b"").decode("utf-8", "replace")
        named.setdefault(name, part)
    return named


def _field(parts: dict[str, _Part], name: str) -> str | None:
    part = parts.get(name)
    if part is None:
        return None
    value = part.data.getvalue().decode("utf-8", "replace").strip()
    return value or None


def _audio_type(part: _Part) -> str:
    """The recording's media type without parameters (`audio/webm;codecs=opus`
    is `audio/webm`), refused (415) unless it is one of `AUDIO_TYPES`."""
    kind, _options = parse_options_header(part.headers.get(b"content-type", b""))
    media_type = kind.decode("latin-1").lower()
    if media_type not in AUDIO_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"expected a recording ({', '.join(sorted(AUDIO_TYPES))})",
        )
    return media_type


def _speech_error(exc: Exception) -> HTTPException:
    """The person's answer for a failed speech call. A provider failure names
    at most its status or its exception's class: its message may carry a URL
    or a key."""
    if isinstance(exc, SpeechAllowanceUsedUp):
        return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc))
    if isinstance(exc, SpeechNotConfigured):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, ClassificationViolation):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, SpeechUnavailable):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    if isinstance(exc, httpx.HTTPStatusError):
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"the speech provider refused the call (HTTP {exc.response.status_code})",
        )
    if isinstance(exc, httpx.TransportError):
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY, "the speech provider could not be reached"
        )
    if isinstance(exc, (UnexpectedAudioResponse, json.JSONDecodeError)):
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY, "the speech provider answered unexpectedly"
        )
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY, f"the speech provider call failed ({type(exc).__name__})"
    )


#: What a speech call raises on purpose or the provider's HTTP client raises.
#: Anything else (a KeyError, a database error) is our bug and stays a 500.
_PROVIDER_ERRORS = (
    SpeechAllowanceUsedUp,
    SpeechUnavailable,
    ClassificationViolation,
    UnexpectedAudioResponse,
    json.JSONDecodeError,
    httpx.HTTPError,
    httpx.InvalidURL,
    httpx.StreamError,
)


def _is_provider_error(exc: Exception) -> bool:
    return isinstance(exc, _PROVIDER_ERRORS)


#: Detached audit writes still running; held so they are not collected.
_PENDING: set[asyncio.Task[None]] = set()


async def _audit_detached(tenant_id: uuid.UUID, **event: Any) -> None:
    """`append_event` on a session of its own, as a detached task awaited
    through a shield: it runs while the answer streams, after the request's
    transaction committed, and survives the client going away."""
    from oc8.db.session import tenant_session

    async def write() -> None:
        async with tenant_session(tenant_id) as own:
            await append_event(own, tenant_id=tenant_id, **event)

    task = asyncio.ensure_future(write())
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    await asyncio.shield(task)


@router.get("/chat/audio/capabilities", response_model=AudioCapabilities)
async def audio_capabilities(db: DbSession, actor: _ChatActor) -> AudioCapabilities:
    """Which voice buttons the chat shows: none without a configured model,
    nor where the tenant set the member allowance to 0."""
    tenant_id = actor.principal.tenant_id
    organization = await db.get(m.Organization, tenant_id)
    stt = await configured_speech_model(db, tenant_id=tenant_id, capability=SPEECH_TO_TEXT)
    tts = await configured_speech_model(db, tenant_id=tenant_id, capability=TEXT_TO_SPEECH)
    stt_on = stt is not None and daily_limit(organization, SPEECH_TO_TEXT) > 0
    tts_on = tts is not None and daily_limit(organization, TEXT_TO_SPEECH) > 0
    return AudioCapabilities(
        speech_to_text=stt_on,
        text_to_speech=tts_on,
        text_to_speech_local=tts_on and tts is not None and tts.locality == "local",
    )


@router.post("/chat/audio/transcribe", response_model=Transcription)
async def transcribe_audio(request: Request, db: DbSession, actor: _ChatActor) -> Transcription:
    """The person's recording as text, into one of the person's own chats.
    Dictation is the person's own words, like typing, so it is allowed in a
    chat that holds restricted content."""
    parts = await _form_parts(request)
    audio_part = parts.get("file")
    if audio_part is None or not audio_part.data.tell():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a recording is required")
    content_type = _audio_type(audio_part)
    language = _field(parts, "language")
    if language is not None and not _LANGUAGE.match(language):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "language must be like 'de'")
    raw_session = _field(parts, "session_id")
    if raw_session is None:
        # Usage and the audit event always name the chat's agent.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "session_id is required")
    try:
        session_id = uuid.UUID(raw_session)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid session_id") from exc
    session = await session_owned_by_caller(session_id, db, actor)
    agent = await db.get(m.Agent, session.agent_id)
    size = audio_part.data.tell()
    audio_part.data.seek(0)
    try:
        result = await transcribe(
            db,
            tenant_id=actor.principal.tenant_id,
            audio=audio_part.data,
            content_type=content_type,
            language=language,
            member_id=actor.member.id,
            agent_id=session.agent_id,
            department_id=agent.department_id if agent is not None else None,
        )
    except Exception as exc:
        if not _is_provider_error(exc):
            raise
        raise _speech_error(exc) from exc
    finally:
        audio_part.data.close()
    await append_event(
        db,
        tenant_id=actor.principal.tenant_id,
        actor_type="operator",
        actor_id=actor.member.id,
        category="chat",
        action="chat.audio.transcribed",
        resource={
            "session_id": str(session.id),
            # The key `GET /audit?chat_session_id=` reads, as on chat.feedback.
            "chat_session_id": str(session.id),
            # Names the subject, so the audit read treats a personal agent's
            # event as personal (PR #187).
            "agent_id": str(session.agent_id),
            "department_id": str(agent.department_id) if agent is not None else None,
            "provider": result.provider,
            "model": result.model,
            "seconds": result.seconds,
            "bytes": size,
        },
        principal=actor.principal,
    )
    if result.overshoots:
        # The allowance bounds upload bytes, not minutes: very low-bitrate or
        # mostly silent audio holds far more time than it reserved. Not
        # stopped (an accepted residual), but shown to an admin.
        await append_event(
            db,
            tenant_id=actor.principal.tenant_id,
            actor_type="operator",
            actor_id=actor.member.id,
            category="chat",
            action="chat.audio.allowance_overshoot",
            resource={
                "session_id": str(session.id),
                "chat_session_id": str(session.id),
                "agent_id": str(session.agent_id),
                "department_id": str(agent.department_id) if agent is not None else None,
                "model": result.model,
                "reserved_seconds": result.reserved_seconds,
                "reported_seconds": result.seconds,
            },
            principal=actor.principal,
        )
    await db.commit()
    return Transcription(text=result.text)


@router.post(
    "/chat/sessions/{session_id}/messages/{message_id}/speech",
    response_class=StreamingResponse,
    responses={200: {"content": {"audio/mpeg": {}}}},
)
async def speak_message(
    session_id: uuid.UUID, message_id: uuid.UUID, db: DbSession, actor: _ChatActor
) -> StreamingResponse:
    """One answer read aloud. The gates, the first piece's usage row and the
    audit event are written before the stream starts; a later piece is billed
    when the provider accepts it, and a later piece that fails ends the audio
    and is audited as an error."""
    session = await session_owned_by_caller(session_id, db, actor)
    msg = await db.get(m.ChatMessage, message_id)
    if msg is None or msg.session_id != session.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    if msg.role != "assistant":
        raise HTTPException(status.HTTP_409_CONFLICT, "only an answer can be read aloud")
    content = msg.content or ""
    # Checked before stripping too, so no markdown pass sees an oversized text.
    if len(content) > _MAX_RAW_CHARS:
        raise _too_long()
    text = speakable(content)
    if not text:
        raise HTTPException(status.HTTP_409_CONFLICT, "this answer has no text to read aloud")
    if len(text) > MAX_SPOKEN_CHARS:
        raise _too_long()
    agent = await db.get(m.Agent, session.agent_id)
    tenant_id = actor.principal.tenant_id
    resource = {
        "session_id": str(session.id),
        "chat_session_id": str(session.id),
        "message_id": str(msg.id),
        "run_id": str(msg.run_id) if msg.run_id else None,
        "agent_id": str(session.agent_id),
        "department_id": str(agent.department_id) if agent is not None else None,
    }
    principal = actor.principal
    member_id = actor.member.id

    async def on_error(exc: Exception) -> None:
        await _audit_detached(
            tenant_id,
            actor_type="operator",
            actor_id=member_id,
            category="chat",
            action="chat.audio.speech_failed",
            resource={**resource, "error": type(exc).__name__},
            decision="error",
            principal=principal,
        )

    try:
        spoken = await synthesize(
            db,
            tenant_id=tenant_id,
            text=text,
            restricted=bool(msg.restricted),
            member_id=member_id,
            agent_id=session.agent_id,
            department_id=agent.department_id if agent is not None else None,
            on_error=on_error,
        )
    except Exception as exc:
        if not _is_provider_error(exc):
            raise
        raise _speech_error(exc) from exc
    try:
        await append_event(
            db,
            tenant_id=tenant_id,
            actor_type="operator",
            actor_id=member_id,
            category="chat",
            action="chat.audio.spoken",
            resource={
                **resource,
                "provider": spoken.provider,
                "model": spoken.model,
                "characters": spoken.characters,
            },
            principal=principal,
        )
        await db.commit()
    except BaseException:
        await spoken.aclose()
        raise
    return StreamingResponse(
        spoken.chunks,
        media_type=spoken.media_type,
        # Never cached, by the browser or the service worker: the audio is
        # the answer's words.
        headers={"Cache-Control": "no-store"},
        # Runs after the response, also when the client went away mid-answer:
        # the provider stream that is still open is closed, not left to GC.
        background=BackgroundTask(spoken.aclose),
    )
