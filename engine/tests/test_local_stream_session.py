"""Session integration with the real streaming controller and synthetic PCM."""
import asyncio
from collections import deque
from copy import deepcopy
import struct
import threading
from types import SimpleNamespace

import pytest

from engine.local_streaming import ASRWord, LocalWhisperStreaming
from engine.sessions import SessionManager, StreamSession


def pcm(seconds, amplitude=1000):
    return struct.pack("<h", amplitude) * round(seconds * 16000)


class Socket:
    def __init__(self):
        self.events = []
        self.changed = asyncio.Condition()

    async def send_json(self, event):
        async with self.changed:
            self.events.append(deepcopy(event))
            self.changed.notify_all()

    async def wait_for(self, predicate):
        async def wait():
            async with self.changed:
                await self.changed.wait_for(lambda: predicate(self.events))
        await asyncio.wait_for(wait(), 3)

    def events_of(self, kind):
        return [event for event in self.events if event["type"] == kind]


class ScriptedRuntime:
    asr_backend = "whisper"
    translation_warnings = []

    def __init__(self, responses, detected_language="en"):
        self.responses = deque(responses)
        self.detected_language = detected_language
        self.calls, self.translations = [], []

    async def prepare(self, settings):
        self.settings = settings

    def transcribe_stream(self, audio, language, initial_prompt):
        self.calls.append((audio, language, initial_prompt))
        assert self.responses, "Unexpected duplicate recognition without new audio"
        return [ASRWord(*word) for word in self.responses.popleft()], self.detected_language

    def transcribe(self, *args):
        raise AssertionError("Whisper must use the streaming adapter")

    async def translate(self, text, language, context=None, *, target_language="ko"):
        self.translations.append((text, language, list(context or [])))
        return "번역: " + text


def make_session(runtime, language="en"):
    socket = Socket()
    session = StreamSession(socket, runtime, SessionManager(), SimpleNamespace(data={}), "local", language)
    return session, socket


def final_captions(socket):
    return [event for event in socket.events_of("caption") if event["is_final"]]


async def wait_caption(socket, count, *, final=True):
    # A provisional rendering and its final upgrade share one spoken phrase.
    # Waiting for any two caption events would mistake that for two phrases.
    await socket.wait_for(lambda events: sum(event["type"] == "caption" and event["is_final"] == final
                                            for event in events) >= count)


def assert_no_stream_failures(socket):
    assert not socket.events_of("error")
    assert not socket.events_of("warning")
    assert not socket.events_of("stopped")


