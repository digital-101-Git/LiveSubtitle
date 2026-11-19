"""Caption protocol for the optional local profiles, using deterministic ASR."""
import asyncio
from types import SimpleNamespace

import pytest

from engine.local_streaming import ASRWord
from engine.qwen_streaming import QwenResult, _agreed_pending
from engine.sessions import SessionManager, StreamSession
from engine.stable_sessions import CommittedPhrases, QwenWindowCaptions


class Socket:
    def __init__(self):
        self.events = []
        self.changed = asyncio.Event()

    async def send_json(self, event):
        self.events.append(event)
        self.changed.set()

    async def caption(self, final):
        return await self.wait_for(lambda event: event["type"] == "caption" and event["is_final"] == final)

    async def wait_for(self, predicate):
        while True:
            self.changed.clear()
            for event in self.events:
                if predicate(event):
                    return event
                if event["type"] == "error":
                    pytest.fail(str(event))
            await asyncio.wait_for(self.changed.wait(), 3)


class Recognizer:
    start = "Look at that small"
    final = "Look at that small bird."

    def __init__(self, backend):
        self.asr_backend = backend
        self.calls = 0

    async def prepare(self, settings):
        pass

    async def translate(self, text, language, context, *, target_language):
        return "번역 " + text

    def transcribe(self, pcm, language):
        self.calls += 1
        return (self.start if self.calls == 1 else self.final), "en"

    def transcribe_stream(self, pcm, language, prompt):
        text, detected = self.transcribe(pcm, language)
        return [ASRWord(i * .16, (i + 1) * .16, (" " if i else "") + part)
                for i, part in enumerate(text.split())], detected

    def create_alignatt_stream(self, language):
        return object()

    def transcribe_alignatt(self, stream, pcm, final):
        self.calls += 1
        words = [ASRWord(0, .75, self.start)] if self.calls == 1 else []
        if final:
            words.append(ASRWord(.75, 1.4, " bird."))
        return words, "en"

    def close_alignatt_stream(self, stream):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,backend", [
    ("stable", "qwen3_asr"), ("stable", "whisper"), ("alignatt", "whisper"),
])
async def test_spoken_partial_and_endpoint_share_caption_id(profile, backend):
    socket = Socket()
    runtime = Recognizer(backend)
    session = StreamSession(socket, runtime, SessionManager(),
                            SimpleNamespace(data={"asr_profile": profile}), "local", "auto")
    await session.start()
    try:
        block = b"\xe8\x03" * 11200
        await session.feed(block)
        await session.feed(block)
        if profile == "stable":
            raw = await socket.wait_for(lambda event: event["type"] == "transcript")
            assert raw["text"] == runtime.start and not any(e["type"] == "caption" for e in socket.events)
            await session.feed(block)
            await session.feed(block)
        draft = await socket.caption(False)
        assert draft["source_text"] == runtime.start
        assert draft["language"] == "en"
        await session.feed(bytes(16000))
        final = await socket.caption(True)
        assert final["source_text"] == runtime.final
        assert final["segment_id"] == draft["segment_id"]
        assert final["revision"] > draft["revision"]
        assert not any(e["type"] == "caption_remove" for e in socket.events)
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_qwen_rewrite_retains_confirmed_ids_and_retracts_the_unsupported_tail():
    socket = Socket()
    session = StreamSession(socket, Recognizer("qwen3_asr"), SessionManager(),
                            SimpleNamespace(data={}), "local", "en")
    session.active = True
    captions = QwenWindowCaptions()
    await captions.publish(session, 1, QwenResult(pending_text="A.", agreed_pending="A."), "en", 0)
    await captions.publish(session, 1, QwenResult(segments=("A.",), pending_text="A.", agreed_pending="A."),
                           "en", 1)
    assert captions.confirmed == ((1, "A."),)
    assert captions.draft == ((2, "A."),)
    await captions.publish(session, 1, QwenResult(replacement_segments=("New.", "A."), window_closed=True),
                           "en", 2)
    # The two identical observations occupy distinct sound intervals.
    assert len({key for key, source in captions.confirmed}) == 2
    assert tuple(source for key, source in captions.confirmed) == ("New.", "A.")
    assert 1 in dict(captions.confirmed) and 2 in session._removed_segments
    assert not captions.draft


