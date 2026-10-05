"""Preparation-only warmup contracts. No real model, CUDA, or network is used."""
import asyncio
from contextlib import nullcontext
import logging
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from engine.asr_qwen import QwenASR
from engine.asr_warmup import finish_thread_before_cancel, warmup_alignatt, warmup_whisper
from engine.runtime import Runtime
from engine.settings import EngineError


def test_qwen_warmup_runs_real_private_generation_despite_silence_and_never_decodes(monkeypatch):
    calls = []
    class Batch(dict):
        def to(self, device, dtype):
            calls.append(("to", device, dtype))
            return self
    def request(**kwargs):
        assert kwargs["audio"].dtype == np.float32
        assert kwargs["audio"].shape == (16000,) and not kwargs["audio"].any()
        assert kwargs["language"] == "en" and "prompt" not in kwargs
        calls.append(("features",))
        return Batch(input_ids=np.array([[1, 2, 3]]))
    def generate(**kwargs):
        assert kwargs["max_new_tokens"] == 2 and kwargs["do_sample"] is False
        calls.append(("generate",))
        return np.array([[1, 2, 3, 4, 5]])
    def vad(*args, **kwargs):
        calls.append(("vad",))
        return []
    adapter = QwenASR.__new__(QwenASR)
    adapter.model = SimpleNamespace(device="fake-cuda", dtype="fake-bfloat16", generate=generate)
    adapter.processor = SimpleNamespace(apply_transcription_request=request,
        decode=lambda *a, **k: pytest.fail("Warmup must not parse or return a transcript"))
    adapter.torch = SimpleNamespace(inference_mode=nullcontext,
        cuda=SimpleNamespace(synchronize=lambda device: calls.append(("sync", device))))
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", SimpleNamespace(get_speech_timestamps=vad))
    assert adapter.warmup() is None
    assert [item[0] for item in calls] == ["vad", "features", "to", "generate", "sync"]
    # The public path still rejects silence; warmup did not disable normal VAD.
    assert adapter.transcribe(np.zeros(16000, np.float32), "ja") == ("", "ja")
    assert [item[0] for item in calls].count("generate") == 1


def test_qwen_warmup_rejects_closed_adapter_without_provider_calls():
    adapter = QwenASR.__new__(QwenASR)
    adapter.model = adapter.processor = None
    with pytest.raises(EngineError) as error:
        adapter.warmup()
    assert error.value.code == "asr_not_ready"


def test_whisper_warmup_consumes_lazy_generator_and_uses_bounded_vad_bypass():
    calls = []
    def transcribe(samples, **kwargs):
        assert samples.dtype == np.float32 and samples.shape == (16000,) and not samples.any()
        assert kwargs == {"language": "en", "task": "transcribe", "beam_size": 5,
            "temperature": 0.0, "condition_on_previous_text": False,
            "vad_filter": False, "no_speech_threshold": None, "log_prob_threshold": None,
            "compression_ratio_threshold": None, "word_timestamps": False, "max_new_tokens": 2}
        def results():
            calls.append("native inference")
            yield SimpleNamespace(text="must never be shown")
            calls.append("finished")
        return results(), SimpleNamespace(language="en")
    assert warmup_whisper(SimpleNamespace(transcribe=transcribe)) is None
    assert calls == ["native inference", "finished"]


@pytest.mark.parametrize("fail", [False, True])
def test_alignatt_warmup_uses_disposable_stream_and_closes_on_failure(fail):
    events = []
    def feed(pcm, final):
        assert pcm == bytes(32000) and final is True
        events.append("feed")
        if fail:
            raise ValueError("mock native failure")
        return ["must never be shown"], "en"
    stream = SimpleNamespace(feed=feed, close=lambda: events.append("close"))
    def create(language):
        assert language == "en"
        events.append("create")
        return stream
    adapter = SimpleNamespace(create_stream=create)
    if fail:
        with pytest.raises(ValueError):
            warmup_alignatt(adapter)
    else:
        assert warmup_alignatt(adapter) is None
    assert events == ["create", "feed", "close"]


def test_runtime_warmup_runs_under_lock_once_without_touching_hints_or_outputs(tmp_path, caplog):
    runtime = Runtime(tmp_path)
    calls = []
    def warmup():
        assert runtime.asr_lock.locked()
        calls.append("warm")
    runtime.asr = SimpleNamespace(warmup=warmup)
    runtime.asr_backend = "qwen3_asr"
    runtime.asr_hints, runtime.asr_hint_prompt = ("東京",), "Vocabulary: 東京"
    with caplog.at_level(logging.DEBUG, logger="engine.runtime"):
        runtime._warmup_asr("legacy")
        elapsed = runtime.asr_warmup_ms
        runtime._warmup_asr("stable")
    assert calls == ["warm"] and elapsed is not None and elapsed >= 0
    assert runtime.asr_warmup_ms == elapsed
    assert runtime.asr_hints == ("東京",) and runtime.asr_hint_prompt == "Vocabulary: 東京"
    assert "elapsed_ms=" in caplog.text and "東京" not in caplog.text
    assert list(tmp_path.iterdir()) == []  # No caption/history/audio/log file created.


