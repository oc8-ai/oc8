"""OpenAI's two audio endpoints, which LiteLLM, vLLM and most self-hosted
speech servers speak too: `POST /audio/transcriptions` (multipart, the audio
as `file`) and `POST /audio/speech` (JSON in, audio bytes out).

Shared by the OpenAI and the OpenAI-compatible adapter; each passes its own
base URL and key. Nothing here keeps the audio: it is sent from memory and the
spoken answer is streamed through.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from oc8.modelrouter.http_errors import araise_for_status_with_body, raise_for_status_with_body
from oc8.modelrouter.types import Usage

_TRANSCRIBE_TIMEOUT_S = 300.0
_SPEECH_TIMEOUT_S = httpx.Timeout(120.0, connect=15.0)

#: The file name sent with the audio, by content type. The client's own name
#: never leaves: the provider only needs the extension to pick a decoder.
_EXTENSIONS: dict[str, str] = {
    "audio/webm": "webm",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/ogg": "ogg",
}


#: (base URL, model) pairs that refused `verbose_json`; asked with `json` only.
_NO_VERBOSE_JSON: set[tuple[str, str]] = set()


class UnexpectedAudioResponse(ValueError):
    """The provider answered, but not in a shape an audio endpoint answers in."""


@dataclass(frozen=True)
class Transcript:
    text: str
    #: The audio's length as the provider reports it; 0.0 when it does not.
    seconds: float
    #: Token usage, where the provider reports tokens; None otherwise.
    usage: Usage | None


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _parse_transcript(data: dict[str, Any]) -> Transcript:
    """`{"text": ..., "usage": {"type": "tokens"|"duration", ...}}`, or the
    older `verbose_json` shape with a top-level `duration`."""
    raw = data.get("usage")
    usage_raw: dict[str, Any] = raw if isinstance(raw, dict) else {}
    usage: Usage | None = None
    seconds = 0.0
    if usage_raw.get("type") == "tokens" or "input_tokens" in usage_raw:
        usage = Usage(
            tokens_in=int(usage_raw.get("input_tokens") or 0),
            tokens_out=int(usage_raw.get("output_tokens") or 0),
        )
    if usage_raw.get("seconds") is not None:
        seconds = float(usage_raw["seconds"])
    elif data.get("duration") is not None:
        seconds = float(data["duration"])
    return Transcript(text=str(data.get("text") or "").strip(), seconds=seconds, usage=usage)


async def post_transcription(
    base_url: str,
    api_key: str,
    audio: bytes | io.BytesIO,
    *,
    model: str,
    content_type: str,
    language: str | None,
) -> Transcript:
    """The recording as text, with its length where the provider says it.

    `verbose_json` is asked first: it carries `duration`, which the member's
    daily allowance counts. A model that refuses that format (a 4xx naming
    `response_format`, as gpt-4o-transcribe answers) is asked again once with
    `json`, and remembered per (base URL, model) for this process."""
    key = (base_url, model)
    formats = ("json",) if key in _NO_VERBOSE_JSON else ("verbose_json", "json")
    filename = f"audio.{_EXTENSIONS.get(content_type, 'webm')}"
    async with httpx.AsyncClient(timeout=_TRANSCRIBE_TIMEOUT_S) as client:
        for response_format in formats:
            fields: dict[str, str] = {"model": model, "response_format": response_format}
            if language:
                fields["language"] = language
            if isinstance(audio, io.BytesIO):
                audio.seek(0)
            resp = await client.post(
                f"{base_url}/audio/transcriptions",
                data=fields,
                files={"file": (filename, audio, content_type)},
                headers=_auth(api_key),
            )
            if (
                response_format == "verbose_json"
                and 400 <= resp.status_code < 500
                and "response_format" in resp.text
            ):
                _NO_VERBOSE_JSON.add(key)
                continue
            raise_for_status_with_body(resp)
            try:
                data = resp.json()
            except ValueError as exc:
                raise UnexpectedAudioResponse(
                    f"the transcription answer of model {model!r} is not JSON"
                ) from exc
            if not isinstance(data, dict):
                raise UnexpectedAudioResponse(
                    f"unexpected transcription response for model {model!r}"
                )
            return _parse_transcript(data)
    raise UnexpectedAudioResponse(f"model {model!r} took no transcription format")


async def open_speech_stream(
    base_url: str,
    api_key: str,
    text: str,
    *,
    model: str,
    extra: dict[str, Any] | None,
) -> AsyncIterator[bytes]:
    """The spoken `text` as MP3 bytes, streamed as the provider sends them.

    The request is sent and its status checked here, before anything is
    returned: a refused call raises now, while the caller can still answer
    with an error instead of a broken audio stream. `extra` (the model's raw
    params, e.g. `voice`, `speed`) is forwarded; the format stays MP3."""
    payload: dict[str, Any] = {"model": model, "input": text, "voice": "alloy", **(extra or {})}
    payload["response_format"] = "mp3"
    client = httpx.AsyncClient(timeout=_SPEECH_TIMEOUT_S)
    try:
        request = client.build_request(
            "POST",
            f"{base_url}/audio/speech",
            json=payload,
            headers={**_auth(api_key), "content-type": "application/json"},
        )
        resp = await client.send(request, stream=True)
        await araise_for_status_with_body(resp)
    except BaseException:
        await client.aclose()
        raise

    async def chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return chunks()
