import asyncio
import os
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from engine.audio import AudioQueue, PCMChunker, remove_overlap
from engine.models import import_gguf
from engine.runtime import Runtime, korean_output
from engine.server import create_app
from engine.sessions import SessionManager, StreamSession
from engine.settings import EngineError, Settings, confined_model


class FakeRuntime:
    def __init__(self):
        self.ready = False
        self.prepared = 0

    def status(self):
        return {"state": "ready" if self.ready else "idle", "asr_ready": self.ready,
                "translation_ready": self.ready, "last_error": None}

    async def prepare(self, settings):
        self.ready = True
        self.prepared += 1

    async def release(self):
        self.ready = False

    async def translate(self, text, language="auto", context=None, *, target_language="ko"):
        return "한국어 자막"

    def transcribe(self, pcm, language):
        return "hello", "en"


@pytest.fixture
def service(tmp_path):
    runtime = FakeRuntime()
    app = create_app(tmp_path, runtime)
    with TestClient(app) as client:
        yield client, app, {"Authorization": "Bearer " + app.state.settings.token}


def start(ws, token):
    ws.send_json({"type": "auth", "token": token})
    ws.send_json({"type": "start", "mode": "local", "language": "en"})
    assert ws.receive_json()["type"] == "status"
    message = ws.receive_json()
    assert message["type"] == "ready"
    return message["session_id"]


def test_http_auth_and_public_health(service):
    client, app, headers = service
    health = client.get("/health")
    assert health.status_code == 200
    assert set(health.json()) == {"ok", "service", "version"}
    assert client.get("/v1/settings").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/settings", headers=headers).status_code == 200
    assert client.get("/v1/settings", headers={**headers, "Origin": "https://malicious.example"}).status_code == 403


def test_auth_and_body_limit_run_before_json_parser(service):
    client, _, headers = service
    assert client.put("/v1/settings", content=b"not-json", headers={"Content-Type": "application/json"}).status_code == 401
    assert client.put("/v1/settings", content=b"x" * 65537, headers=headers).status_code == 413

    def chunked():
        yield b"x" * 32768
        yield b"x" * 32769

    result = client.put("/v1/settings", content=chunked(), headers={**headers, "Content-Type": "application/json"})
    assert result.status_code == 413


def test_api_key_never_returned_and_memory_default(service):
    client, app, headers = service
    response = client.put("/v1/settings", headers=headers, json={"gemini_api_key": "test-private-key", "mode": "gemini"})
    assert response.status_code == 200
    assert "test-private-key" not in response.text
    assert response.json()["gemini_key_set"] is True
    assert response.json()["save_gemini_key"] is False
    assert not app.state.settings.key_file.exists()
    assert "test-private-key" not in app.state.settings.file.read_text()
    assert app.state.settings.key() == "test-private-key"


def test_settings_partial_and_model_traversal(service):
    client, app, headers = service
    result = client.put("/v1/settings", headers=headers, json={"language": "ja"})
    assert result.status_code == 200
    assert result.json()["mode"] == "local"
    for bad in ("../outside.gguf", "models/translation/../../config/settings.json", "models/asr/x.gguf"):
        assert client.put("/v1/settings", headers=headers, json={"translation_model": bad}).status_code == 400


def test_websocket_rejects_audio_or_wrong_token_before_auth(service):
    client, app, _ = service
    for payload in (b"\0\0", {"type": "auth", "token": "wrong"}):
        with client.websocket_connect("/v1/stream") as ws:
            if isinstance(payload, bytes):
                ws.send_bytes(payload)
            else:
                ws.send_json(payload)
            with pytest.raises(WebSocketDisconnect) as caught:
                ws.receive_json()
            assert caught.value.code == 1008
    assert app.state.runtime.prepared == 0


def test_only_one_session_and_stop_then_restart(service):
    client, app, headers = service
    with client.websocket_connect("/v1/stream") as first:
        one = start(first, app.state.settings.token)
        assert client.put("/v1/settings", headers=headers, json={"language": "ja"}).status_code == 409
        with client.websocket_connect("/v1/stream") as second:
            second.send_json({"type": "auth", "token": app.state.settings.token})
            second.send_json({"type": "start"})
            assert second.receive_json()["code"] == "session_busy"
        assert app.state.sessions.active.id == one
        first.send_json({"type": "stop"})
        assert first.receive_json()["type"] == "stopped"
        first.send_json({"type": "start", "mode": "local", "language": "zh"})
        assert first.receive_json()["type"] == "status"
        assert first.receive_json()["session_id"] != one
    assert client.get("/v1/settings", headers=headers).json()["status"]["active_session"] is None


