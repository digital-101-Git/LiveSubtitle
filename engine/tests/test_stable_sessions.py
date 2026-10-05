import asyncio
import struct
import threading
from types import SimpleNamespace

import pytest

from engine.local_streaming import ASRWord
from engine.sessions import SessionManager, StreamSession
from engine.stable_sessions import AlignPackets, CommittedPhrases


def pcm(seconds, value=1000):
    return struct.pack("<h", value) * round(seconds * 16000)


class Socket:
    def __init__(self):
        self.events = []
        self.caption = asyncio.Event()

    async def send_json(self, event):
        self.events.append(dict(event))
        if event["type"] == "caption":
            self.caption.set()


class Runtime:
    asr_backend = "whisper"

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    async def prepare(self, settings):
        pass

    def transcribe_stream(self, audio, language, prompt):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(3)
        self.finished.set()
        return [ASRWord(0, .4, "你好。")], "zh"

    async def translate(self, source, language, context=None, *, target_language="ko"):
        return "안녕하세요."


def session_for(runtime):
    socket = Socket()
    settings = SimpleNamespace(data={"asr_profile": "stable"})
    session = StreamSession(socket, runtime, SessionManager(), settings, "local", "auto")
    return session, socket


@pytest.mark.asyncio
async def test_feed_continues_during_gpu_and_final_reuses_early_result(monkeypatch):
    from engine import stable_sessions
    original = stable_sessions.LocalWhisperStreaming
    ended = asyncio.Event()

    class Tracking(original):
        def feed(self, data):
            super().feed(data)
            if data == pcm(.3, 0):
                ended.set()

    monkeypatch.setattr(stable_sessions, "LocalWhisperStreaming", Tracking)
    runtime = Runtime()
    session, socket = session_for(runtime)
    await session.start()
    try:
        await session.feed(pcm(.4) + pcm(.2, 0))
        assert await asyncio.to_thread(runtime.entered.wait, 2)
        await session.feed(pcm(.3, 0))
        await asyncio.wait_for(ended.wait(), 2)
        assert not socket.caption.is_set() and session.audio.bytes == 0
        runtime.release.set()
        await asyncio.wait_for(socket.caption.wait(), 2)
        captions = [e for e in socket.events if e["type"] == "caption"]
        assert runtime.calls == 1
        assert [(e["source_text"], e["language"], e["is_final"]) for e in captions] == [("你好。", "zh", True)]
        assert not any(e["type"] == "error" for e in socket.events)
    finally:
        runtime.release.set()
        await session.stop()


@pytest.mark.asyncio
async def test_stop_cancels_ingest_and_ignores_inflight_inference():
    runtime = Runtime()
    session, socket = session_for(runtime)
    await session.start()
    await session.feed(pcm(.4) + pcm(.5, 0))
    assert await asyncio.to_thread(runtime.entered.wait, 2)
    await session.stop()
    runtime.release.set()
    assert await asyncio.to_thread(runtime.finished.wait, 2)
    assert not socket.caption.is_set()
    assert not any(task.get_name().startswith("streaming-") and not task.done()
                   for task in asyncio.all_tasks())


def test_alignatt_packets_preserve_continuous_audio_and_actual_repetitions():
    controller = AlignPackets()
    original = pcm(2.3) + pcm(.5, 0)
    for offset in range(0, len(original), 3200):
        controller.feed(original[offset:offset+3200])
    packets = []
    while (packet := controller.snapshot()) is not None:
        packets.append(packet)
    assert b"".join(item.pcm for item in packets) == original
    assert [item.is_final for item in packets] == [False, False, True]
    controller.feed(pcm(.2) + pcm(.5, 0))
    assert controller.snapshot().pcm == pcm(.2) + pcm(.5, 0)


def test_alignatt_packets_bound_slow_decoder_without_silent_drop():
    from engine.settings import EngineError
    controller = AlignPackets()
    with pytest.raises(EngineError, match="속도"):
        for _ in range(35):
            controller.feed(pcm(1))


def test_committed_words_without_punctuation_use_timed_boundaries():
    phrases = CommittedPhrases()
    words = [ASRWord(i * .5, i * .5 + .5, char) for i, char in enumerate("甲乙丙丁戊己庚辛")]
    assert phrases.accept(words[:4]) == []
    assert phrases.accept(words[4:]) == ["甲乙丙丁戊己"]
    assert phrases.accept([], final=True) == ["庚辛"]


def test_all_complete_sentences_and_actual_repetitions_emit_in_one_packet():
    phrases = CommittedPhrases()
    words = [ASRWord(i, i+.1, "Yes.") for i in range(3)]
    assert phrases.accept(words) == ["Yes.", "Yes.", "Yes."]
    assert phrases.accept([], final=True) == []
