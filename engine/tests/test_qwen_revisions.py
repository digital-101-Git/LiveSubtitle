"""Authoritative Qwen revisions retain chronological speech identities."""
import asyncio
from types import SimpleNamespace

import pytest

from engine.qwen_revisions import reconcile
from engine.qwen_streaming import QwenResult
from engine.sessions import SessionManager, StreamSession
from engine import stable_sessions


def test_leading_insertion_keeps_all_existing_ids_and_allocates_fresh_speech():
    result = reconcile([(1, "A."), (2, "B."), (3, "C.")], ("X.", "A.", "B.", "C."), 3)
    assert result.captions == ((4, "X."), (1, "A."), (2, "B."), (3, "C."))
    assert result.removed == () and result.last_id == 4


def test_leading_deletion_retracts_only_deleted_sentence():
    result = reconcile([(1, "A."), (2, "B."), (3, "C.")], ("B.", "C."), 8)
    assert result.captions == ((2, "B."), (3, "C."))
    assert result.removed == ((1, "A."),) and result.last_id == 8


@pytest.mark.parametrize("before,after,expected", [
    ([(1, "Yes."), (2, "Yes.")], ("Yes.", "Yes.", "Yes."), ((1, "Yes."), (2, "Yes."), (3, "Yes."))),
    ([(1, "Yes."), (2, "Yes."), (3, "No.")], ("Yes.", "No."), ((2, "Yes."), (3, "No."))),
    ([(1, "顾小糖。"), (2, "Yes.")], ("顧 小糖！", "YES?"), ((1, "顧 小糖！"), (2, "YES?"))),
    ([(1, "A."), (2, "Old."), (3, "C.")], ("A.", "New.", "C."), ((1, "A."), (2, "New."), (3, "C."))),
])
def test_ordered_matching_preserves_occurrences_and_authoritative_spelling(before, after, expected):
    result = reconcile(before, after, max(identifier for identifier, _ in before))
    assert result.captions == expected
    assert len({identifier for identifier, _ in result.captions}) == len(result.captions)


def test_split_and_merge_preserve_every_final_sentence():
    split = reconcile([(1, "One. Two."), (2, "End.")], ("One.", "Two.", "End."), 2)
    assert split.captions == ((1, "One."), (3, "Two."), (2, "End."))
    merged = reconcile(split.captions, ("One. Two.", "End."), split.last_id)
    assert merged.captions == ((1, "One. Two."), (2, "End."))
    assert merged.removed == ((3, "Two."),)


def test_empty_final_retracts_all_and_future_ids_never_reuse_them():
    removed = reconcile([(7, "Yes.")], (), 10)
    assert removed.captions == () and removed.removed == ((7, "Yes."),)
    assert reconcile([], ("Yes.",), removed.last_id).captions == ((11, "Yes."),)


class Socket:
    def __init__(self):
        self.events = []

    async def send_json(self, event):
        self.events.append(dict(event))


class Runtime:
    def __init__(self):
        self.calls = []

    async def translate(self, source, language, context=None, *, target_language="ko"):
        self.calls.append((source, language, tuple(context or [])))
        return "번역 " + source


async def replay(monkeypatch, replacements):
    outputs = [QwenResult(segments=("A.", "B.", "C."), language="en"),
               QwenResult(replacement_segments=tuple(replacements), window_closed=True, language="en")]

    class Controller:
        def __init__(self, **kwargs):
            pass
        def next_snapshot(self):
            return None

        def accept(self, *args, **kwargs):
            return outputs.pop(0)

    async def drive(session, controller, next_snapshot, recognize, accept):
        for _ in range(2):
            await accept(SimpleNamespace(window_id=1), ("", "en"), .01)
            await session.texts.join()

    monkeypatch.setattr(stable_sessions, "QwenStreaming", Controller)
    monkeypatch.setattr(stable_sessions, "drive", drive)
    socket, runtime = Socket(), Runtime()
    session = StreamSession(socket, runtime, SessionManager(), SimpleNamespace(data={}), "local", "auto")
    session.active = True
    worker = asyncio.create_task(session._translate())
    try:
        await stable_sessions.qwen(session)
    finally:
        session.active = False
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    return session, socket, runtime


@pytest.mark.asyncio
async def test_session_leading_insertion_uses_new_caption_id_and_correct_context_order(monkeypatch):
    session, socket, runtime = await replay(monkeypatch, ("X.", "A.", "B.", "C."))
    captions = [event for event in socket.events if event["type"] == "caption"]
    assert [(event["segment_id"], event["source_text"]) for event in captions] == [
        (1, "A."), (2, "B."), (3, "C."), (4, "X.")]
    assert all(event["is_final"] for event in captions)
    assert session._sentence_order == {1: (1, 1), 2: (1, 2), 3: (1, 3), 4: (1, 0)}
    assert runtime.calls[-1] == ("X.", "en", ())
    assert not any(event["type"] == "caption_remove" for event in socket.events)


@pytest.mark.asyncio
async def test_session_deletion_retracts_only_old_id_and_append_gets_new_id(monkeypatch):
    session, socket, runtime = await replay(monkeypatch, ("B.", "C.", "D."))
    removals = [event["segment_id"] for event in socket.events if event["type"] == "caption_remove"]
    assert removals == [1]
    assert runtime.calls[-1] == ("D.", "en", ("B.", "C."))
    assert session._sentence_order[2] == (1, 0) and session._sentence_order[4] == (1, 2)
    assert 1 not in session._context and 1 in session._removed_segments


@pytest.mark.asyncio
async def test_session_replacement_reuses_only_changed_id_and_cleans_old_context(monkeypatch):
    session, socket, runtime = await replay(monkeypatch, ("A.", "Correct.", "C."))
    captions = [event for event in socket.events if event["type"] == "caption"]
    assert captions[-1]["segment_id"] == 2 and captions[-1]["revision"] == 2
    assert runtime.calls[-1] == ("Correct.", "en", ("A.",))
    assert "B." not in session._context.values()


@pytest.mark.asyncio
async def test_session_empty_final_retracts_all_without_translation(monkeypatch):
    session, socket, runtime = await replay(monkeypatch, ())
    assert len(runtime.calls) == 3
    assert [event["segment_id"] for event in socket.events if event["type"] == "caption_remove"] == [1, 2, 3]
    assert session._context == {}