def test_invalid_pcm_stops_session(service):
    client, app, _ = service
    with client.websocket_connect("/v1/stream") as ws:
        start(ws, app.state.settings.token)
        ws.send_bytes(b"x")
        assert ws.receive_json()["code"] == "invalid_audio"


def test_client_audio_gap_keeps_session_and_processes_following_speech(service):
    client, app, _ = service
    with client.websocket_connect("/v1/stream") as ws:
        session_id = start(ws, app.state.settings.token)
        ws.send_json({"type": "audio_gap", "dropped_samples": 1600})
        voice = struct.pack("<320h", *([1000, -1000] * 160))
        ws.send_bytes(voice * 40)
        ws.send_bytes(b"\0" * (640 * 30))
        while True:
            message = ws.receive_json()
            assert message["type"] not in {"error", "stopped"}
            assert message["session_id"] == session_id
            if message["type"] == "caption":
                assert message["text"] == "한국어 자막"
                break
        assert app.state.sessions.active.audio_dropped_samples == 1600
        ws.send_json({"type": "stop"})
        while ws.receive_json()["type"] != "stopped":
            pass


@pytest.mark.parametrize("started, count, code", [
    (False, 1600, "not_started"), (True, True, "invalid_audio_gap")])
def test_client_audio_gap_requires_active_session_and_valid_count(service, started, count, code):
    client, app, _ = service
    with client.websocket_connect("/v1/stream") as ws:
        if started:
            start(ws, app.state.settings.token)
        else:
            ws.send_json({"type": "auth", "token": app.state.settings.token})
        ws.send_json({"type": "audio_gap", "dropped_samples": count})
        assert ws.receive_json()["code"] == code


@pytest.mark.asyncio
async def test_audio_queue_enforces_bytes_and_recovers_after_read():
    queue = AudioQueue(max_bytes=8, max_items=10)
    queue.put_nowait(b"123456")
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(b"789")
    assert queue.bytes == 6
    assert await queue.get() == b"123456"
    queue.put_nowait(b"12345678")
    assert queue.bytes == 8


def test_chunker_silence_and_forced_overlap():
    voice = struct.pack("<320h", *([1000, -1000] * 160))
    silence = b"\0" * 640
    chunker = PCMChunker()
    assert chunker.feed(silence * 200) == []
    utterances = chunker.feed(voice * 110 + silence * 25)
    assert len(utterances) == 1 and not utterances[0].overlaps_previous
    chunker = PCMChunker()
    utterances = chunker.feed(voice * 390)
    assert len(utterances) == 2
    assert utterances[1].overlaps_previous
    assert remove_overlap("hello beautiful world", "beautiful world again") == "again"
    assert remove_overlap("test", "testing") == "testing"


def test_chunker_preserves_soft_onset_and_quiet_tail():
    def frame(amplitude):
        return struct.pack("<320h", *([amplitude, -amplitude] * 160))

    # The first 300 ms is below the gate, then 600 ms clear speech, followed by
    # 700 ms quieter audio. The old gate discarded 100 ms onset and 200 ms tail.
    source = frame(40) * 15 + frame(300) * 30 + frame(100) * 35 + b"\0" * (640 * 25)
    utterances = PCMChunker().feed(source)
    assert len(utterances) == 1
    assert utterances[0].pcm == source


def test_quiet_segment_reaches_speech_specific_vad():
    quiet = struct.pack("<320h", *([100, -100] * 160))
    source = quiet * 20 + b"\0" * (640 * 25)
    utterances = PCMChunker().feed(source)
    assert len(utterances) == 1 and utterances[0].pcm == source


def test_gguf_import_exclusive_and_extension_restriction(service, tmp_path):
    client, app, headers = service
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUF" + b"model" * 5)
    first = client.post("/v1/models/import", headers=headers, json={"path": str(source)})
    assert first.status_code == 200
    target = app.state.settings.root / first.json()["path"]
    original = target.read_bytes()
    source.write_bytes(b"GGUFchanged")
    assert client.post("/v1/models/import", headers=headers, json={"path": str(source)}).status_code == 409
    assert target.read_bytes() == original
    assert client.post("/v1/models/import", headers={**headers, "Origin": "chrome-extension://" + "a" * 32}, json={"path": str(source)}).status_code == 403


