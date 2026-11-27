"""Voice in the chat: speech models in the catalog, the tenant's pick, and the
three chat-audio routes.

A speech model is never a chat model: it is not offered in the chat picker,
cannot be the Copilot's model, an agent's model or an auto tier, and the auto
router never picks it. Dictation needs a speech-to-text model; reading an
answer aloud needs a text-to-speech model, and a local one when the answer is
restricted. Both are the chat owner's alone, metered for the person, and
audited without their words.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from oc8 import models as m
from oc8.credentials.service import create_credential
from tests.api.test_personal_agent_content import _headers, _http, _subject_uuid, _world
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_AUDIO = b"\x1aE\xdf\xa3" + b"webm" * 100
_SECRET_WORDS = "Bitte den Vertrag kuendigen"


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """A fake OpenAI-compatible server behind every outgoing httpx call. The
    ASGI test client sends through its transport, not `AsyncClient.send` of a
    real network client, so only calls whose URL is the provider's land here."""
    seen: list[httpx.Request] = []
    real_send = httpx.AsyncClient.send

    async def send(client: Any, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if request.url.host != "speech.example":
            return await real_send(client, request, **kwargs)
        await request.aread()
        seen.append(request)
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(
                200, json={"text": _SECRET_WORDS, "duration": 2.5}, request=request
            )
        return httpx.Response(
            200, content=b"ID3-audio", headers={"content-type": "audio/mpeg"}, request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return seen


async def _organization(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant, slug=f"t{tenant.hex[:6]}", name="T", tier="standard", region="eu"
            )
        )
        await create_credential(
            db,
            tenant_id=tenant,
            name="Speech",
            credential_type="openai_compatible_api_key",
            field_values={"api_key": "sk-speech", "base_url": "https://speech.example/v1"},
        )
        await db.flush()


def _admin(tenant: uuid.UUID) -> dict[str, str]:
    return _headers(tenant, "admin", "org_admin")


async def _model(
    http: AsyncClient,
    tenant: uuid.UUID,
    *,
    capability: str | None,
    locality: str = "cloud",
    provider: str = "openai_compatible",
    model: str = "m",
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"provider": provider, "model": model, "locality": locality, **extra}
    if capability is not None:
        body["capability"] = capability
    r = await http.post("/api/v1/models", json=body, headers=_admin(tenant))
    assert r.status_code == 201, r.text
    return r.json()


async def _speech_world(
    app_session: AppSessionFactory, http: AsyncClient, *, tts_locality: str = "cloud"
) -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """`_world` plus an organization, a chat model, one speech model of each
    kind, both picked for the tenant."""
    w = await _world(app_session)
    await _organization(app_session, w.tenant)
    chat = await _model(http, w.tenant, capability=None, model="gpt-chat")
    stt = await _model(http, w.tenant, capability="speech_to_text", model="whisper-1")
    tts = await _model(
        http, w.tenant, capability="text_to_speech", model="tts-1", locality=tts_locality
    )
    r = await http.put(
        "/api/v1/settings/speech",
        json={"speechToTextModelId": stt["id"], "textToSpeechModelId": tts["id"]},
        headers=_admin(w.tenant),
    )
    assert r.status_code == 200, r.text
    return w, chat, stt, tts


async def _chat(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    agent: uuid.UUID,
    subject: str,
    *,
    restricted: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        member_id = (
            await db.execute(
                select(m.OrgMember.id).where(m.OrgMember.subject_uuid == _subject_uuid(subject))
            )
        ).scalar_one()
        session = m.ChatSession(tenant_id=tenant, agent_id=agent, member_id=member_id)
        db.add(session)
        await db.flush()
        answer = m.ChatMessage(
            tenant_id=tenant,
            session_id=session.id,
            role="assistant",
            content="Der Termin ist am Montag.",
            restricted=restricted,
        )
        db.add(answer)
        await db.flush()
        return session.id, answer.id


def _upload(
    audio: bytes = _AUDIO, content_type: str = "audio/webm", **fields: str
) -> dict[str, Any]:
    return {
        "files": {"file": ("../../etc/evil name.webm", audio, content_type)},
        "data": fields,
    }


# ----------------------------------------------------------------- catalog


async def test_the_capability_round_trips_through_the_catalog(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    await _organization(app_session, w.tenant)
    async with _http() as http:
        chat = await _model(http, w.tenant, capability=None, model="gpt-chat")
        stt = await _model(http, w.tenant, capability="speech_to_text", model="whisper-1")
        assert chat["capability"] == "chat"
        assert stt["capability"] == "speech_to_text"
        # A speech model is never offered to a chat.
        assert stt["chatEnabled"] is False
        assert stt["usedByCopilot"] is False
        listed = {
            row["id"]: row
            for row in (await http.get("/api/v1/models", headers=_admin(w.tenant))).json()
        }
        assert listed[stt["id"]]["capability"] == "speech_to_text"
        # Back to chat on update; it stays off for chat until an admin turns it on.
        r = await http.patch(
            f"/api/v1/models/{stt['id']}",
            json={"provider": "openai_compatible", "model": "whisper-1", "capability": "chat"},
            headers=_admin(w.tenant),
        )
        assert r.status_code == 200, r.text
        assert r.json()["capability"] == "chat"
        assert r.json()["chatEnabled"] is False
        r = await http.patch(
            f"/api/v1/models/{stt['id']}",
            json={
                "provider": "openai_compatible",
                "model": "whisper-1",
                "capability": "text_to_speech",
            },
            headers=_admin(w.tenant),
        )
        assert r.json()["capability"] == "text_to_speech"
        assert r.json()["chatEnabled"] is False


async def test_the_only_model_of_a_tenant_is_not_the_copilot_model_when_it_is_speech(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    await _organization(app_session, w.tenant)
    async with _http() as http:
        stt = await _model(http, w.tenant, capability="speech_to_text")
    assert stt["usedByCopilot"] is False


@pytest.mark.parametrize("provider", ["anthropic", "ollama"])
async def test_only_openai_style_providers_can_be_speech_models(
    app_session: AppSessionFactory, provider: str
) -> None:
    w = await _world(app_session)
    await _organization(app_session, w.tenant)
    async with _http() as http:
        r = await http.post(
            "/api/v1/models",
            json={"provider": provider, "model": "x", "capability": "speech_to_text"},
            headers=_admin(w.tenant),
        )
    assert r.status_code == 422, r.text
    assert "speech" in r.json()["detail"]


async def test_an_unknown_capability_is_refused(app_session: AppSessionFactory) -> None:
    w = await _world(app_session)
    await _organization(app_session, w.tenant)
    async with _http() as http:
        r = await http.post(
            "/api/v1/models",
            json={"provider": "openai_compatible", "model": "x", "capability": "images"},
            headers=_admin(w.tenant),
        )
    assert r.status_code == 422


async def test_a_speech_model_cannot_be_a_chat_or_copilot_or_agent_model(
    app_session: AppSessionFactory,
) -> None:
    async with _http() as http:
        w, chat, stt, _tts = await _speech_world(app_session, http)
        h = _admin(w.tenant)
        base = {"provider": "openai_compatible", "model": "whisper-1"}
        for change in ({"usedByCopilot": True}, {"chatEnabled": True}):
            r = await http.patch(f"/api/v1/models/{stt['id']}", json={**base, **change}, headers=h)
            assert r.status_code == 422, (change, r.text)
        r = await http.post(
            "/api/v1/models",
            json={**base, "capability": "speech_to_text", "usedByCopilot": True},
            headers=h,
        )
        assert r.status_code == 422, r.text
        # The Copilot's model does not become a speech model.
        r = await http.patch(
            f"/api/v1/models/{chat['id']}",
            json={
                "provider": "openai_compatible",
                "model": "gpt-chat",
                "capability": "speech_to_text",
            },
            headers=h,
        )
        assert r.status_code == 422, r.text
        # Not an auto tier.
        r = await http.post(
            "/api/v1/models",
            json={"provider": "auto", "model": "auto", "autoTiers": {"fast": stt["id"]}},
            headers=h,
        )
        assert r.status_code == 422, r.text
        # Not an agent's model.
        r = await http.patch(
            f"/api/v1/agents/{w.standard_agent}/model-config",
            json={"modelConfigId": stt["id"]},
            headers=h,
        )
        assert r.status_code == 422, r.text

        session_id, _ = await _chat(app_session, w.tenant, w.standard_agent, "admin")
        options = await http.get(f"/api/v1/chat/sessions/{session_id}/models", headers=h)
        assert options.status_code == 200, options.text
        ids = {o["id"] for o in options.json()}
        assert chat["id"] in ids
        assert stt["id"] not in ids and _tts["id"] not in ids
        r = await http.put(
            f"/api/v1/chat/sessions/{session_id}/model",
            json={"modelConfigId": stt["id"]},
            headers=h,
        )
        assert r.status_code == 422, r.text


async def test_a_speech_model_flipped_on_for_chat_in_the_database_is_still_not_offered(
    app_session: AppSessionFactory,
) -> None:
    """The picker reads the capability itself, not only the flag the catalog
    keeps off."""
    async with _http() as http:
        w, _chat_model, stt, _tts = await _speech_world(app_session, http)
        async with app_session(w.tenant) as db:
            row = await db.get(m.ModelConfig, uuid.UUID(stt["id"]))
            assert row is not None
            row.chat_enabled = True
            await db.flush()
        session_id, _ = await _chat(app_session, w.tenant, w.standard_agent, "admin")
        options = await http.get(
            f"/api/v1/chat/sessions/{session_id}/models", headers=_admin(w.tenant)
        )
    assert stt["id"] not in {o["id"] for o in options.json()}


async def test_the_copilot_never_falls_back_to_a_speech_model(
    app_session: AppSessionFactory,
) -> None:
    from oc8.agent.assistant import _select_model_config

    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        speech = m.ModelConfig(
            tenant_id=w.tenant,
            provider="openai_compatible",
            model="whisper-1",
            chat_enabled=False,
            params={"capability": "speech_to_text"},
        )
        db.add(speech)
        await db.flush()
        assert await _select_model_config(db, tenant_id=w.tenant) is None
        chat = m.ModelConfig(tenant_id=w.tenant, provider="openai_compatible", model="gpt")
        db.add(chat)
        await db.flush()
        assert await _select_model_config(db, tenant_id=w.tenant) == chat.id


# ---------------------------------------------------------------- settings


async def test_the_speech_settings_take_only_matching_speech_models(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    await _organization(app_session, w.tenant)
    async with _http() as http:
        h = _admin(w.tenant)
        r = await http.get("/api/v1/settings/speech", headers=h)
        assert r.status_code == 200, r.text
        assert r.json() == {
            "speechToTextModelId": None,
            "textToSpeechModelId": None,
            "dailyTranscriptionMinutes": 60,
            "dailyReadAloudCharacters": 200_000,
        }
        chat = await _model(http, w.tenant, capability=None)
        stt = await _model(http, w.tenant, capability="speech_to_text")
        for bad in (
            {"speechToTextModelId": chat["id"], "textToSpeechModelId": None},
            {"speechToTextModelId": None, "textToSpeechModelId": stt["id"]},
            {"speechToTextModelId": str(uuid.uuid4()), "textToSpeechModelId": None},
        ):
            r = await http.put("/api/v1/settings/speech", json=bad, headers=h)
            assert r.status_code == 422, (bad, r.text)
        good = {
            "speechToTextModelId": stt["id"],
            "textToSpeechModelId": None,
            "dailyTranscriptionMinutes": 30,
            "dailyReadAloudCharacters": 5000,
        }
        member = _headers(w.tenant, "owner", "member")
        assert (
            await http.put("/api/v1/settings/speech", json=good, headers=member)
        ).status_code == 403
        r = await http.put("/api/v1/settings/speech", json=good, headers=h)
        assert r.status_code == 200 and r.json() == good
        assert (await http.get("/api/v1/settings/speech", headers=h)).json() == good


async def test_capabilities_say_which_voice_buttons_to_show(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    await _organization(app_session, w.tenant)
    member = _headers(w.tenant, "owner", "member")
    async with _http() as http:
        r = await http.get("/api/v1/chat/audio/capabilities", headers=member)
        assert r.status_code == 200, r.text
        assert r.json() == {
            "speechToText": False,
            "textToSpeech": False,
            "textToSpeechLocal": False,
        }
        tts = await _model(http, w.tenant, capability="text_to_speech", locality="local")
        await http.put(
            "/api/v1/settings/speech",
            json={"speechToTextModelId": None, "textToSpeechModelId": tts["id"]},
            headers=_admin(w.tenant),
        )
        r = await http.get("/api/v1/chat/audio/capabilities", headers=member)
    assert r.json() == {"speechToText": False, "textToSpeech": True, "textToSpeechLocal": True}


# -------------------------------------------------------------- transcribe


async def test_dictation_returns_the_text_and_meters_and_audits_without_it(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, _chat_model, _stt, _tts = await _speech_world(app_session, http)
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        r = await http.post(
            "/api/v1/chat/audio/transcribe",
            headers=_headers(w.tenant, "owner", "member"),
            **_upload(session_id=str(session_id)),
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"text": _SECRET_WORDS}
    [request] = provider
    assert str(request.url) == "https://speech.example/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer sk-speech"
    assert b"evil" not in request.content and b"etc" not in request.content
    async with app_session(w.tenant) as db:
        owner = (
            await db.execute(
                select(m.OrgMember.id).where(m.OrgMember.subject_uuid == _subject_uuid("owner"))
            )
        ).scalar_one()
        rows = (
            (
                await db.execute(
                    select(m.TokenUsageRecord).where(m.TokenUsageRecord.tenant_id == w.tenant)
                )
            )
            .scalars()
            .all()
        )
        # The reservation (the upload at 16 kbit/s) plus the reported seconds
        # beyond it: 2.5 s rounded up.
        assert sum(r.platform_units for r in rows) == 3
        assert {(r.member_id, r.agent_id) for r in rows} == {(owner, w.personal_agent)}
        assert {(r.provider, r.model) for r in rows} == {("openai_compatible", "whisper-1")}
        [event] = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "chat.audio.transcribed")
                )
            )
            .scalars()
            .all()
        )
    assert event.resource["agent_id"] == str(w.personal_agent)
    assert event.resource["chat_session_id"] == str(session_id)
    assert event.resource["model"] == "whisper-1"
    assert event.resource["seconds"] == pytest.approx(2.5)
    assert _SECRET_WORDS not in str(event.resource)


async def test_dictation_needs_the_persons_chat(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        r = await http.post(
            "/api/v1/chat/audio/transcribe",
            headers=_headers(w.tenant, "owner", "member"),
            **_upload(content_type="audio/webm;codecs=opus"),
        )
    # Usage and audit always name the chat's agent, so there is no dictation
    # outside a chat.
    assert r.status_code == 422, r.text
    assert provider == []


async def test_dictation_into_someone_elses_chat_is_not_found(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, _ = await _chat(app_session, w.tenant, w.standard_agent, "admin")
        r = await http.post(
            "/api/v1/chat/audio/transcribe",
            headers=_headers(w.tenant, "manager", "member"),
            **_upload(session_id=str(session_id)),
        )
    assert r.status_code == 404, r.text
    assert provider == []


async def test_dictation_refuses_what_is_not_audio(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        r = await http.post(
            "/api/v1/chat/audio/transcribe",
            headers=_headers(w.tenant, "owner", "member"),
            **_upload(content_type="text/html", session_id=str(session_id)),
        )
    assert r.status_code == 415, r.text
    assert provider == []


async def test_dictation_over_12_mb_is_too_large(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        r = await http.post(
            "/api/v1/chat/audio/transcribe",
            headers=_headers(w.tenant, "owner", "member"),
            **_upload(audio=b"\0" * (12 * 1024 * 1024 + 1), session_id=str(session_id)),
        )
    assert r.status_code == 413, r.text
    assert provider == []


async def test_without_a_speech_model_dictation_and_speech_are_not_found(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    w = await _world(app_session)
    await _organization(app_session, w.tenant)
    session_id, message_id = await _chat(app_session, w.tenant, w.personal_agent, "owner")
    h = _headers(w.tenant, "owner", "member")
    async with _http() as http:
        r = await http.post(
            "/api/v1/chat/audio/transcribe",
            headers=h,
            **_upload(session_id=str(session_id)),
        )
        assert r.status_code == 404, r.text
        r = await http.post(
            f"/api/v1/chat/sessions/{session_id}/messages/{message_id}/speech", headers=h
        )
        assert r.status_code == 404, r.text
    assert provider == []


# ------------------------------------------------------------------ speech


async def test_an_answer_is_read_aloud_as_an_audio_stream(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, message_id = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        r = await http.post(
            f"/api/v1/chat/sessions/{session_id}/messages/{message_id}/speech",
            headers=_headers(w.tenant, "owner", "member"),
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "audio/mpeg"
        assert r.content == b"ID3-audio"
    [request] = provider
    assert str(request.url) == "https://speech.example/v1/audio/speech"
    async with app_session(w.tenant) as db:
        [usage] = (await db.execute(select(m.TokenUsageRecord))).scalars().all()
        assert usage.platform_units == len("Der Termin ist am Montag.")
        assert usage.model == "tts-1"
        [event] = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "chat.audio.spoken")
                )
            )
            .scalars()
            .all()
        )
    assert event.resource["agent_id"] == str(w.personal_agent)
    assert event.resource["message_id"] == str(message_id)
    assert event.resource["characters"] == len("Der Termin ist am Montag.")
    assert event.resource["model"] == "tts-1"
    assert "Montag" not in str(event.resource)


async def test_reading_someone_elses_answer_is_not_found(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, message_id = await _chat(app_session, w.tenant, w.standard_agent, "admin")
        r = await http.post(
            f"/api/v1/chat/sessions/{session_id}/messages/{message_id}/speech",
            headers=_headers(w.tenant, "manager", "member"),
        )
    assert r.status_code == 404, r.text
    assert provider == []


async def test_a_restricted_answer_is_not_read_by_a_cloud_model(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, message_id = await _chat(
            app_session, w.tenant, w.personal_agent, "owner", restricted=True
        )
        r = await http.post(
            f"/api/v1/chat/sessions/{session_id}/messages/{message_id}/speech",
            headers=_headers(w.tenant, "owner", "member"),
        )
    assert r.status_code == 409, r.text
    assert "local" in r.json()["detail"]
    assert provider == []


async def test_a_restricted_answer_is_read_by_a_local_model(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http, tts_locality="local")
        session_id, message_id = await _chat(
            app_session, w.tenant, w.personal_agent, "owner", restricted=True
        )
        r = await http.post(
            f"/api/v1/chat/sessions/{session_id}/messages/{message_id}/speech",
            headers=_headers(w.tenant, "owner", "member"),
        )
    assert r.status_code == 200, r.text
    assert len(provider) == 1


# ------------------------------------------- a speech model is never an agent's


async def _department_of(
    app_session: AppSessionFactory, tenant: uuid.UUID, agent: uuid.UUID
) -> str:
    async with app_session(tenant) as db:
        row = await db.get(m.Agent, agent)
        assert row is not None
        return str(row.department_id)


async def test_a_new_agent_cannot_run_on_a_speech_model(
    app_session: AppSessionFactory,
) -> None:
    async with _http() as http:
        w, _chat_model, stt, _tts = await _speech_world(app_session, http)
        dept = await _department_of(app_session, w.tenant, w.standard_agent)
        r = await http.post(
            "/api/v1/agents",
            json={"departmentId": dept, "name": "Sprachlos", "modelConfigId": stt["id"]},
            headers=_admin(w.tenant),
        )
    assert r.status_code == 422, r.text
    assert "speech" in r.text


async def test_a_model_a_version_or_an_auto_tier_runs_on_stays_a_chat_model(
    app_session: AppSessionFactory,
) -> None:
    """Converting checks every agent version that pins the model and every
    auto tier that points at it, not only the agents' live rows."""
    async with _http() as http:
        w, chat, _stt, _tts = await _speech_world(app_session, http)
        h = _admin(w.tenant)
        other = await _model(http, w.tenant, capability=None, model="gpt-other")
        dept = await _department_of(app_session, w.tenant, w.standard_agent)
        hired = await http.post(
            "/api/v1/agents",
            json={"departmentId": dept, "name": "Vera", "modelConfigId": other["id"]},
            headers=h,
        )
        assert hired.status_code == 201, hired.text
        agent_id = hired.json()["id"]
        switched = await http.patch(
            f"/api/v1/agents/{agent_id}/model-config",
            json={"modelConfigId": chat["id"]},
            headers=h,
        )
        assert switched.status_code == 200, switched.text
        assert hired.json()["currentVersionNo"] == 1
        published = await http.post(
            f"/api/v1/agents/{agent_id}/versions",
            json={"expectedCurrentVersionNo": 1},
            headers=h,
        )
        assert published.status_code == 201, published.text
        # No live agent runs on `other` now; v1 still pins it.
        r = await http.patch(
            f"/api/v1/models/{other['id']}",
            json={
                "provider": "openai_compatible",
                "model": "gpt-other",
                "capability": "speech_to_text",
            },
            headers=h,
        )
        assert r.status_code == 422, r.text
        assert "version" in r.json()["detail"]

        tiered = await _model(http, w.tenant, capability=None, model="gpt-tier")
        auto = await http.post(
            "/api/v1/models",
            json={"provider": "auto", "model": "auto", "autoTiers": {"fast": tiered["id"]}},
            headers=h,
        )
        assert auto.status_code == 201, auto.text
        r = await http.patch(
            f"/api/v1/models/{tiered['id']}",
            json={
                "provider": "openai_compatible",
                "model": "gpt-tier",
                "capability": "text_to_speech",
            },
            headers=h,
        )
        assert r.status_code == 422, r.text
        assert "auto" in r.json()["detail"]


async def test_the_llm_gateway_lists_no_speech_model(app_session: AppSessionFactory) -> None:
    from tests.api.test_llm_gateway import _agent_run, _agent_token

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _dept = await _agent_run(db, tenant, model="gpt-chat")
        db.add(
            m.ModelConfig(
                tenant_id=tenant,
                provider="openai_compatible",
                model="whisper-1",
                chat_enabled=False,
                params={"capability": "speech_to_text"},
            )
        )
        await db.flush()
    token = _agent_token(tenant, agent_id, run_id)
    async with _http() as http:
        r = await http.get("/llm/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    names = {row["id"] for row in r.json()["data"]}
    assert "gpt-chat" in names and "whisper-1" not in names


async def test_a_speech_model_forced_on_for_chat_cannot_be_picked_for_a_session(
    app_session: AppSessionFactory,
) -> None:
    async with _http() as http:
        w, _chat_model, stt, _tts = await _speech_world(app_session, http)
        async with app_session(w.tenant) as db:
            row = await db.get(m.ModelConfig, uuid.UUID(stt["id"]))
            assert row is not None
            row.chat_enabled = True
            await db.flush()
        session_id, _ = await _chat(app_session, w.tenant, w.standard_agent, "admin")
        r = await http.put(
            f"/api/v1/chat/sessions/{session_id}/model",
            json={"modelConfigId": stt["id"]},
            headers=_admin(w.tenant),
        )
    assert r.status_code == 422, r.text


# ------------------------------------------------------ allowance and input


async def test_the_daily_allowance_is_set_and_bounded_in_the_speech_settings(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, _chat_model, stt, tts = await _speech_world(app_session, http)
        h = _admin(w.tenant)
        base = {"speechToTextModelId": stt["id"], "textToSpeechModelId": tts["id"]}
        for bad in (
            {"dailyTranscriptionMinutes": -1},
            {"dailyTranscriptionMinutes": 24 * 60 + 1},
            {"dailyReadAloudCharacters": -5},
            {"dailyReadAloudCharacters": 10_000_001},
        ):
            r = await http.put("/api/v1/settings/speech", json={**base, **bad}, headers=h)
            assert r.status_code == 422, (bad, r.text)
        r = await http.put(
            "/api/v1/settings/speech",
            json={**base, "dailyTranscriptionMinutes": 0, "dailyReadAloudCharacters": 0},
            headers=h,
        )
        assert r.status_code == 200, r.text
        member = _headers(w.tenant, "owner", "member")
        caps = await http.get("/api/v1/chat/audio/capabilities", headers=member)
        assert caps.json() == {
            "speechToText": False,
            "textToSpeech": False,
            "textToSpeechLocal": False,
        }
        session_id, message_id = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        r = await http.post(
            "/api/v1/chat/audio/transcribe",
            headers=member,
            **_upload(session_id=str(session_id)),
        )
        assert r.status_code == 429, r.text
        assert "switched off" in r.json()["detail"]
        r = await http.post(
            f"/api/v1/chat/sessions/{session_id}/messages/{message_id}/speech", headers=member
        )
        assert r.status_code == 429, r.text
    assert provider == []


async def test_dictation_past_the_daily_minutes_is_refused(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, _chat_model, stt, tts = await _speech_world(app_session, http)
        r = await http.put(
            "/api/v1/settings/speech",
            json={
                "speechToTextModelId": stt["id"],
                "textToSpeechModelId": tts["id"],
                "dailyTranscriptionMinutes": 1,
            },
            headers=_admin(w.tenant),
        )
        assert r.status_code == 200, r.text
        async with app_session(w.tenant) as db:
            from oc8.metering.usage import record_usage

            owner = (
                await db.execute(
                    select(m.OrgMember.id).where(m.OrgMember.subject_uuid == _subject_uuid("owner"))
                )
            ).scalar_one()
            await record_usage(
                db,
                tenant_id=w.tenant,
                request_id=uuid.uuid4(),
                model="whisper-1",
                provider="openai_compatible",
                tokens_in=0,
                tokens_out=0,
                platform_units=60,
                member_id=owner,
            )
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        r = await http.post(
            "/api/v1/chat/audio/transcribe",
            headers=_headers(w.tenant, "owner", "member"),
            **_upload(session_id=str(session_id)),
        )
    assert r.status_code == 429, r.text
    assert "used up" in r.json()["detail"]
    assert provider == []


async def test_a_bad_language_is_refused(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        for language in ("german", "DE", "d1", "de-DE; drop"):
            r = await http.post(
                "/api/v1/chat/audio/transcribe",
                headers=_headers(w.tenant, "owner", "member"),
                **_upload(session_id=str(session_id), language=language),
            )
            assert r.status_code == 422, (language, r.text)
    assert provider == []


async def test_a_chunked_upload_over_12_mb_is_too_large(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    """No Content-Length to refuse up front: the bytes are counted as read."""
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        boundary = "oc8boundary"
        head = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="session_id"\r\n\r\n'
            f"{session_id}\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="a.webm"\r\n'
            "Content-Type: audio/webm\r\n\r\n"
        ).encode()

        async def body() -> Any:
            yield head
            chunk = b"\0" * (1024 * 1024)
            for _ in range(13):
                yield chunk
            yield f"\r\n--{boundary}--\r\n".encode()

        r = await http.post(
            "/api/v1/chat/audio/transcribe",
            headers={
                **_headers(w.tenant, "owner", "member"),
                "content-type": f"multipart/form-data; boundary={boundary}",
            },
            content=body(),
        )
    assert r.status_code == 413, r.text
    assert provider == []


async def test_another_tenants_speech_model_is_not_a_choice(
    app_session: AppSessionFactory,
) -> None:
    async with _http() as http:
        theirs, _c, their_stt, _t = await _speech_world(app_session, http)
        ours, *_ = await _speech_world(app_session, http)
        r = await http.put(
            "/api/v1/settings/speech",
            json={"speechToTextModelId": their_stt["id"], "textToSpeechModelId": None},
            headers=_admin(ours.tenant),
        )
    assert theirs.tenant != ours.tenant
    assert r.status_code == 422, r.text


async def test_the_provider_failing_oddly_is_a_bad_gateway_without_details(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_send = httpx.AsyncClient.send
    mode = {"kind": "invalid-url"}

    async def send(client: Any, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if request.url.host != "speech.example":
            return await real_send(client, request, **kwargs)
        if mode["kind"] == "invalid-url":
            raise httpx.InvalidURL("sk-speech leaked in a url")
        return httpx.Response(200, content=b"<html>not json", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        h = _headers(w.tenant, "owner", "member")
        r = await http.post(
            "/api/v1/chat/audio/transcribe", headers=h, **_upload(session_id=str(session_id))
        )
        assert r.status_code == 502, r.text
        assert "InvalidURL" in r.json()["detail"] and "sk-speech" not in r.text
        mode["kind"] = "not-json"
        r = await http.post(
            "/api/v1/chat/audio/transcribe", headers=h, **_upload(session_id=str(session_id))
        )
    assert r.status_code == 502, r.text
    assert "unexpectedly" in r.json()["detail"]


async def _answer(
    app_session: AppSessionFactory, tenant: uuid.UUID, session_id: uuid.UUID, content: str
) -> uuid.UUID:
    async with app_session(tenant) as db:
        msg = m.ChatMessage(
            tenant_id=tenant, session_id=session_id, role="assistant", content=content
        )
        db.add(msg)
        await db.flush()
        return msg.id


async def test_a_very_long_answer_is_not_read_aloud(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        long_id = await _answer(app_session, w.tenant, session_id, "Wort " * 4001)
        r = await http.post(
            f"/api/v1/chat/sessions/{session_id}/messages/{long_id}/speech",
            headers=_headers(w.tenant, "owner", "member"),
        )
    assert r.status_code == 409, r.text
    assert "20000" in r.json()["detail"]
    assert provider == []


async def test_markdown_is_stripped_before_it_is_read_aloud(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        md = "## Antwort\n\n**Ja**, siehe [Doku](https://x.example).\n\n```sh\nrm -rf /\n```\n"
        msg_id = await _answer(app_session, w.tenant, session_id, md)
        r = await http.post(
            f"/api/v1/chat/sessions/{session_id}/messages/{msg_id}/speech",
            headers=_headers(w.tenant, "owner", "member"),
        )
        assert r.status_code == 200, r.text
    [request] = provider
    import json as _json

    spoken = _json.loads(request.content)["input"]
    assert "rm -rf" not in spoken and "**" not in spoken and "https://" not in spoken
    assert "Antwort" in spoken and "Ja" in spoken and "Doku" in spoken


async def test_a_later_piece_failing_ends_the_audio_and_is_audited(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_send = httpx.AsyncClient.send
    sent: list[httpx.Request] = []

    async def send(client: Any, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if request.url.host != "speech.example":
            return await real_send(client, request, **kwargs)
        await request.aread()
        sent.append(request)
        if len(sent) > 1:
            return httpx.Response(500, json={"error": "down"}, request=request)
        return httpx.Response(200, content=b"ID3-first", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        msg_id = await _answer(
            app_session, w.tenant, session_id, "Ein langer Satz der vorgelesen wird. " * 200
        )
        r = await http.post(
            f"/api/v1/chat/sessions/{session_id}/messages/{msg_id}/speech",
            headers=_headers(w.tenant, "owner", "member"),
        )
        assert r.status_code == 200, r.text
        assert r.content == b"ID3-first"
    async with app_session(w.tenant) as db:
        [event] = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "chat.audio.speech_failed")
                )
            )
            .scalars()
            .all()
        )
    assert event.resource["message_id"] == str(msg_id)
    assert event.resource["error"] == "HTTPStatusError"
    assert event.decision == "error"


async def _agent_on_v1_then_moved(
    app_session: AppSessionFactory, http: AsyncClient, w: Any, first: str, then: str
) -> str:
    """An agent hired on `first` (v1), moved to `then` and published (v2)."""
    h = _admin(w.tenant)
    dept = await _department_of(app_session, w.tenant, w.standard_agent)
    hired = await http.post(
        "/api/v1/agents",
        json={"departmentId": dept, "name": "Vera", "modelConfigId": first},
        headers=h,
    )
    assert hired.status_code == 201, hired.text
    agent_id = str(hired.json()["id"])
    switched = await http.patch(
        f"/api/v1/agents/{agent_id}/model-config", json={"modelConfigId": then}, headers=h
    )
    assert switched.status_code == 200, switched.text
    published = await http.post(
        f"/api/v1/agents/{agent_id}/versions", json={"expectedCurrentVersionNo": 1}, headers=h
    )
    assert published.status_code == 201, published.text
    return agent_id


async def test_a_rollback_to_a_version_whose_model_became_speech_is_refused(
    app_session: AppSessionFactory,
) -> None:
    """The model was turned into a speech model past the catalog (direct
    SQL): restoring the version that pins it would make it the agent's."""
    async with _http() as http:
        w, chat, _stt, _tts = await _speech_world(app_session, http)
        other = await _model(http, w.tenant, capability=None, model="gpt-other")
        agent_id = await _agent_on_v1_then_moved(app_session, http, w, other["id"], chat["id"])
        async with app_session(w.tenant) as db:
            row = await db.get(m.ModelConfig, uuid.UUID(other["id"]))
            assert row is not None
            row.params = {"capability": "speech_to_text"}
            row.chat_enabled = False
            await db.flush()
        r = await http.post(
            f"/api/v1/agents/{agent_id}/versions/1/rollback", headers=_admin(w.tenant)
        )
        assert r.status_code == 422, r.text
        assert "speech" in r.text
        async with app_session(w.tenant) as db:
            agent = await db.get(m.Agent, uuid.UUID(agent_id))
            assert agent is not None
            assert str(agent.model_config_id) == chat["id"]


async def test_the_gateway_refuses_an_agent_pinned_to_a_speech_model(
    app_session: AppSessionFactory,
) -> None:
    from tests.api.test_llm_gateway import _agent_run, _agent_token

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _dept = await _agent_run(db, tenant, model="whisper-1")
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        cfg = await db.get(m.ModelConfig, agent.model_config_id)
        assert cfg is not None
        cfg.params = {"capability": "speech_to_text"}
        cfg.chat_enabled = False
        await db.flush()
    token = _agent_token(tenant, agent_id, run_id)
    body = {"model": "whisper-1", "messages": [{"role": "user", "content": "hi"}]}
    async with _http() as http:
        for stream in (False, True):
            r = await http.post(
                "/llm/v1/chat/completions",
                json={**body, "stream": stream},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 409, (stream, r.text)
            assert "speech model" in r.json()["detail"]


async def test_parallel_dictations_are_reserved_not_raced(
    app_session: AppSessionFactory, provider: list[httpx.Request]
) -> None:
    """Five recordings at once against a day that fits two: two are
    transcribed, three refused, and the day is not overshot."""
    import asyncio

    async with _http() as http:
        w, _chat_model, stt, tts = await _speech_world(app_session, http)
        r = await http.put(
            "/api/v1/settings/speech",
            json={
                "speechToTextModelId": stt["id"],
                "textToSpeechModelId": tts["id"],
                "dailyTranscriptionMinutes": 1,
            },
            headers=_admin(w.tenant),
        )
        assert r.status_code == 200, r.text
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        h = _headers(w.tenant, "owner", "member")
        # 30 seconds each at 16 kbit/s.
        audio = b"\0" * (2_000 * 30)
        results = await asyncio.gather(
            *(
                http.post(
                    "/api/v1/chat/audio/transcribe",
                    headers=h,
                    **_upload(audio=audio, session_id=str(session_id)),
                )
                for _ in range(5)
            )
        )
    assert sorted(r.status_code for r in results) == [200, 200, 429, 429, 429]
    assert len(provider) == 2
    async with app_session(w.tenant) as db:
        units = await db.scalar(
            select(func.sum(m.TokenUsageRecord.platform_units)).where(
                m.TokenUsageRecord.tenant_id == w.tenant
            )
        )
    assert units is not None and units <= 60


async def test_a_bug_in_a_speech_call_is_not_passed_off_as_the_providers(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.modelrouter.adapters.openai_compatible import OpenAICompatibleAdapter

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise KeyError("a programming error")

    monkeypatch.setattr(OpenAICompatibleAdapter, "transcribe", broken)
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        with pytest.raises(KeyError):
            await http.post(
                "/api/v1/chat/audio/transcribe",
                headers=_headers(w.tenant, "owner", "member"),
                **_upload(session_id=str(session_id)),
            )


async def test_an_oversized_answer_is_refused_before_its_markdown_is_read(
    app_session: AppSessionFactory, provider: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.api.v1 import chat_audio

    def never(_text: str) -> str:
        raise AssertionError("an oversized answer must not be stripped")

    monkeypatch.setattr(chat_audio, "speakable", never)
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        msg_id = await _answer(app_session, w.tenant, session_id, "*|`" * 30_000)
        r = await http.post(
            f"/api/v1/chat/sessions/{session_id}/messages/{msg_id}/speech",
            headers=_headers(w.tenant, "owner", "member"),
        )
    assert r.status_code == 409, r.text
    assert provider == []


async def test_a_duration_far_above_the_reservation_is_audited(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mostly silent VBR audio holds far more seconds per byte than the
    reservation assumes: the overshoot is audited for an admin to see."""
    real_send = httpx.AsyncClient.send
    duration = {"seconds": 5.0}

    async def send(client: Any, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if request.url.host != "speech.example":
            return await real_send(client, request, **kwargs)
        await request.aread()
        return httpx.Response(
            200, json={"text": _SECRET_WORDS, "duration": duration["seconds"]}, request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    async with _http() as http:
        w, *_ = await _speech_world(app_session, http)
        session_id, _ = await _chat(app_session, w.tenant, w.personal_agent, "owner")
        h = _headers(w.tenant, "owner", "member")
        # 1 second reserved (404 bytes): 5 s is within 10x, 50 s is not.
        for seconds in (5.0, 50.0):
            duration["seconds"] = seconds
            r = await http.post(
                "/api/v1/chat/audio/transcribe", headers=h, **_upload(session_id=str(session_id))
            )
            assert r.status_code == 200, r.text
    async with app_session(w.tenant) as db:
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == "chat.audio.allowance_overshoot"
                    )
                )
            )
            .scalars()
            .all()
        )
    [event] = events
    assert event.resource["agent_id"] == str(w.personal_agent)
    assert event.resource["reserved_seconds"] == 1
    assert event.resource["reported_seconds"] == pytest.approx(50.0)
    assert _SECRET_WORDS not in str(event.resource)
