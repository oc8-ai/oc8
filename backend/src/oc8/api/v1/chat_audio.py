"""Voice in the chat (spec 2026-11-27 §5.4, §9): dictation and reading an
answer aloud, through the tenant's speech models (`oc8.modelrouter.speech`).

- `GET /chat/audio/capabilities`: which voice buttons the chat shows.
- `POST /chat/audio/transcribe`: multipart `file` (one recording, at most
  25 MB), optional `session_id` and `language` -> `{"text": ...}`. The text is
  put into the composer for the person to review; nothing is sent.
- `POST /chat/sessions/{sid}/messages/{mid}/speech`: the answer as an
  `audio/mpeg` stream. A restricted answer needs a local speech model (409).

Both calls are the chat owner's alone (another member gets 404), metered for
the person and audited with duration / characters and the model -- never the
words. Audio is never stored: the upload is read from the request into a
size-checked buffer in memory (not a spooled temp file) and the spoken answer
is streamed through.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from python_multipart.multipart import MultipartParser, parse_options_header

from oc8 import models as m
from oc8.api.deps import DbSession, require_departmental
from oc8.api.v1._chat_common import session_owned_by_caller
from oc8.audit import append_event
from oc8.authz.permissions import AGENT, COPILOT_USE, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.modelrouter.router import ClassificationViolation
from oc8.modelrouter.speech import (
    SPEECH_TO_TEXT,
    TEXT_TO_SPEECH,
    SpeechNotConfigured,
    SpeechUnavailable,
    configured_speech_model,
    synthesize,
    transcribe,
)
from oc8.schemas.base import CamelModel

router = APIRouter()

#: The same admission as every other chat route (`api/v1/chat.py`).
_ChatActor = Annotated[
    HumanActor, Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE))
]

#: OpenAI's own limit for one transcription upload.
MAX_AUDIO_BYTES = 25 * 1024 * 1024
#: The multipart envelope around the audio: boundaries, headers, two fields.
_MAX_ENVELOPE_BYTES = 64 * 1024
_MAX_BODY_BYTES = MAX_AUDIO_BYTES + _MAX_ENVELOPE_BYTES

#: What a browser's MediaRecorder (or an uploaded voice memo) records.
AUDIO_TYPES = frozenset({"audio/webm", "audio/mp4", "audio/mpeg", "audio/wav", "audio/ogg"})

_LANGUAGE = re.compile(r"^[a-z]{2,3}$")


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


async def _read_capped(request: Request) -> bytes:
    """The request body, refused (413) as soon as it passes the cap: the
    declared length is checked before reading, the bytes read while reading."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
        raise _too_large()
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise _too_large()
    return bytes(body)


@dataclass
class _Part:
    headers: dict[bytes, bytes] = field(default_factory=dict)
    data: bytearray = field(default_factory=bytearray)


def _form_parts(content_type: str, body: bytes) -> dict[str, _Part]:
    """`multipart/form-data` parsed in memory, by field name (first wins)."""
    kind, options = parse_options_header(content_type)
    boundary = options.get(b"boundary")
    if kind != b"multipart/form-data" or not boundary:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "expected multipart/form-data with a file"
        )
    parts: list[_Part] = []
    header_field = bytearray()
    header_value = bytearray()

    def on_part_begin() -> None:
        parts.append(_Part())

    def on_part_data(data: bytes, start: int, end: int) -> None:
        part = parts[-1]
        part.data.extend(data[start:end])
        if len(part.data) > MAX_AUDIO_BYTES:
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
    try:
        parser.write(body)
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
    value = bytes(part.data).decode("utf-8", "replace").strip()
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
    return HTTPException(status.HTTP_502_BAD_GATEWAY, "the speech provider could not be reached")


_PROVIDER_ERRORS = (SpeechUnavailable, ClassificationViolation, httpx.HTTPError, ValueError)


@router.get("/chat/audio/capabilities", response_model=AudioCapabilities)
async def audio_capabilities(db: DbSession, actor: _ChatActor) -> AudioCapabilities:
    """Which voice buttons the chat shows: none without a configured model."""
    tenant_id = actor.principal.tenant_id
    stt = await configured_speech_model(db, tenant_id=tenant_id, capability=SPEECH_TO_TEXT)
    tts = await configured_speech_model(db, tenant_id=tenant_id, capability=TEXT_TO_SPEECH)
    return AudioCapabilities(
        speech_to_text=stt is not None,
        text_to_speech=tts is not None,
        text_to_speech_local=tts is not None and tts.locality == "local",
    )