def test_failed_warmup_is_not_cached_and_is_retried(tmp_path):
    runtime = Runtime(tmp_path)
    count = 0
    def warmup():
        nonlocal count
        count += 1
        if count == 1:
            raise ValueError("mock native failure")
    runtime.asr = SimpleNamespace(warmup=warmup)
    runtime.asr_backend = "qwen3_asr"
    with pytest.raises(EngineError) as error:
        runtime._warmup_asr("legacy")
    assert error.value.code == "asr_warmup_failed" and runtime.asr_warmup_ms is None
    assert not runtime._asr_warmed
    runtime._warmup_asr("legacy")
    assert count == 2 and runtime.asr_warmup_ms is not None


def test_unload_resets_warmup_marker_for_next_model(tmp_path, monkeypatch):
    import engine.asr_qwen as qwen
    monkeypatch.setattr(qwen, "clear_cuda_cache", lambda: None)
    runtime = Runtime(tmp_path)
    calls = []
    runtime.asr = SimpleNamespace(warmup=lambda: calls.append("warm"), close=lambda: calls.append("close"))
    runtime.asr_backend = "qwen3_asr"
    runtime._warmup_asr("legacy")
    runtime._unload_asr()
    assert not runtime._asr_warmed and runtime.asr_warmup_ms is None
    runtime.asr = SimpleNamespace(warmup=lambda: calls.append("new warm"))
    runtime.asr_backend = "qwen3_asr"
    runtime._warmup_asr("legacy")
    assert calls == ["warm", "close", "new warm"]


def setup_prepare(tmp_path, monkeypatch, warmup):
    import engine.runtime as module
    runtime = Runtime(tmp_path)
    model = SimpleNamespace(warmup=warmup, close=lambda: None)
    runtime.asr = model
    runtime.asr_path = tmp_path / "asr"
    runtime.asr_backend = "qwen3_asr"
    monkeypatch.setattr(module, "confined_model", lambda root, value, kind: tmp_path / kind)
    monkeypatch.setattr(module, "validate_gguf", lambda path: None)
    monkeypatch.setattr(module, "asr_backend", lambda path: "qwen3_asr")
    async def start(path):
        assert runtime.state == "loading"
    monkeypatch.setattr(runtime, "_start_llama", start)
    settings = {"mode": "local", "asr_model": "asr", "translation_model": "translation"}
    return runtime, settings


@pytest.mark.asyncio
async def test_prepare_reaches_ready_only_after_warmup_and_reuses_same_model(tmp_path, monkeypatch):
    calls = []
    def warmup():
        assert runtime.state == "loading" and runtime.asr_lock.locked()
        calls.append("warm")
    runtime, settings = setup_prepare(tmp_path, monkeypatch, warmup)
    await runtime.prepare(settings)
    assert runtime.state == "ready" and calls == ["warm"]
    await runtime.prepare(settings)
    assert runtime.state == "ready" and calls == ["warm"]


@pytest.mark.asyncio
async def test_failed_prepare_warmup_never_advertises_ready(tmp_path, monkeypatch):
    def warmup():
        raise RuntimeError("GPU mocked error")
    runtime, settings = setup_prepare(tmp_path, monkeypatch, warmup)
    with pytest.raises(EngineError) as error:
        await runtime.prepare(settings)
    assert error.value.code == "asr_warmup_failed" and runtime.state == "error"


@pytest.mark.asyncio
async def test_cancelled_warmup_drains_native_work_before_release_can_unload(tmp_path, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    order = []
    def warmup():
        entered.set()
        assert finish.wait(5), "test failed to release native worker"
        order.append("native done")
    runtime, settings = setup_prepare(tmp_path, monkeypatch, warmup)
    async def stop():
        order.append("stop llama")
    monkeypatch.setattr(runtime, "_stop_llama", stop)
    def unload():
        with runtime.asr_lock:
            order.append("unload")
            runtime.asr = None
    monkeypatch.setattr(runtime, "_unload_asr", unload)
    preparation = asyncio.create_task(runtime.prepare(settings))
    releasing = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        preparation.cancel()
        await asyncio.sleep(.02)
        preparation.cancel()  # Repeated cancellation must not leak the worker.
        releasing = asyncio.create_task(runtime.release())
        await asyncio.sleep(.02)
        assert not preparation.done() and not releasing.done()
        assert runtime.lifecycle.locked() and runtime.state == "loading"
        assert order == []
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await preparation
        if releasing is not None:
            await releasing
    assert order == ["native done", "stop llama", "unload"]
    assert runtime.state == "idle" and runtime.asr is None


@pytest.mark.asyncio
async def test_thread_helper_propagates_normal_worker_error():
    def fail():
        raise ValueError("native error")
    with pytest.raises(ValueError, match="native error"):
        await finish_thread_before_cancel(fail)


@pytest.mark.asyncio
async def test_cancelled_thread_error_does_not_replace_cancellation():
    entered, finish = threading.Event(), threading.Event()
    def fail():
        entered.set()
        assert finish.wait(5)
        raise ValueError("native error after cancellation")
    task = asyncio.create_task(finish_thread_before_cancel(fail))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(.01)
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
