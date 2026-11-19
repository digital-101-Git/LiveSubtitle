"""Real session queue with deterministic incremental recognizer responses."""
import asyncio
from types import SimpleNamespace

import pytest

from engine.r2t2_session import R2T2Captions, R2T2Packets, run
from engine.sessions import SessionManager, StreamSession


def pcm(seconds, sample=2000):
    return sample.to_bytes(2, "little", signed=True) * round(16000 * seconds)


def test_packet_coalescing_preserves_all_audio_and_stops_at_the_endpoint():
    controller = R2T2Packets()
    first = pcm(.64) + pcm(.5, 0)
    second = pcm(.32)
    for section in (first[:17], first[17:], second):
        controller.feed(section)
    one = controller.snapshot()
    two = controller.snapshot()
    assert one.is_final and one.pcm == first
    assert not two.is_final and two.pcm == second
    assert controller.snapshot() is None


class Client:
    def __init__(self):
        self.events = []
        self.changed = asyncio.Condition()

    async def send_json(self, message):
        async with self.changed:
            self.events.append(message)
            self.changed.notify_all()

    async def wait(self, predicate):
        async def event():
            async with self.changed:
                await self.changed.wait_for(lambda: any(predicate(x) for x in self.events))
                return next(x for x in self.events if predicate(x))
        return await asyncio.wait_for(event(), 4)


class Runtime:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.starts = []
        self.closes = []
        self.steps = []

    def create_r2t2_stream(self, language):
        state = SimpleNamespace(language="zh")
        self.starts.append(state)
        return state

    def close_r2t2_stream(self, state):
        self.closes.append(state)

    def r2t2_step(self, state, audio, final=False):
        self.steps.append((state, audio, final))
        if final:
            return "今天不开会。", "今天不开会。"
        if len(self.starts) > 1:
            return "再见。", "再见"
        return "今天要。", "今天"

    async def translate(self, source, language, context, *, target_language="ko"):
        self.entered.set()
        await self.release.wait()
        return "번역 " + source


async def start(runtime):
    client = Client()
    session = StreamSession(client, runtime, SessionManager(), SimpleNamespace(data={}), "local", "auto")
    await session.manager.claim(session)
    session.active = True
    session._local_once = lambda: run(session)
    for fn in (session._local, session._translate):
        session.tasks.append(asyncio.create_task(session._guard(fn)))
    return session, client


@pytest.mark.asyncio
async def test_final_replaces_inflight_preview_on_same_id_without_translating_unstable_suffix():
    runtime = Runtime()
    session, client = await start(runtime)
    try:
        await session.feed(pcm(.32))
        await asyncio.wait_for(runtime.entered.wait(), 4)
        preview = await client.wait(lambda x: x["type"] == "transcript" and not x["is_final"])
        assert preview["text"] == "今天"
        await session.feed(pcm(.5, 0))
        final = await client.wait(lambda x: x["type"] == "transcript" and x["is_final"])
        assert final["segment_id"] == preview["segment_id"] and final["text"] == "今天不开会。"
        runtime.release.set()
        caption = await client.wait(lambda x: x["type"] == "caption")
        assert caption["is_final"] and caption["source_text"] == final["text"] and caption["language"] == "zh"
        assert len(runtime.starts) == len(runtime.closes) == 1
    finally:
        runtime.release.set()
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_input_gap_retires_preview_and_starts_fresh_stream_without_replaying_pcm():
    runtime = Runtime()
    runtime.release.set()
    session, client = await start(runtime)
    try:
        await session.feed(pcm(.32))
        first = await client.wait(lambda x: x["type"] == "caption")
        session.input_gap(1600)
        new_pcm = pcm(.32, 4200)
        await session.feed(new_pcm)
        removed = await client.wait(lambda x: x["type"] == "caption_remove")
        second = await client.wait(lambda x: x["type"] == "caption" and x["source_text"] == "再见")
        assert removed["segment_id"] == first["segment_id"] != second["segment_id"]
        assert runtime.steps[-1][1] == new_pcm
        assert len(runtime.starts) == 2 and runtime.closes == runtime.starts[:1]
    finally:
        await session.stop(notify=False)
    assert runtime.closes == runtime.starts


@pytest.mark.asyncio
async def test_throttle_retry_repeated_sentences_and_empty_final_retraction():
    runtime = Runtime()
    client = Client()
    session = StreamSession(client, runtime, SessionManager(), SimpleNamespace(data={}), "local", "zh")
    session.active = True
    captions = R2T2Captions()
    await captions.publish(session, "你好。你好。", "你好。你好。", "zh", False, 0.0)
    assert session.texts.qsize() == 2
    await captions.publish(session, "你好。你好。好", "你好。你好。好", "zh", False, .2)
    assert session.texts.qsize() == 2
    await captions.publish(session, "你好。你好。好", "你好。你好。好", "zh", True, .21)
    assert (session._translations[1].is_final and session._translations[2].is_final
            and session._translations[1].source == session._translations[2].source == "你好。")
    assert session._translations[3].source == "好"
    # Finals in the queue defer a second utterance's preview. The unchanged
    # candidate is offered again after completed work is released.
    await captions.publish(session, "早", "早", "zh", False, 1.5)
    assert 4 not in session._translations
    runtime.release.set()
    worker = asyncio.create_task(session._translate())
    try:
        await asyncio.wait_for(session.texts.join(), 4)
        await captions.publish(session, "早", "早", "zh", False, 2.5)
        await client.wait(lambda x: x["type"] == "caption" and x["source_text"] == "早")
        await captions.publish(session, "", "", "zh", True, 2.6)
        assert client.events[-1]["type"] == "caption_remove" and client.events[-1]["segment_id"] == 4
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
