import asyncio
import struct
import threading
from types import SimpleNamespace

import pytest

from engine.sessions import SessionManager, StreamSession


def pcm(seconds, value=1000):
    return struct.pack('<h', value) * round(seconds * 16000)


class Socket:
    def __init__(self):
        self.events = []
        self.caption = asyncio.Event()
        self.error = asyncio.Event()
        self.recovered = asyncio.Event()

    async def send_json(self, event):
        self.events.append(dict(event))
        if event['type'] == 'caption':
            self.caption.set()
        if event['type'] == 'error':
            self.error.set()
        if event.get('code') == 'buffer_recovered':
            self.recovered.set()


class Runtime:
    asr_backend = 'qwen3_asr'

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = []
        self.translations = []

    async def prepare(self, settings):
        pass

    def transcribe(self, audio, language):
        self.calls.append((bytes(audio), language))
        self.entered.set()
        assert self.release.wait(3)
        self.finished.set()
        return ('日本語。' if len(self.calls) == 1 else '続きの日本語。'), 'ja'

    async def translate(self, source, language, context=None, *, target_language="ko"):
        self.translations.append((source, language))
        return '한국어 번역입니다.'


def session_for(runtime, language='auto', recheck=False):
    socket = Socket()
    settings = SimpleNamespace(data={'asr_profile': 'legacy', 'qwen_boundary_recheck': recheck})
    session = StreamSession(socket, runtime, SessionManager(), settings, 'local', language)
    return session, socket