@pytest.mark.parametrize("older,current,expected", [
    ("甚至出现交易机。", "甚至出现交易几乎停滞了。", "甚至出现交易"),
    ("My car", "My carpet", "My"), ("I can", "I can't", "I"),
    ("We paid 10", "We paid 10.50", "We paid"), ("It's dark.", "It's dark?", "It's dark"),
    ("We paid 10.", "We paid 10.50", "We paid"), ("지금 여기", "지금 여기는", "지금"),
])
def test_stable_qwen_offers_only_independently_observed_complete_prefixes(older, current, expected):
    assert _agreed_pending(older, current) == expected


def test_alignatt_keeps_standalone_closing_token_with_its_long_phrase():
    phrases = CommittedPhrases()
    assert phrases.accept([ASRWord(.1, 3.3, "甚至出现交易几乎停滞的情况")]) == []
    assert phrases.accept([ASRWord(3.7, 3.8, "。")], True) == ["甚至出现交易几乎停滞的情况。"]


@pytest.mark.asyncio
async def test_alignatt_later_closing_quote_corrects_the_existing_final_id():
    socket = Socket()
    runtime = Recognizer("whisper")
    def transcribe(stream, pcm, final):
        return ([ASRWord(0, .8, "He left!")] if not final else [ASRWord(1.1, 1.2, "”")]), "en"
    runtime.transcribe_alignatt = transcribe
    session = StreamSession(socket, runtime, SessionManager(),
                            SimpleNamespace(data={"asr_profile": "alignatt"}), "local", "en")
    await session.start()
    try:
        await session.feed(b"\xe8\x03" * 16000)
        first = await socket.caption(True)
        await session.feed(bytes(16000))
        corrected = await socket.wait_for(lambda e: e.get("source_text") == "He left!”")
        assert first["segment_id"] == corrected["segment_id"]
        assert first["revision"] < corrected["revision"] and corrected["is_final"]
        assert not any(e.get("source_text") == "”" for e in socket.events)
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["whisper", "qwen3_asr"])
@pytest.mark.parametrize("new_source,expect_draft", [
    ("We can lift the crate carefully", True), ("We cannot lift the crate", False),
])
async def test_new_raw_observation_suppresses_incompatible_slow_preview(backend, new_source, expect_draft):
    runtime = Recognizer(backend)
    runtime.start = runtime.final = "We can lift the crate"
    started, corrected, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    socket = Socket()
    session = StreamSession(socket, runtime, SessionManager(),
                            SimpleNamespace(data={"asr_profile": "stable"}), "local", "en")
    observe = session.observe_draft
    def observing(identifier, source, language):
        changed = observe(identifier, source, language)
        if source == new_source:
            corrected.set()
        return changed
    async def translate(source, language, context, *, target_language):
        if source == runtime.start:
            started.set()
            await release.wait()
        return "번역 " + source
    session.observe_draft = observing
    runtime.translate = translate
    block = b"\xe8\x03" * 11200
    await session.start()
    try:
        await session.feed(block)
        await session.feed(block)
        await socket.wait_for(lambda e: e["type"] == "transcript")
        await session.feed(block)
        await session.feed(block)
        await asyncio.wait_for(started.wait(), 2)
        runtime.final = new_source
        await session.feed(block)
        await session.feed(block)
        await asyncio.wait_for(corrected.wait(), 2)
        release.set()
        await asyncio.wait_for(session.texts.join(), 2)
        assert any(e["type"] == "caption" and e["source_text"] == runtime.start
                   for e in socket.events) == expect_draft
    finally:
        release.set()
        await session.stop(notify=False)
