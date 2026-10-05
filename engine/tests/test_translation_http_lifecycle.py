"""Real HTTPX clients with in-memory transports; no sockets, models, or GPU."""
import asyncio
import json
import io
import os

import httpx
import pytest

from engine.runtime import Runtime
from engine.settings import EngineError


def configured_runtime(tmp_path, monkeypatch, handler=None, on_close=None):
    import engine.runtime as module

    clients, requests, processes, transports = [], [], [], []
    real_client = httpx.AsyncClient

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.closed = False

        async def handle_async_request(self, request):
            assert not self.closed
            requests.append(request)
            if handler:
                return await handler(request)
            if request.url.path == "/health":
                return httpx.Response(200)
            return httpx.Response(200, json={"choices": [{
                "message": {"content": "안녕하세요."}, "finish_reason": "stop"}]})

        async def aclose(self):
            assert not self.closed, "A pool must be closed exactly once"
            if on_close:
                await on_close()
            self.closed = True

    def client_factory(**kwargs):
        assert kwargs == {"trust_env": False, "timeout": 45}
        transport = Transport()
        transports.append(transport)
        client = real_client(transport=transport, **kwargs)
        clients.append(client)
        return client

    class Process:
        def __init__(self, args, **kwargs):
            self.args = args
            assert kwargs['stdout'] == module.subprocess.PIPE
            assert kwargs['stderr'] == module.subprocess.STDOUT and kwargs['bufsize'] == 0
            self.stdout = io.BytesIO(b'')
            self.closed = False
            processes.append(self)

        def poll(self):
            return 0 if self.closed else None

        def terminate(self):
            self.closed = True

        def wait(self, timeout):
            assert self.closed
            return 0

    binary = tmp_path / "runtime/llama" / ("llama-server.exe" if os.name == "nt" else "llama-server")
    binary.parent.mkdir(parents=True)
    binary.touch()
    for name in ("HY-MT2-7B-Q6_K.gguf", "other.gguf"):
        model = tmp_path / "models/translation" / name
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_bytes(b"GGUFdummy")
    monkeypatch.setattr(module.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(module.subprocess, "Popen", Process)
    runtime = Runtime(tmp_path)
    monkeypatch.setattr(runtime, "_unload_asr", lambda: None)
    settings = {"mode": "gemini", "translation_model": "models/translation/HY-MT2-7B-Q6_K.gguf"}
    return runtime, settings, clients, requests, processes, transports


@pytest.mark.asyncio
async def test_prepare_and_quality_retries_reuse_pool_with_separate_timeouts(tmp_path, monkeypatch):
    answers = iter(["Still English.", "안녕하세요.", "좋습니다."])

    async def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(200, json={"choices": [{
            "message": {"content": next(answers)}, "finish_reason": "stop"}]})

    runtime, settings, clients, requests, processes, transports = configured_runtime(tmp_path, monkeypatch, handler)
    try:
        await runtime.prepare(settings)
        assert runtime.state == "ready" and len(clients) == 1
        await runtime.prepare(settings)
        assert len(clients) == 1 and len(processes) == 1
        assert await runtime.translate("Hello.", "en", ["Earlier."]) == "안녕하세요."
        assert await runtime.translate("Good.", "en") == "좋습니다."
        assert len(clients) == 1 and not clients[0].is_closed
        assert [r.url.path for r in requests] == ["/health"] + ["/v1/chat/completions"] * 3
        assert set(requests[0].extensions["timeout"].values()) == {2}
        assert all(set(r.extensions["timeout"].values()) == {45} for r in requests[1:])
        assert json.loads(requests[1].content) == json.loads(requests[2].content)
        assert all(r.headers["Authorization"] == "Bearer " + runtime.llama_key for r in requests)
    finally:
        await runtime.release()
    assert clients[0].is_closed and transports[0].closed and processes[0].closed


@pytest.mark.asyncio
async def test_model_replacement_closes_old_pool_and_uses_current_address_and_key(tmp_path, monkeypatch):
    runtime, settings, clients, requests, processes, transports = configured_runtime(tmp_path, monkeypatch)
    try:
        runtime.llama_key = "first-test-key"
        await runtime.prepare(settings)
        first_url = runtime.llama_url
        await runtime.translate("Hello.", "en")
        runtime.llama_key = "second-test-key"
        await runtime.prepare({**settings, "translation_model": "models/translation/other.gguf"})
        assert clients[0].is_closed and transports[0].closed and processes[0].closed
        assert len(clients) == 2 and len(processes) == 2
        await runtime.translate("Hello again.", "en")
        assert str(requests[1].url) == first_url + "/v1/chat/completions"
        assert str(requests[-1].url) == runtime.llama_url + "/v1/chat/completions"
        assert [r.headers["Authorization"] for r in requests] == [
            "Bearer first-test-key", "Bearer first-test-key", "Bearer second-test-key", "Bearer second-test-key"]
        assert not clients[1].headers.get("Authorization")
    finally:
        await runtime.release()


@pytest.mark.asyncio
async def test_release_waits_for_inflight_request_before_closing_pool(tmp_path, monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def handler(request):
        if request.url.path != "/health":
            entered.set()
            await finish.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "안녕하세요."}}]})

    runtime, settings, clients, requests, processes, transports = configured_runtime(tmp_path, monkeypatch, handler)
    await runtime.prepare(settings)
    translating = asyncio.create_task(runtime.translate("Hello.", "en"))
    await asyncio.wait_for(entered.wait(), 1)
    releasing = asyncio.create_task(runtime.release())
    await asyncio.sleep(0)
    assert not releasing.done() and not clients[0].is_closed and not transports[0].closed
    finish.set()
    assert await translating == "안녕하세요."
    await releasing
    with pytest.raises(EngineError, match="번역 모델") as error:
        await runtime.translate("Next.", "en")
    assert error.value.code == "translation_not_ready" and len(clients) == 1
    assert transports[0].closed


