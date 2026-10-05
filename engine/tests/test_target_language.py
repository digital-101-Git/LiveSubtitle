"""Target selection persists and reaches translation, captions, and local logs."""
import json

import pytest
from fastapi.testclient import TestClient

from engine.history import CaptionHistory
from engine.server import create_app
from engine.sessions import SessionManager, StreamSession
from engine.settings import EngineError, Settings, normalize_target_language
from engine.tests.test_gemini import ClientSocket, ProviderSocket


class LanguageRuntime:
    def __init__(self):
        self.calls = []
        self.prepared = []

    def status(self):
        return {"state": "ready"}

    async def prepare(self, settings):
        self.prepared.append(dict(settings))

    async def release(self):
        pass

    async def translate(self, text, language="auto", context=None, *, target_language="ko"):
        self.calls.append((text, language, target_language))
        return {"ko": "안녕하세요.", "en": "Hello.", "zh": "你好。", "ja": "こんにちは。"}[target_language]


def test_old_settings_default_and_saved_selection_survive_restart(tmp_path):
    settings = Settings(tmp_path)
    settings.file.write_text(json.dumps({"language": "zh"}), encoding="utf-8")
    settings = Settings(tmp_path)
    assert settings.data["target_language"] == "ko"
    settings.update({"language": "ko", "target_language": " EN "})
    settings.update({"mode": "local"})
    loaded = Settings(tmp_path)
    assert loaded.data["language"] == "ko"
    assert loaded.data["target_language"] == "en"
    for value in (None, "", "   "):
        loaded.update({"target_language": value})
        assert Settings(tmp_path).data["target_language"] == "ko"


@pytest.mark.parametrize("bad", ["auto", "fr", "zh-CN", 1, [], {}])
def test_invalid_target_is_rejected(bad):
    with pytest.raises(EngineError) as error:
        normalize_target_language(bad)
    assert error.value.code == "invalid_target_language"


def test_translate_api_saved_default_override_and_validation(tmp_path):
    runtime = LanguageRuntime()
    app = create_app(tmp_path, runtime)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + app.state.settings.token}
        assert client.put("/v1/settings", headers=headers,
                          json={"language": "ko", "target_language": "ja"}).status_code == 200
        for fields, expected in [({}, "ja"), ({"target_language": "en"}, "en"),
                                 ({"target_language": None}, "ko"), ({"target_language": ""}, "ko")]:
            reply = client.post("/v1/translate", headers=headers,
                                json={"text": "안녕하세요.", "source_language": "ko", **fields})
            assert reply.status_code == 200
            assert reply.json()["target_language"] == expected
            assert runtime.calls[-1] == ("안녕하세요.", "ko", expected)
        count = len(runtime.calls)
        reply = client.post("/v1/translate", headers=headers,
                            json={"text": "Hello.", "target_language": "auto"})
        assert reply.status_code == 400 and len(runtime.calls) == count
        assert app.state.settings.data["target_language"] == "ja"


@pytest.mark.parametrize("fields,expected", [({}, "ja"), ({"target_language": "zh"}, "zh"),
                                            ({"target_language": None}, "ko"), ({"target_language": ""}, "ko")])
def test_websocket_routes_target_without_replacing_saved_default(tmp_path, fields, expected):
    runtime = LanguageRuntime()
    app = create_app(tmp_path, runtime)
    app.state.settings.update({"language": "ko", "target_language": "ja"})
    with TestClient(app) as client:
        with client.websocket_connect("/v1/stream") as ws:
            ws.send_json({"type": "auth", "token": app.state.settings.token})
            ws.send_json({"type": "start", **fields})
            assert ws.receive_json()["type"] == "status"
            ready = ws.receive_json()
            assert ready["type"] == "ready" and ready["target_language"] == expected
            assert runtime.prepared[-1]["target_language"] == expected
            assert app.state.sessions.active.language == "ko"
            headers = {"Authorization": "Bearer " + app.state.settings.token}
            assert client.put("/v1/settings", headers=headers, json={"target_language": "en"}).status_code == 409
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "stopped"
        assert app.state.settings.data["target_language"] == "ja"


def test_invalid_start_target_fails_before_preparing_models(tmp_path):
    runtime = LanguageRuntime()
    app = create_app(tmp_path, runtime)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/stream") as ws:
            ws.send_json({"type": "auth", "token": app.state.settings.token})
            ws.send_json({"type": "start", "target_language": "auto"})
            assert ws.receive_json()["code"] == "invalid_target_language"
    assert not runtime.prepared


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["ko", "en", "zh", "ja"])
async def test_session_snapshot_caption_and_history_target(tmp_path, target):
    settings = Settings(tmp_path)
    runtime, socket = LanguageRuntime(), ClientSocket()
    manager = SessionManager(CaptionHistory(tmp_path))
    session = StreamSession(socket, runtime, manager, settings, "local", "ko", target_language=target)
    try:
        await session.start()
        settings.data["target_language"] = "en" if target != "en" else "zh"
        await session._final("안녕하세요.", "ko")
        caption = await socket.wait_type("caption")
        await manager.flush_history()
        assert runtime.calls == [("안녕하세요.", "ko", target)]
        assert caption["language"] == "ko" and caption["target_language"] == target
        assert manager.history.snapshot()[0]["target_language"] == target
        assert manager.history.snapshot()[0]["text"] == caption["text"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_gemini_korean_input_hint_and_independent_translation_target(tmp_path, monkeypatch):
    provider = ProviderSocket()
    async def connect(*args, **kwargs):
        return provider
    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    settings = Settings(tmp_path)
    settings.ephemeral_key = "local-test-only"
    session = StreamSession(ClientSocket(), LanguageRuntime(), SessionManager(), settings,
                            "gemini", "ko", target_language="ja")
    try:
        await session.start()
        assert provider.sent[0]["setup"]["inputAudioTranscription"]["languageCodes"] == ["ko-KR"]
        assert session.target_language == "ja"
    finally:
        await session.stop(notify=False)