def test_confined_model_resolves_symlink_escape(tmp_path):
    base = tmp_path / "models" / "translation"
    base.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (base / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(EngineError):
        confined_model(tmp_path, "models/translation/link/secret.gguf", "translation", must_exist=False)


@pytest.mark.asyncio
async def test_stop_cancels_translation_and_prevents_late_caption(tmp_path):
    started = asyncio.Event()
    finish = asyncio.Event()

    class SlowRuntime(FakeRuntime):
        async def translate(self, *args, target_language="ko"):
            started.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                # Simulate a non-cooperative provider finishing after cancellation.
                return "늦게 도착한 번역"
            return "번역"

    class Socket:
        def __init__(self):
            self.events = []

        async def send_json(self, data):
            self.events.append(data)

    ws, manager = Socket(), SessionManager()
    session = StreamSession(ws, SlowRuntime(), manager, Settings(tmp_path), "local", "en")
    await session.start()
    await session._final("hello", "en")
    await asyncio.wait_for(started.wait(), 1)
    await session.stop()
    assert manager.active is None
    assert not any(event["type"] == "caption" for event in ws.events)
    assert ws.events[-1]["type"] == "stopped"


def test_korean_output_detects_untranslated_sentences():
    assert korean_output("다음 경기는 5분 후에 시작합니다.")
    assert not korean_output("次の試合は5分後に始まります。")
    assert not korean_output("The next match begins in five minutes.")
    assert korean_output("RTX 4080")
    assert not korean_output("Hello")


@pytest.mark.skipif(os.name != "nt", reason="Windows CurrentUser DPAPI")
def test_saved_key_is_encrypted_and_survives_restart(tmp_path):
    settings = Settings(tmp_path)
    settings.update({"gemini_api_key": "private-test-value", "save_gemini_key": True})
    assert b"private-test-value" not in settings.key_file.read_bytes()
    reloaded = Settings(tmp_path)
    assert reloaded.key() == "private-test-value"
    assert reloaded.public()["save_gemini_key"] is True
    reloaded.update({"gemini_api_key": "session-only-value", "save_gemini_key": False})
    assert not reloaded.key_file.exists()
    assert reloaded.key() == "session-only-value"
    assert Settings(tmp_path).public()["gemini_key_set"] is False


@pytest.mark.asyncio
async def test_translation_retries_wrong_language_and_fails_explicitly(tmp_path, monkeypatch):
    outputs = ["次の試合は5分後です。", "다음 경기는 5분 후입니다."]
    requests = []

    class Reply:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": self.content}}]}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, headers, json):
            requests.append(url)
            return Reply(outputs.pop(0))

    monkeypatch.setattr("engine.runtime.httpx.AsyncClient", Client)
    runtime = Runtime(tmp_path)
    runtime.llama_url = "http://127.0.0.1:12345"
    monkeypatch.setattr(runtime, "status", lambda: {"translation_ready": True})
    assert await runtime.translate("次の試合は5分後です。", "ja") == "다음 경기는 5분 후입니다."
    assert len(requests) == 2
    outputs.extend(["Japanese source unchanged", "Still not Korean"])
    with pytest.raises(EngineError) as caught:
        await runtime.translate("hello", "en")
    assert caught.value.code == "translation_language"
    outputs.append("길이 제한으로 잘린 번역")
    monkeypatch.setattr(Reply, "json", lambda self: {"choices": [{"message": {"content": self.content}, "finish_reason": "length"}]})
    with pytest.raises(EngineError) as caught:
        await runtime.translate("long speech", "en")
    assert caught.value.code == "translation_truncated"


@pytest.mark.asyncio
async def test_gemini_prepare_unloads_local_asr(tmp_path, monkeypatch):
    model = tmp_path / "models" / "translation" / "test.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"GGUFdummy")
    runtime = Runtime(tmp_path)
    runtime.asr = object()
    runtime.asr_path = tmp_path / "models" / "asr" / "test"

    async def start_llama(path):
        assert runtime.asr is None

    monkeypatch.setattr(runtime, "_start_llama", start_llama)
    await runtime.prepare({"mode": "gemini", "translation_model": "models/translation/test.gguf"})
    assert runtime.asr is None and runtime.asr_path is None