@router.post("/chat/audio/transcribe", response_model=Transcription)
async def transcribe_audio(request: Request, db: DbSession, actor: _ChatActor) -> Transcription:
    """The person's recording as text. Dictation is the person's own words,
    like typing, so it is allowed in a chat that holds restricted content."""
    parts = _form_parts(request.headers.get("content-type", ""), await _read_capped(request))
    audio_part = parts.get("file")
    if audio_part is None or not audio_part.data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a recording is required")
    content_type = _audio_type(audio_part)
    language = _field(parts, "language")
    if language is not None and not _LANGUAGE.match(language):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "language must be like 'de'")
    session: m.ChatSession | None = None
    raw_session = _field(parts, "session_id")
    if raw_session is not None:
        try:
            session_id = uuid.UUID(raw_session)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid session_id") from exc
        session = await session_owned_by_caller(session_id, db, actor)
    agent = await db.get(m.Agent, session.agent_id) if session is not None else None
    try:
        result = await transcribe(
            db,
            tenant_id=actor.principal.tenant_id,
            audio=bytes(audio_part.data),
            content_type=content_type,
            language=language,
            member_id=actor.member.id,
            agent_id=agent.id if agent is not None else None,
            department_id=agent.department_id if agent is not None else None,
        )
    except _PROVIDER_ERRORS as exc:
        raise _speech_error(exc) from exc
    await append_event(
        db,
        tenant_id=actor.principal.tenant_id,
        actor_type="operator",
        actor_id=actor.member.id,
        category="chat",
        action="chat.audio.transcribed",
        resource={
            "session_id": str(session.id) if session is not None else None,
            # The key `GET /audit?chat_session_id=` reads, as on chat.feedback.
            "chat_session_id": str(session.id) if session is not None else None,
            # Names the subject, so the audit read treats a personal agent's
            # event as personal (PR #187).
            "agent_id": str(agent.id) if agent is not None else None,
            "department_id": str(agent.department_id) if agent is not None else None,
            "provider": result.provider,
            "model": result.model,
            "seconds": result.seconds,
            "bytes": len(audio_part.data),
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
    """One answer read aloud. Everything that touches the database (the
    gates, the usage row, the audit event) is done before the stream starts;
    the stream itself only relays the provider's audio."""
    session = await session_owned_by_caller(session_id, db, actor)
    msg = await db.get(m.ChatMessage, message_id)
    if msg is None or msg.session_id != session.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    if msg.role != "assistant":
        raise HTTPException(status.HTTP_409_CONFLICT, "only an answer can be read aloud")
    if not (msg.content or "").strip():
        raise HTTPException(status.HTTP_409_CONFLICT, "this answer has no text to read aloud")
    agent = await db.get(m.Agent, session.agent_id)
    try:
        spoken = await synthesize(
            db,
            tenant_id=actor.principal.tenant_id,
            text=msg.content,
            restricted=bool(msg.restricted),
            member_id=actor.member.id,
            agent_id=session.agent_id,
            department_id=agent.department_id if agent is not None else None,
        )
    except _PROVIDER_ERRORS as exc:
        raise _speech_error(exc) from exc
    await append_event(
        db,
        tenant_id=actor.principal.tenant_id,
        actor_type="operator",
        actor_id=actor.member.id,
        category="chat",
        action="chat.audio.spoken",
        resource={
            "session_id": str(session.id),
            "chat_session_id": str(session.id),
            "message_id": str(msg.id),
            "run_id": str(msg.run_id) if msg.run_id else None,
            "agent_id": str(session.agent_id),
            "department_id": str(agent.department_id) if agent is not None else None,
            "provider": spoken.provider,
            "model": spoken.model,
            "characters": spoken.characters,
        },
        principal=actor.principal,
    )
    await db.commit()
    return StreamingResponse(
        spoken.chunks,
        media_type=spoken.media_type,
        # Never cached, by the browser or the service worker: the audio is
        # the answer's words.
        headers={"Cache-Control": "no-store"},
    )