@pytest.mark.asyncio
async def test_cancelled_translation_can_reuse_the_open_client(tmp_path, monkeypatch):
    entered = asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        if request.url.path != "/health":
            calls += 1
            if calls == 1:
                entered.set()
                await asyncio.Event().wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "안녕하세요."}}]})

    runtime, settings, clients, requests, processes, transports = configured_runtime(tmp_path, monkeypatch, handler)
    try:
        await runtime.prepare(settings)
        translating = asyncio.create_task(runtime.translate("Hello.", "en"))
        await asyncio.wait_for(entered.wait(), 1)
        translating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await translating
        assert not clients[0].is_closed and not runtime.translation_lock.locked()
        assert await runtime.translate("Next.", "en") == "안녕하세요."
        assert len(clients) == 1
    finally:
        await runtime.release()


@pytest.mark.asyncio
async def test_repeated_cancel_during_close_keeps_locks_until_resources_closed(tmp_path, monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def closing():
        entered.set()
        await finish.wait()

    runtime, settings, clients, requests, processes, transports = configured_runtime(tmp_path, monkeypatch, on_close=closing)
    await runtime.prepare(settings)
    releasing = asyncio.create_task(runtime.release())
    await asyncio.wait_for(entered.wait(), 1)
    releasing.cancel()
    await asyncio.sleep(0)
    releasing.cancel()
    preparing = asyncio.create_task(runtime.prepare(settings))
    await asyncio.sleep(0)
    assert runtime.translation_lock.locked() and runtime.lifecycle.locked()
    assert not preparing.done() and not releasing.done() and len(clients) == 1
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await releasing
    await preparing
    assert transports[0].closed and processes[0].closed and len(clients) == 2
    await runtime.release()
    assert transports[1].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_start_closes_pool_process_and_log(tmp_path, monkeypatch, cancel):
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise ValueError("mock health failure")

    runtime, settings, clients, requests, processes, transports = configured_runtime(tmp_path, monkeypatch, handler)
    preparing = asyncio.create_task(runtime.prepare(settings))
    if cancel:
        await asyncio.wait_for(entered.wait(), 1)
        preparing.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else EngineError):
        await preparing
    assert len(clients) == 1 and clients[0].is_closed and transports[0].closed
    assert processes[0].closed and runtime.process is None and runtime._log_writer is None
    assert processes[0].stdout.closed
    assert runtime.llama_url is None and runtime._translation_client is None
    await runtime.release()


@pytest.mark.asyncio
async def test_asr_failure_closes_pool_and_next_prepare_recreates_it(tmp_path, monkeypatch):
    import engine.runtime as module
    runtime, settings, clients, requests, processes, transports = configured_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "confined_model", lambda root, value, kind: root / value)
    monkeypatch.setattr(module, "asr_backend", lambda path: "whisper")
    monkeypatch.setattr(runtime, "_load_asr", lambda path: None)
    monkeypatch.setattr(runtime, "_warmup_asr", lambda profile: (_ for _ in ()).throw(ValueError("mock warmup failure")))
    with pytest.raises(EngineError):
        await runtime.prepare({**settings, "mode": "local", "asr_model": "unused"})
    assert transports[0].closed and clients[0].is_closed and runtime.state == "error"
    assert not processes[0].closed  # Existing loaded-process reuse policy.
    await runtime.prepare(settings)
    assert len(clients) == 2 and len(processes) == 1 and runtime.state == "ready"
    await runtime.release()


@pytest.mark.asyncio
async def test_idle_status_and_release_never_create_http_resources(tmp_path, monkeypatch):
    runtime, settings, clients, requests, processes, transports = configured_runtime(tmp_path, monkeypatch)
    assert not runtime.status()["translation_ready"]
    with pytest.raises(EngineError):
        await runtime.translate("Hello.", "en")
    await runtime.release()
    await runtime.release()
    assert clients == requests == processes == transports == []


@pytest.mark.asyncio
async def test_direct_ready_caller_gets_one_lazy_client_and_dynamic_headers(tmp_path, monkeypatch):
    runtime, settings, clients, requests, processes, transports = configured_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "status", lambda: {"translation_ready": True})
    runtime.translation_path = tmp_path / "HY-MT2-7B-Q6_K.gguf"
    runtime.llama_url, runtime.llama_key = "http://127.0.0.1:101", "first-test-key"
    try:
        assert await runtime.translate("Hello.", "en") == "안녕하세요."
        runtime.llama_url, runtime.llama_key = "http://127.0.0.1:102", "second-test-key"
        assert await runtime.translate("Next.", "en") == "안녕하세요."
        assert len(clients) == 1
        assert [r.url.port for r in requests] == [101, 102]
        assert [r.headers["Authorization"] for r in requests] == ["Bearer first-test-key", "Bearer second-test-key"]
    finally:
        await runtime.release()