@pytest.mark.asyncio
async def test_first_second_keeps_raw_preview_then_translation_is_upgraded_on_agreement():
    runtime = ScriptedRuntime([
        [(.1, .7, "Wrong.")], [(.1, .7, "Right.")], [(.1, .7, "Right.")],
    ])
    session, socket = make_session(runtime)
    await session.start()
    try:
        await session.feed(pcm(1))
        await socket.wait_for(lambda events: any(event["type"] == "transcript" and event["text"] == "Wrong."
                                                 for event in events))
        preview = socket.events_of("transcript")[-1]
        assert (preview["text"], preview["is_final"], preview["preview"]) == ("Wrong.", False, True)
        assert runtime.translations == [] and session.texts.empty()
        assert not socket.events_of("caption")
        await session.feed(pcm(1))
        await wait_caption(socket, 1, final=False)
        provisional = socket.events_of("caption")[-1]
        assert provisional["source_text"] == "Right." and not provisional["is_final"]
        assert runtime.translations == [("Right.", "en", [])]
        await session.feed(pcm(1))
        await wait_caption(socket, 1)
        assert runtime.translations == [("Right.", "en", [])]
        assert [(e["source_text"], e["is_final"]) for e in socket.events_of("caption")] == [
            ("Right.", False), ("Right.", True)]
        final = final_captions(socket)[0]
        assert (final["segment_id"], final["revision"]) == (provisional["segment_id"], provisional["revision"])
        final_transcripts = [e for e in socket.events_of("transcript") if e["is_final"]]
        assert [e["text"] for e in final_transcripts] == ["Right."]
        assert all(e["text"] in {"Wrong.", "Right."} for e in socket.events_of("transcript"))
        assert len(runtime.calls) == 3
        assert_no_stream_failures(socket)
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_rolling_prompt_uses_committed_text_then_resets_for_the_next_utterance():
    runtime = ScriptedRuntime([
        [(.1, .7, "Hello.")],
        [(.1, .7, "Hello."), (.9, 1.8, " More")],
        [(.2, 1.1, " More")],  # PCM was trimmed through Hello's .7-second boundary.
        [(.4, .6, "Hello.")],  # New utterance includes .4 seconds of silent prefix.
    ], detected_language="zh")
    session, socket = make_session(runtime, language="en")
    await session.start()
    try:
        await session.feed(pcm(1))
        await socket.wait_for(lambda events: any(e["type"] == "transcript" for e in events))
        await session.feed(pcm(1))
        await wait_caption(socket, 1)
        await session.feed(pcm(.5, 0))
        await wait_caption(socket, 2)
        await session.feed(pcm(.2) + pcm(.5, 0))
        await wait_caption(socket, 3)
        assert [call[2] for call in runtime.calls] == ["", "", "Hello.", ""]
        assert [len(call[0]) for call in runtime.calls] == [32000, 64000, 57600, 35200]
        assert all(call[1] == "en" for call in runtime.calls)
        assert runtime.translations == [("Hello.", "en", []), ("More", "en", ["Hello."]),
                                        ("Hello.", "en", ["Hello.", "More"])]
        captions = final_captions(socket)
        assert [e["source_text"] for e in captions] == ["Hello.", "More", "Hello."]
        assert [e["segment_id"] for e in captions] == [1, 2, 3]
        assert all(e["language"] == "en" and e["is_final"] for e in captions)
        assert set(e["segment_id"] for e in socket.events_of("caption")) == {1, 2, 3}
        assert_no_stream_failures(socket)
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_identical_spoken_sentences_within_one_window_keep_distinct_caption_ids():
    words = [(.1, .4, "Yes."), (.5, .8, " Yes.")]
    runtime = ScriptedRuntime([words, words], detected_language="ja")
    session, socket = make_session(runtime, language="auto")
    await session.start()
    try:
        await session.feed(pcm(1))
        await socket.wait_for(lambda events: any(e["type"] == "transcript" for e in events))
        assert runtime.translations == []
        await session.feed(pcm(1))
        await wait_caption(socket, 2)
        captions = final_captions(socket)
        assert [(e["segment_id"], e["source_text"], e["language"]) for e in captions] == [
            (1, "Yes.", "ja"), (2, "Yes.", "ja")]
        assert [call[0] for call in runtime.translations] == ["Yes.", "Yes."]
        assert set(e["segment_id"] for e in socket.events_of("caption")) == {1, 2}
        assert_no_stream_failures(socket)
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_time_fallback_preserves_audio_context_without_duplicating_the_text_prompt():
    opening = [(0, .6, "Alpha"), (.8, 1.4, " Beta"),
               (1.6, 2.2, " Gamma"), (2.4, 2.9, " Delta")]
    tail = [(3.1, 3.6, " Epsilon"), (4.1, 4.5, " ends.")]
    runtime = ScriptedRuntime([opening[:1], opening[:2], opening,
                               opening + tail[:1], opening + tail, opening + tail])
    session, socket = make_session(runtime)
    await session.start()
    try:
        for expected in ("Alpha", "Alpha Beta", "Alpha Beta Gamma Delta"):
            await session.feed(pcm(1))
            await socket.wait_for(lambda events: any(e["type"] == "transcript" and e["text"] == expected
                                                      for e in events))
        await session.feed(pcm(1))
        await wait_caption(socket, 1)
        assert final_captions(socket)[0]["source_text"] == "Alpha Beta Gamma Delta"
        await session.feed(pcm(1))
        await socket.wait_for(lambda events: any(e["type"] == "transcript" and e["text"] == "Epsilon ends."
                                                 for e in events))
        await session.feed(pcm(1))
        await wait_caption(socket, 2)
        assert [item["source_text"] for item in final_captions(socket)] == [
            "Alpha Beta Gamma Delta", "Epsilon ends."]
        assert [item["segment_id"] for item in final_captions(socket)] == [1, 2]
        assert [len(call[0]) for call in runtime.calls[4:]] == [160000, 192000]
        assert [call[2] for call in runtime.calls[4:]] == ["", ""]
        assert runtime.translations[-1][2] == ["Alpha Beta Gamma Delta"]
        assert_no_stream_failures(socket)
    finally:
        await session.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_registered_task", [True, False])
async def test_stop_ignores_late_thread_result_even_if_the_waiting_task_is_not_cancelled(monkeypatch, cancel_registered_task):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    accepted = []

    class TrackingController(LocalWhisperStreaming):
        def accept(self, *args):
            accepted.append(args)
            return super().accept(*args)

    class BlockingRuntime(ScriptedRuntime):
        def transcribe_stream(self, audio, language, initial_prompt):
            started.set()
            try:
                assert release.wait(5), "Test failed to release its own fake decoder"
                return [ASRWord(.1, .7, "Too late.")], "en"
            finally:
                finished.set()

    monkeypatch.setattr("engine.sessions.LocalWhisperStreaming", TrackingController)
    runtime = BlockingRuntime([])
    session, socket = make_session(runtime)
    local_task = None
    try:
        if cancel_registered_task:
            await session.start()
        else:
            # Exercise the explicit active guard independently of task cancellation.
            await session.manager.claim(session)
            session.active = True
            local_task = asyncio.create_task(session._local_whisper())
        await session.feed(pcm(1))
        assert await asyncio.to_thread(started.wait, 2)
        await session.stop()
        before = deepcopy(socket.events)
        assert not session.active and session.manager.active is None
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        if local_task is not None:
            await asyncio.wait_for(local_task, 2)
        await asyncio.sleep(0)
        assert socket.events == before
        assert socket.events[-1]["type"] == "stopped"
        assert accepted == [] and runtime.translations == []
        assert not socket.events_of("transcript") and not socket.events_of("caption")
    finally:
        release.set()
        await session.stop(notify=False)
        if local_task is not None and not local_task.done():
            local_task.cancel()
            await asyncio.gather(local_task, return_exceptions=True)