def track(monkeypatch):
    from engine import fast_qwen_session
    original = fast_qwen_session.FastQwenController
    controllers = []
    fed = asyncio.Event()

    class Tracking(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            controllers.append(self)

        def feed(self, data):
            super().feed(data)
            fed.set()

    monkeypatch.setattr(fast_qwen_session, 'FastQwenController', Tracking)
    return controllers, fed


@pytest.mark.asyncio
@pytest.mark.parametrize('language,expected', [('auto', 'ja'), ('zh', 'zh')])
async def test_ingest_during_early_inference_publishes_final_once_with_correct_language(monkeypatch, language, expected):
    controllers, fed = track(monkeypatch)
    runtime = Runtime()
    session, socket = session_for(runtime, language)
    await session.start()
    try:
        await session.feed(pcm(.4) + pcm(.2, 0))
        assert await asyncio.to_thread(runtime.entered.wait, 2)
        fed.clear()
        await session.feed(pcm(.3, 0))
        await asyncio.wait_for(fed.wait(), 2)
        assert session.audio.bytes == 0 and not socket.caption.is_set()
        assert not runtime.translations
        runtime.release.set()
        await asyncio.wait_for(socket.caption.wait(), 2)
        captions = [e for e in socket.events if e['type'] == 'caption']
        assert len(runtime.calls) == 1 and len(captions) == 1
        assert captions[0]['is_final'] and captions[0]['language'] == expected
        assert runtime.translations == [('日本語。', expected)]
        assert controllers[0].statistics['cache_reuses'] == 1
        assert not any(e['type'] == 'error' for e in socket.events)
    finally:
        runtime.release.set()
        await session.stop()


@pytest.mark.asyncio
async def test_resumed_audio_replaces_stale_early_guess_before_translation(monkeypatch):
    _, fed = track(monkeypatch)
    runtime = Runtime()
    session, socket = session_for(runtime)
    await session.start()
    try:
        first = pcm(.4) + pcm(.2, 0)
        rest = pcm(.2, 2000) + pcm(.5, 0)
        await session.feed(first)
        assert await asyncio.to_thread(runtime.entered.wait, 2)
        fed.clear()
        await session.feed(rest)
        await asyncio.wait_for(fed.wait(), 2)
        runtime.release.set()
        await asyncio.wait_for(socket.caption.wait(), 2)
        assert len(runtime.calls) == 2
        assert runtime.calls[1][0] == first + rest
        assert runtime.translations == [('続きの日本語。', 'ja')]
        assert [e['source_text'] for e in socket.events if e['type'] == 'caption'] == ['続きの日本語。']
    finally:
        runtime.release.set()
        await session.stop()


@pytest.mark.asyncio
async def test_stop_cancels_workers_and_late_model_result_does_not_publish():
    runtime = Runtime()
    session, socket = session_for(runtime)
    await session.start()
    await session.feed(pcm(.4) + pcm(.2, 0))
    assert await asyncio.to_thread(runtime.entered.wait, 2)
    await session.stop()
    runtime.release.set()
    assert await asyncio.to_thread(runtime.finished.wait, 2)
    assert not socket.caption.is_set() and not runtime.translations
    assert not any(t.get_name().startswith('streaming-') and not t.done() for t in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_slow_recognition_overrun_is_visible_and_resumes_with_latest_audio(monkeypatch):
    controllers, fed = track(monkeypatch)
    runtime = Runtime()
    session, socket = session_for(runtime)
    await session.start()
    try:
        for _ in range(4):
            fed.clear()
            await session.feed(pcm(1))
            await asyncio.wait_for(fed.wait(), 2)
        assert await asyncio.to_thread(runtime.entered.wait, 2)
        for _ in range(7):
            fed.clear()
            await session.feed(pcm(1, 2000))
            await asyncio.wait_for(fed.wait(), 2)
        assert session.active and controllers[0].generation == 1
        assert len(runtime.calls) == 1  # No parallel replacement ASR.
        assert not runtime.translations
        await asyncio.wait_for(socket.recovered.wait(), 2)
        runtime.release.set()
        await asyncio.wait_for(socket.caption.wait(), 2)
        assert session.active and len(runtime.calls) == 2
        assert runtime.calls[1][0] == pcm(.2) + pcm(3.8, 2000)
        assert runtime.translations == [('続きの日本語。', 'ja')]
        assert not any(e['type'] == 'error' for e in socket.events)
        assert any(e['type'] == 'warning' and e.get('code') == 'buffer_recovered'
                   for e in socket.events)
    finally:
        runtime.release.set()
        await session.stop()


@pytest.mark.asyncio
async def test_input_audio_gap_discards_inflight_result_and_keeps_sample_clock(monkeypatch):
    controllers, fed = track(monkeypatch)
    runtime = Runtime()
    session, socket = session_for(runtime)
    await session.start()
    try:
        await session.feed(pcm(.4) + pcm(.2, 0))
        assert await asyncio.to_thread(runtime.entered.wait, 2)
        session.input_gap(160)  # Ten milliseconds of original input was lost.
        latest = pcm(.4, 2000) + pcm(.5, 0)
        fed.clear()
        await session.feed(latest)
        await asyncio.wait_for(fed.wait(), 2)
        controller = controllers[0]
        assert controller.generation == 1 and len(runtime.calls) == 1
        assert controller._windows[0].start == 9760
        assert controller._windows[0].pcm == latest
        assert not controller._windows[0].overlaps_previous
        runtime.release.set()
        await asyncio.wait_for(socket.caption.wait(), 2)
        assert session.active and runtime.calls[1][0] == latest
        assert runtime.translations == [('続きの日本語。', 'ja')]
        assert not any(e['type'] == 'error' for e in socket.events)
    finally:
        runtime.release.set()
        await session.stop()


@pytest.mark.asyncio
async def test_optional_boundary_recheck_route_remains_separate(monkeypatch):
    called = asyncio.Event()

    async def recheck(session):
        called.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(StreamSession, '_local_qwen_recheck', recheck)
    runtime = Runtime()
    session, _ = session_for(runtime, recheck=True)
    await session.start()
    try:
        await asyncio.wait_for(called.wait(), 2)
        assert runtime.calls == []
    finally:
        await session.stop()
