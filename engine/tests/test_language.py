"""Source language selection must survive conflicting recognizer metadata."""
import asyncio
import struct
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from engine.runtime import Runtime
from engine.server import create_app
from engine.sessions import SessionManager, StreamSession
from engine.settings import Settings


@pytest.mark.parametrize("selected,whisper_language,returned", [
    ("en", "en", "en"),
    ("zh", "zh", "zh"),
    ("ja", "ja", "ja"),
    ("ko", "ko", "ko"),
    ("auto", None, "ja"),
    ("", None, "ja"),
    (None, None, "ja"),
])
def test_whisper_selection_overrides_conflicting_metadata(tmp_path, selected, whisper_language, returned):
    calls = []

    class WhisperProvider:
        def transcribe(self, samples, **kwargs):
            calls.append(kwargs)
            segment = SimpleNamespace(text="中文原文", no_speech_prob=0.01,
                                      avg_logprob=-0.1, compression_ratio=1.0)
            # Deliberately conflict with forced Chinese/English to catch the bug.
            return iter([segment]), SimpleNamespace(language="ja")

    runtime = Runtime(tmp_path)
    runtime.asr = WhisperProvider()
    text, language = runtime.transcribe(b"\0\0" * 1600, selected)
    assert calls[0]["language"] == whisper_language
    assert calls[0]["task"] == "transcribe"
    assert text == "中文原文" and language == returned


class ConflictingAdapter:
    def __init__(self):
        self.asr_languages = []
        self.translation_languages = []
        self.prepared_language = None

    def status(self):
        return {"state": "ready", "asr_ready": True, "translation_ready": True, "last_error": None}

    async def prepare(self, settings):
        self.prepared_language = settings["language"]

    async def release(self):
        pass

    def transcribe(self, pcm, language):
        self.asr_languages.append(language)
        return "中文原文", "ja"

    async def translate(self, text, language, context=None, *, target_language="ko"):
        self.translation_languages.append(language)
        return "한국어 번역"


@pytest.mark.asyncio
@pytest.mark.parametrize("selected,expected", [("zh", "zh"), ("en", "en"), ("ja", "ja"), ("ko", "ko"), ("auto", "ja")])
async def test_session_routes_selected_or_detected_language_to_translation(tmp_path, selected, expected):
    class Socket:
        def __init__(self):
            self.messages = asyncio.Queue()

        async def send_json(self, message):
            self.messages.put_nowait(message)

    runtime, socket, manager = ConflictingAdapter(), Socket(), SessionManager()
    session = StreamSession(socket, runtime, manager, Settings(tmp_path), "local", selected)
    try:
        await session.start()
        assert runtime.prepared_language == selected
        voice = struct.pack("<320h", *([300, -300] * 160))
        # Exercise PCM -> ASR -> final queue -> translator -> caption, without a GPU.
        await session.feed(voice * 20 + b"\0" * (640 * 25))
        while True:
            message = await asyncio.wait_for(socket.messages.get(), 2)
            if message["type"] == "caption":
                break
        assert runtime.asr_languages == [selected]
        assert runtime.translation_languages == [expected]
        assert message["language"] == expected
        assert message["source_text"] == "中文原文" and message["text"] == "한국어 번역"
    finally:
        await session.stop(notify=False)


def test_settings_language_omission_and_explicit_empty_have_distinct_meanings(tmp_path):
    settings = Settings(tmp_path)
    settings.update({"language": "zh"})
    settings.update({"mode": "local"})
    assert Settings(tmp_path).data["language"] == "zh"
    for selection in ("", None, "   "):
        settings.update({"language": "zh"})
        settings.update({"language": selection})
        assert settings.data["language"] == "auto"
        assert Settings(tmp_path).data["language"] == "auto"


@pytest.mark.parametrize("start_fields,expected", [({}, "zh"), ({"language": ""}, "auto"),
                                                      ({"language": None}, "auto"), ({"language": "ja"}, "ja")])
def test_websocket_omission_uses_saved_selection_but_empty_means_auto(tmp_path, start_fields, expected):
    app = create_app(tmp_path, ConflictingAdapter())
    with TestClient(app) as client:
        token = app.state.settings.token
        headers = {"Authorization": "Bearer " + token}
        assert client.put("/v1/settings", headers=headers, json={"language": "zh"}).status_code == 200
        with client.websocket_connect("/v1/stream") as ws:
            ws.send_json({"type": "auth", "token": token})
            ws.send_json({"type": "start", **start_fields})
            assert ws.receive_json()["type"] == "status"
            assert ws.receive_json()["type"] == "ready"
            assert app.state.sessions.active.language == expected
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "stopped"
        # A per-session override does not silently replace the stored preference.
        assert client.get("/v1/settings", headers=headers).json()["language"] == "zh"


def test_settings_api_normalizes_null_and_empty_language(tmp_path):
    app = create_app(tmp_path, ConflictingAdapter())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + app.state.settings.token}
        for value in (None, ""):
            client.put("/v1/settings", headers=headers, json={"language": "zh"})
            response = client.put("/v1/settings", headers=headers, json={"language": value})
            assert response.status_code == 200 and response.json()["language"] == "auto"
