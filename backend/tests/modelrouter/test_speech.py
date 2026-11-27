"""Speech models behind the model router: dictation and reading answers aloud.

A model config declares `params.capability` = `speech_to_text` or
`text_to_speech`; the tenant picks one of each. The call goes to the
OpenAI-compatible `/audio/transcriptions` / `/audio/speech` endpoints with the
tenant's own credential, with no fallback to another model, and is metered
like a completion: one usage row naming the person, the provider the router
resolved and the model, with seconds / characters as platform units when the
provider reports no tokens. Audio is never stored.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.credentials.service import create_credential
from oc8.modelrouter import ClassificationViolation, registry
from oc8.modelrouter import speech as speech_mod
from oc8.modelrouter.adapters.openai import OpenAIAdapter
from oc8.modelrouter.adapters.openai_compatible import OpenAICompatibleAdapter
from oc8.modelrouter.auto_router import _capability_ok
from oc8.modelrouter.speech import (
    SpeechNotConfigured,
    capability_of,
    split_for_speech,
    synthesize,
    transcribe,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_AUDIO = b"\x1aE\xdf\xa3fake-webm-bytes"


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


class _FakeAudioServer:
    """Stands in for `httpx.AsyncClient.send`: an OpenAI-compatible server's
    two audio endpoints. Remembers every request it was sent."""

    def __init__(
        self, *, transcript: dict[str, Any] | None = None, audio: bytes = b"ID3mp3-bytes"
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._transcript = transcript or {"text": "Hallo Welt"}
        self._audio = audio

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _FakeAudioServer:
        async def send(client: Any, request: httpx.Request, **_kwargs: Any) -> httpx.Response:
            await request.aread()
            self.requests.append(request)
            if request.url.path.endswith("/audio/transcriptions"):
                return httpx.Response(200, json=self._transcript, request=request)
            if request.url.path.endswith("/audio/speech"):
                return httpx.Response(
                    200,
                    content=self._audio,
                    headers={"content-type": "audio/mpeg"},
                    request=request,
                )
            return httpx.Response(404, json={"error": "no such endpoint"}, request=request)

        monkeypatch.setattr(httpx.AsyncClient, "send", send)
        return self


# ------------------------------------------------------------------ adapter


async def test_the_compatible_adapter_posts_the_audio_as_multipart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _FakeAudioServer(
        transcript={"text": "Hallo Welt", "usage": {"type": "duration", "seconds": 3.2}}
    ).install(monkeypatch)
    adapter = OpenAICompatibleAdapter("https://llm.example/v1/", "sk-test")

    result = await adapter.transcribe(
        _AUDIO, model="whisper-1", content_type="audio/webm", language="de"
    )

    assert result.text == "Hallo Welt"
    assert result.seconds == pytest.approx(3.2)
    assert result.usage is None
    [request] = server.requests
    assert str(request.url) == "https://llm.example/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert _AUDIO in body
    assert b'name="model"\r\n\r\nwhisper-1' in body
    assert b'name="language"\r\n\r\nde' in body
    # Never the client's own file name: a fixed one by content type.
    assert b'filename="audio.webm"' in body
    assert b"Content-Type: audio/webm" in body


async def test_reported_tokens_are_the_transcription_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAudioServer(
        transcript={
            "text": "ok",
            "usage": {"type": "tokens", "input_tokens": 40, "output_tokens": 3},
        }
    ).install(monkeypatch)
    adapter = OpenAICompatibleAdapter("https://llm.example/v1", "k")
    result = await adapter.transcribe(
        _AUDIO, model="gpt-4o-transcribe", content_type="audio/mp4", language=None
    )
    assert result.usage is not None
    assert (result.usage.tokens_in, result.usage.tokens_out) == (40, 3)


async def test_the_compatible_adapter_streams_speech_from_the_speech_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _FakeAudioServer(audio=b"ID3-the-answer").install(monkeypatch)
    adapter = OpenAICompatibleAdapter("https://llm.example/v1", "sk-test")

    chunks = await adapter.open_speech("Guten Tag", model="tts-1", extra={"voice": "nova"})
    audio = b"".join([c async for c in chunks])

    assert audio == b"ID3-the-answer"
    [request] = server.requests
    assert str(request.url) == "https://llm.example/v1/audio/speech"
    assert request.headers["authorization"] == "Bearer sk-test"
    import json

    payload = json.loads(request.content)
    assert payload["model"] == "tts-1"
    assert payload["input"] == "Guten Tag"
    assert payload["voice"] == "nova"
    # The route answers audio/mpeg, so an `extra` cannot change the format.
    assert payload["response_format"] == "mp3"


async def test_the_openai_adapter_uses_the_openai_audio_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _FakeAudioServer().install(monkeypatch)
    adapter = OpenAIAdapter("sk-openai")
    await adapter.transcribe(_AUDIO, model="whisper-1", content_type="audio/wav", language=None)
    chunks = await adapter.open_speech("Hi", model="tts-1", extra=None)
    _ = [c async for c in chunks]
    assert [str(r.url) for r in server.requests] == [
        "https://api.openai.com/v1/audio/transcriptions",
        "https://api.openai.com/v1/audio/speech",
    ]


async def test_an_error_from_the_speech_endpoint_raises_before_any_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def send(client: Any, request: httpx.Request, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    adapter = OpenAICompatibleAdapter("https://llm.example/v1", "bad")
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.open_speech("Hi", model="tts-1", extra=None)


# ------------------------------------------------------------- capability


async def test_a_model_without_a_capability_is_a_chat_model() -> None:
    assert capability_of(m.ModelConfig(params={})) == "chat"
    assert capability_of(m.ModelConfig(params={"capability": "speech_to_text"})) == (
        "speech_to_text"
    )


async def test_the_auto_router_never_routes_to_a_speech_model() -> None:
    for capability in ("speech_to_text", "text_to_speech"):
        cfg = m.ModelConfig(locality="cloud", params={"capability": capability})
        assert not _capability_ok(cfg, needs_vision=False, approx_tokens=10, require_local=False)
    chat = m.ModelConfig(locality="cloud", params={})
    assert _capability_ok(chat, needs_vision=False, approx_tokens=10, require_local=False)


async def test_long_text_is_split_into_pieces_the_endpoint_takes() -> None:
    text = ("Ein Satz mit Inhalt. " * 400).strip()
    pieces = split_for_speech(text, limit=1000)
    assert len(pieces) > 1
    assert all(len(p) <= 1000 for p in pieces)
    assert " ".join(pieces).split() == text.split()
    assert split_for_speech("kurz", limit=1000) == ["kurz"]


# ------------------------------------------------------------------ router


async def _tenant(
    db: Any,
    tenant: uuid.UUID,
    *,
    stt_locality: str = "cloud",
    tts_locality: str = "cloud",
    provider: str = "openai_compatible",
) -> tuple[m.ModelConfig, m.ModelConfig]:
    """A tenant with a compatible-provider key and one speech model of each kind,
    both picked in the tenant settings."""
    await create_credential(
        db,
        tenant_id=tenant,
        name="Tenant LLM",
        credential_type=f"{provider}_api_key",
        field_values={"api_key": "sk-tenant", "base_url": "https://tenant-llm.example/v1"},
    )
    stt = m.ModelConfig(
        tenant_id=tenant,
        provider=provider,
        model="whisper-1",
        locality=stt_locality,
        chat_enabled=False,
        params={"capability": "speech_to_text"},
    )
    tts = m.ModelConfig(
        tenant_id=tenant,
        provider=provider,
        model="tts-1",
        locality=tts_locality,
        chat_enabled=False,
        params={"capability": "text_to_speech"},
    )
    db.add_all([stt, tts])
    await db.flush()
    db.add(
        m.Organization(
            id=tenant,
            slug=f"t{tenant.hex[:6]}",
            name="T",
            tier="standard",
            region="eu",
            settings={
                "speech": {
                    "speech_to_text_model_id": str(stt.id),
                    "text_to_speech_model_id": str(tts.id),
                }
            },
        )
    )
    await db.flush()
    return stt, tts


async def _usage(db: Any, tenant: uuid.UUID) -> list[m.TokenUsageRecord]:
    return list(
        (await db.execute(select(m.TokenUsageRecord).where(m.TokenUsageRecord.tenant_id == tenant)))
        .scalars()
        .all()
    )


async def test_transcribe_uses_the_tenants_key_and_meters_the_seconds(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _FakeAudioServer(
        transcript={"text": "Bitte Termin verschieben", "duration": 4.2}
    ).install(monkeypatch)
    tenant, member = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        await _tenant(db, tenant)
        result = await transcribe(
            db,
            tenant_id=tenant,
            audio=_AUDIO,
            content_type="audio/webm",
            language=None,
            member_id=member,
        )
        assert result.text == "Bitte Termin verschieben"
        assert result.provider == "openai_compatible"
        assert result.model == "whisper-1"
        [request] = server.requests
        assert str(request.url) == "https://tenant-llm.example/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer sk-tenant"
        [row] = await _usage(db, tenant)
        assert row.member_id == member
        assert (row.provider, row.model) == ("openai_compatible", "whisper-1")
        # No tokens reported: the seconds, rounded up, are the platform units.
        assert (row.tokens_in, row.tokens_out, row.platform_units) == (0, 0, 5)


async def test_reported_tokens_are_metered_as_tokens(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeAudioServer(
        transcript={
            "text": "ok",
            "usage": {"type": "tokens", "input_tokens": 50, "output_tokens": 2},
        }
    ).install(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _tenant(db, tenant)
        await transcribe(
            db, tenant_id=tenant, audio=_AUDIO, content_type="audio/ogg", language=None
        )
        [row] = await _usage(db, tenant)
        assert (row.tokens_in, row.tokens_out, row.platform_units) == (50, 2, 0)


async def test_a_plugin_providers_speech_is_metered_under_its_own_name(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin's adapter is the OpenAI-compatible one; the usage row must
    still name the plugin provider, or the call lands outside its budget and
    price rows."""
    _FakeAudioServer(transcript={"text": "ok", "duration": 1.0}).install(monkeypatch)
    entry = registry.ProviderEntry(
        canonical="demo_ai",
        aliases=(),
        locality="cloud",
        factory=lambda _s, base_url, key: OpenAICompatibleAdapter(
            base_url or "https://demo.example/v1", key or ""
        ),
        available=lambda _s, key: bool(key),
    )

    async def resolve(_db: Any, *, tenant_id: uuid.UUID, name: str) -> Any:
        return entry if name == "demo_ai" else None

    monkeypatch.setattr(speech_mod, "resolve_provider", resolve)

    async def key(*_a: Any, **_k: Any) -> str:
        return "sk-demo"

    async def no_base(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(speech_mod, "resolve_model_key", key)
    monkeypatch.setattr(speech_mod, "resolve_model_base_url", no_base)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        stt = m.ModelConfig(
            tenant_id=tenant,
            provider="demo_ai",
            model="whisper",
            locality="cloud",
            chat_enabled=False,
            params={"capability": "speech_to_text"},
        )
        db.add(stt)
        await db.flush()
        db.add(
            m.Organization(
                id=tenant,
                slug=f"t{tenant.hex[:6]}",
                name="T",
                tier="standard",
                region="eu",
                settings={"speech": {"speech_to_text_model_id": str(stt.id)}},
            )
        )
        await db.flush()
        result = await transcribe(
            db, tenant_id=tenant, audio=_AUDIO, content_type="audio/webm", language=None
        )
        assert result.provider == "demo_ai"
        [row] = await _usage(db, tenant)
        assert row.provider == "demo_ai"


async def test_synthesize_streams_audio_and_meters_the_characters(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _FakeAudioServer(audio=b"ID3-audio").install(monkeypatch)
    tenant, member = uuid.uuid4(), uuid.uuid4()
    text = "Der Termin ist am Montag."
    async with app_session(tenant) as db:
        await _tenant(db, tenant)
        spoken = await synthesize(db, tenant_id=tenant, text=text, member_id=member)
        assert (spoken.provider, spoken.model, spoken.characters) == (
            "openai_compatible",
            "tts-1",
            len(text),
        )
        assert spoken.media_type == "audio/mpeg"
        audio = b"".join([c async for c in spoken.chunks])
        assert audio == b"ID3-audio"
        [request] = server.requests
        assert str(request.url) == "https://tenant-llm.example/v1/audio/speech"
        assert request.headers["authorization"] == "Bearer sk-tenant"
        [row] = await _usage(db, tenant)
        assert row.member_id == member
        assert (row.provider, row.model) == ("openai_compatible", "tts-1")
        assert (row.tokens_in, row.tokens_out, row.platform_units) == (0, 0, len(text))


async def test_a_long_answer_is_read_in_pieces_back_to_back(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _FakeAudioServer(audio=b"mp3").install(monkeypatch)
    tenant = uuid.uuid4()
    text = "Ein langer Satz der vorgelesen wird. " * 300
    async with app_session(tenant) as db:
        await _tenant(db, tenant)
        spoken = await synthesize(db, tenant_id=tenant, text=text)
        audio = b"".join([c async for c in spoken.chunks])
    assert len(server.requests) > 1
    assert audio == b"mp3" * len(server.requests)


async def test_a_restricted_text_never_reaches_a_cloud_speech_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _FakeAudioServer().install(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _tenant(db, tenant)
        with pytest.raises(ClassificationViolation):
            await synthesize(db, tenant_id=tenant, text="Gehalt: 5000", restricted=True)
        assert server.requests == []
        assert await _usage(db, tenant) == []


async def test_a_restricted_text_may_reach_a_local_speech_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _FakeAudioServer().install(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _tenant(db, tenant, tts_locality="local")
        spoken = await synthesize(db, tenant_id=tenant, text="Gehalt: 5000", restricted=True)
        _ = [c async for c in spoken.chunks]
    assert len(server.requests) == 1


async def test_no_configured_model_is_not_configured(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _FakeAudioServer().install(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant, slug=f"t{tenant.hex[:6]}", name="T", tier="standard", region="eu"
            )
        )
        await db.flush()
        with pytest.raises(SpeechNotConfigured):
            await transcribe(
                db, tenant_id=tenant, audio=_AUDIO, content_type="audio/webm", language=None
            )
        with pytest.raises(SpeechNotConfigured):
            await synthesize(db, tenant_id=tenant, text="Hi")
    assert server.requests == []


async def test_a_setting_pointing_at_a_chat_model_is_not_a_speech_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capability is read live: a model switched back to chat stops
    serving speech even while the setting still names it."""
    _FakeAudioServer().install(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        stt, _tts = await _tenant(db, tenant)
        stt.params = {}
        await db.flush()
        with pytest.raises(SpeechNotConfigured):
            await transcribe(
                db, tenant_id=tenant, audio=_AUDIO, content_type="audio/webm", language=None
            )
