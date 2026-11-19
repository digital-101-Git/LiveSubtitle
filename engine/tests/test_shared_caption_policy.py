"""Shared provisional/final delivery across local and provider recognition."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from engine.sessions import SessionManager, StreamSession
from engine.streaming_sentences import SentenceUpdate, StreamingSentences


class Socket:
    def __init__(self):
        self.events = []

    async def send_json(self, event):
        self.events.append(event)


class Translator:
    def __init__(self):
        self.calls = []
        self.block_source = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def translate(self, source, language, context, *, target_language):
        self.calls.append((source, tuple(context)))
        if source == self.block_source:
            self.started.set()
            await self.release.wait()
        return f"{source} | {context}"


def make(mode="local"):
    runtime, socket = Translator(), Socket()
    session = StreamSession(socket, runtime, SessionManager(), SimpleNamespace(data={}), mode, "auto")
    session.active = True
    return session, runtime, socket


async def drain(session):
    while not session.texts.empty():
        segment, work = session.texts.get_nowait()
        await session._translate_sentence(segment, work)
        session.texts.task_done()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["gemini", "local"])
@pytest.mark.parametrize(("first", "second", "expect_draft"), [
    ("we", "we are here", True), ("甚至", "甚至出现", True),
    ("これは", "これは私の", True), ("여기", "여기는", False),
    ("can", "cannot", False), ("I can", "I can’t", False),
    ("ten", "tense", False), ("10", "10.50", False), ("1.", "1.6", False),
    ("I left", "I stayed", False),
])
async def test_late_prefix_before_next_translation(mode, first, second, expect_draft):
    session, runtime, socket = make(mode)
    runtime.block_source = first
    worker = asyncio.create_task(session._translate())
    try:
        await session._apply_sentences([SentenceUpdate(1, first, False)], "en")
        await asyncio.wait_for(runtime.started.wait(), 2)
        await session._apply_sentences([SentenceUpdate(1, second, False)], "en")
        runtime.release.set()
        await asyncio.wait_for(session.texts.join(), 2)
        drafts = [e for e in socket.events if e["type"] == "caption"]
        assert [e["source_text"] for e in drafts] == ([first, second] if expect_draft else [second])
        assert all(not e["is_final"] for e in drafts) and session._context == {}
        assert drafts[-1]["revision"] == 2
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["correction", "language", "final", "gap", "remove"])
async def test_lagging_result_cannot_resurrect_after_authority_changes(invalidate):
    session, runtime, socket = make()
    runtime.block_source = "we"
    worker = asyncio.create_task(session._translate())
    try:
        await session._apply_sentences([SentenceUpdate(1, "we", False)], "en")
        await asyncio.wait_for(runtime.started.wait(), 2)
        if invalidate == "correction":
            await session._apply_sentences([SentenceUpdate(1, "they", False)], "en")
        if invalidate == "gap":
            session.break_audio_context()
        if invalidate == "remove":
            await session._apply_sentences([SentenceUpdate(1, "we", True, removed=True)], "en")
        await session._apply_sentences([SentenceUpdate(1, "we are", invalidate == "final")],
                                       "ja" if invalidate == "language" else "en")
        runtime.release.set()
        await asyncio.wait_for(session.texts.join(), 2)
        assert not any(e.get("source_text") == "we" for e in socket.events)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "expected_context"), [
    ("HY-MT2-7B-Q6_K.gguf", ("It was a bag.",)), ("generic.gguf", ("It was a bag.",)),
    ("translategemma-12b-it-Q4_K_M.gguf", ()), ("milmmt-46-7b-Q4_K_M.gguf", ()),
])
async def test_exact_final_promotes_only_when_effective_context_matches(model, expected_context):
    session, runtime, socket = make()
    runtime.translation_path = model
    first, second = "It was a bag.", "It was light."
    for is_final in (False, True):
        await session._apply_sentences([SentenceUpdate(1, first, is_final),
                                        SentenceUpdate(2, second, is_final)], "en")
        await drain(session)
    assert [context for text, context in runtime.calls if text == second] == (
        [(), expected_context] if expected_context else [()])
    finals = [e for e in socket.events if e.get("type") == "caption" and e["is_final"]]
    assert [e["segment_id"] for e in finals] == [1, 2]


@pytest.mark.asyncio
async def test_completed_draft_waits_for_changed_earlier_final_before_reuse():
    session, runtime, socket = make()
    await session._apply_sentences([SentenceUpdate(1, "Earlier draft.", False),
                                    SentenceUpdate(2, "He agrees.", False)], "en")
    await drain(session)
    await session._apply_sentences([SentenceUpdate(1, "Earlier correction.", True),
                                    SentenceUpdate(2, "He agrees.", True)], "en")
    assert not [e for e in socket.events if e["type"] == "caption" and e["is_final"]]
    await drain(session)
    assert runtime.calls[-2:] == [("Earlier correction.", ()), ("He agrees.", ("Earlier correction.",))]
    assert [e["segment_id"] for e in socket.events if e["type"] == "caption" and e["is_final"]] == [1, 2]


@pytest.mark.asyncio
async def test_inflight_second_draft_is_requeued_behind_an_earlier_final():
    session, runtime, socket = make()
    await session._apply_sentences([SentenceUpdate(1, "First draft.", False)], "en")
    await drain(session)
    runtime.block_source = "It broke."
    worker = asyncio.create_task(session._translate())
    try:
        await session._apply_sentences([SentenceUpdate(2, "It broke.", False)], "en")
        await asyncio.wait_for(runtime.started.wait(), 2)
        await session._apply_sentences([SentenceUpdate(1, "A glass fell.", True),
                                        SentenceUpdate(2, "It broke.", True)], "en")
        runtime.release.set()
        await asyncio.wait_for(session.texts.join(), 2)
        assert runtime.calls[-2:] == [("A glass fell.", ()), ("It broke.", ("A glass fell.",))]
        finals = [e for e in socket.events if e["type"] == "caption" and e["is_final"]]
        assert [e["segment_id"] for e in finals] == [1, 2]
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_hymt2_ignores_an_overlong_background_sentence_when_promoting():
    session, runtime, _ = make()
    runtime.translation_path = "HY-MT2-7B-Q6_K.gguf"
    long_sentence = "a" * 1000
    for is_final in (False, True):
        await session._apply_sentences([SentenceUpdate(1, long_sentence, is_final),
                                        SentenceUpdate(2, "It fell.", is_final)], "en")
        await drain(session)
    assert runtime.calls == [(long_sentence, ()), ("It fell.", ())]


@pytest.mark.asyncio
async def test_identical_raw_preview_dedup_does_not_skip_translation_or_final():
    session, runtime, socket = make()
    await session.emit({"type": "transcript", "segment_id": 1, "text": "we are", "is_final": False, "preview": True})
    for is_final in (False, True):
        await session._apply_sentences([SentenceUpdate(1, "we are", is_final)], "en")
        await drain(session)
    assert [e["is_final"] for e in socket.events if e["type"] == "transcript"] == [False, True]
    assert [e["is_final"] for e in socket.events if e["type"] == "caption"] == [False, True]
    assert runtime.calls == [("we are", ())]


@pytest.mark.asyncio
async def test_provider_raw_correction_invalidates_known_clause_before_its_next_stable_offer():
    session, runtime, socket = make("gemini")
    runtime.block_source = "I can."
    worker = asyncio.create_task(session._translate())
    tracker = StreamingSentences()
    async def observe(text, at):
        await session._gemini_message(json.dumps({"serverContent": {"interimInputTranscription": {"text": text}}}),
                                      tracker, at)
    try:
        for when in (0, 0.7):
            await observe("Listen. I can.", when)
        await asyncio.wait_for(runtime.started.wait(), 2)
        await observe("Listen. I can't.", 0.8)
        runtime.release.set()
        await asyncio.wait_for(session.texts.join(), 2)
        assert [e["source_text"] for e in socket.events if e["type"] == "caption"] == ["Listen."]
        for when in (0.9, 1.7):
            await observe("Listen. I can.", when)
        await asyncio.wait_for(session.texts.join(), 2)
        captions = [e for e in socket.events if e["type"] == "caption"]
        assert [(e["source_text"], e["revision"]) for e in captions] == [("Listen.", 1), ("I can.", 2)]
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
