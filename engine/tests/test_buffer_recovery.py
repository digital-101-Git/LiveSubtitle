"""Bounded overload recovery without real models, audio devices or network."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from engine.audio import AudioQueue
from engine.history import CaptionHistory
from engine.sessions import SessionManager, StreamSession
from engine.settings import EngineError
from engine.streaming_sentences import SentenceUpdate


class Socket:
    def __init__(self):
        self.events = []

    async def send_json(self, event):
        self.events.append(dict(event))


class Runtime:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    async def translate(self, text, language, context=None, *, target_language="ko"):
        self.calls.append((text, list(context or [])))
        self.started.set()
        await self.release.wait()
        return "번역 " + text


def make_session(runtime=None, manager=None):
    runtime = runtime or Runtime()
    socket = Socket()
    session = StreamSession(socket, runtime, manager or SessionManager(),
                            SimpleNamespace(data={}), "local", "ja")
    session.active = True
    return session, runtime, socket


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(wait(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("byte_limit,item_limit", [(8, 10), (100, 2)])
async def test_audio_queue_only_evicts_oldest_and_retains_order(byte_limit, item_limit):
    queue = AudioQueue(byte_limit, item_limit)
    queue.put_latest(b"1111")
    queue.put_latest(b"2222")
    assert queue.put_latest(b"3333") == 4
    assert queue.dropped_bytes == 4 and queue.generation == 1
    assert await queue.get() == b"2222"
    assert await queue.get() == b"3333"
    assert queue.bytes == 0
    await asyncio.wait_for(queue.queue.join(), 1)


@pytest.mark.asyncio
async def test_second_gap_discards_deferred_packet_instead_of_splicing_audio():
    session, _, _ = make_session()
    session.audio = AudioQueue(8, 2)
    await session.feed(b"1111")
    await session.feed(b"2222")
    await session.feed(b"3333")
    with pytest.raises(EngineError, match="음성 입력"):
        await session._next_local_audio()
    assert session._deferred_audio == b"2222"
    await session.feed(b"4444")
    await session.feed(b"5555")
    assert session._deferred_audio is None
    session._asr_generation = session.audio_generation
    assert await session._next_local_audio() == b"4444"
    assert await session._next_local_audio() == b"5555"
    assert session.audio.dropped_bytes == 12  # packets 1, 2 and 3


@pytest.mark.asyncio
async def test_client_gap_discards_server_pending_audio_and_counts_both():
    session, _, _ = make_session()
    await session.feed(b"1111")
    session.defer_audio(b"2222")
    session.input_gap(100)
    assert session.audio.bytes == 0 and session._deferred_audio is None
    assert session.audio_dropped_samples == 104
    await session.feed(b"3333")
    session._asr_generation = session.audio_generation
    assert await session._next_local_audio() == b"3333"


@pytest.mark.parametrize("value", [None, True, -1, 0, 1.5, "20", 57_600_001])
def test_client_gap_rejects_invalid_sample_counts(value):
    session, _, _ = make_session()
    with pytest.raises(EngineError) as error:
        session.input_gap(value)
    assert error.value.code == "invalid_audio_gap"
    assert session.audio_generation == 0


@pytest.mark.asyncio
async def test_full_translation_queue_retires_old_inflight_and_continues_latest():
    session, runtime, socket = make_session()
    worker = asyncio.create_task(session._translate())
    session.tasks.append(worker)
    try:
        await session._final("old.", "ja")
        await runtime.started.wait()
        for identifier in range(2, 9):
            await session._final(f"source{identifier}.", "ja")
        assert session.active and session.texts.qsize() == 6
        assert session._removed_segments == {1, 2}
        assert [e["segment_id"] for e in socket.events if e["type"] == "caption_remove"] == [2, 1]
        runtime.release.set()
        await asyncio.wait_for(session.texts.join(), 2)
        assert [e["segment_id"] for e in socket.events if e["type"] == "caption"] == list(range(3, 9))
        assert 1 not in session._context and 2 not in session._context
        await session._final("latest.", "ja")
        await asyncio.wait_for(session.texts.join(), 2)
        assert socket.events[-1]["source_text"] == "latest."
        assert not any(e["type"] in {"error", "stopped"} for e in socket.events)
    finally:
        runtime.release.set()
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_short_translation_burst_is_preserved_without_loss():
    session, runtime, socket = make_session()
    worker = asyncio.create_task(session._translate())
    session.tasks.append(worker)
    async def release_soon():
        await asyncio.sleep(.01)
        runtime.release.set()
    release = asyncio.create_task(release_soon())
    try:
        for identifier in range(12):
            await session._final(f"sentence{identifier}.", "ja")
        await asyncio.wait_for(session.texts.join(), 2)
        assert len([e for e in socket.events if e["type"] == "caption"]) == 12
        assert not session._removed_segments and not session._recovery_pending
    finally:
        await release
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_gap_context_cannot_be_refilled_by_an_older_inflight_translation():
    session, runtime, _ = make_session()
    worker = asyncio.create_task(session._translate())
    session.tasks.append(worker)
    try:
        await session._final("before gap.", "ja")
        await runtime.started.wait()
        session.break_audio_context()
        runtime.release.set()
        await asyncio.wait_for(session.texts.join(), 2)
        assert not session._context
        await session._final("after gap.", "ja")
        await asyncio.wait_for(session.texts.join(), 2)
        assert runtime.calls[-1] == ("after gap.", [])
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_older_final_translation_cannot_read_context_from_after_gap():
    session, runtime, _ = make_session()
    runtime.release.set()
    await session._final("before gap.", "ja")
    older = session._translations[1]
    session.break_audio_context()
    await session._final("after gap.", "ja")
    # A restarted streaming controller may assign a lower window-order value.
    session._sentence_order[2] = (0, 0)
    await session._translate_sentence(2, session._translations[2])
    assert session._context == {2: "after gap."}
    await session._translate_sentence(1, older)
    assert runtime.calls[-1] == ("before gap.", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["final", "updates", "queue"])
async def test_gap_during_transcript_send_does_not_relabel_old_work(monkeypatch, entry):
    session, runtime, socket = make_session()
    sending, resume = asyncio.Event(), asyncio.Event()
    original_send = socket.send_json
    async def blocked_send(event):
        if event["type"] == "transcript" and not sending.is_set():
            sending.set()
            await resume.wait()
        await original_send(event)
    monkeypatch.setattr(socket, "send_json", blocked_send)
    if entry == "final":
        publish = session._final("最初です。次の文です。", "ja")
    elif entry == "updates":
        publish = session._apply_sentences([
            SentenceUpdate(1, "最初です。", True), SentenceUpdate(2, "次の文です。", True)])
    else:
        session._sentence_order[1] = (1, 0)
        publish = session._queue_translation(1, "最初です。", "ja", True)
    task = asyncio.create_task(publish)
    try:
        await asyncio.wait_for(sending.wait(), 2)
        session.break_audio_context()
        resume.set()
        await task
        assert len(session._translations) == (1 if entry == "queue" else 2)
        assert {work.context_epoch for work in session._translations.values()} == {0}
        runtime.release.set()
        for identifier, work in list(session._translations.items()):
            await session._translate_sentence(identifier, work)
        assert not session._context
    finally:
        resume.set()
        await task


@pytest.mark.asyncio
async def test_adapter_restart_retires_preview_but_keeps_final_work(monkeypatch):
    session, _, socket = make_session()
    await session._apply_sentences([SentenceUpdate(1, "preview.", False)])
    await session._apply_sentences([SentenceUpdate(2, "final.", True)])
    calls = 0
    async def once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise EngineError("local_streaming_overrun", "mock overflow")
    monkeypatch.setattr(session, "_local_once", once)
    await session._local()
    assert calls == 2 and session.active
    assert 1 in session._removed_segments and 1 not in session._translations
    assert 2 in session._translations
    assert [e["segment_id"] for e in socket.events if e["type"] == "caption_remove"] == [1]


@pytest.mark.asyncio
async def test_recovery_log_waits_for_clear_barrier_and_contains_no_text(tmp_path):
    runtime = Runtime()
    runtime.root, runtime.log_lock = tmp_path, threading.RLock()
    manager = SessionManager(CaptionHistory(tmp_path))
    session, _, socket = make_session(runtime, manager)
    gate = asyncio.Event()
    manager._history_task = asyncio.create_task(gate.wait())
    clear = asyncio.create_task(manager.clear_logs(runtime.log_lock))
    await asyncio.sleep(0)
    session.report_overload("input", dropped_audio_seconds=.4, buffer_seconds=6)
    worker = asyncio.create_task(session._recovery_reports())
    session.tasks.append(worker)
    path = tmp_path / "logs/stream-recovery.jsonl"
    try:
        await asyncio.sleep(.02)
        assert not path.exists()
        gate.set()
        await clear
        await until(path.exists)
        row = json.loads(path.read_text(encoding="utf-8"))
        assert row["stage"] == "input" and row["dropped_audio_seconds"] == .4
        assert not ({"text", "source_text", "api_key", "error"} & row.keys())
        await until(lambda: any(e["type"] == "warning" for e in socket.events))
        assert session.active
    finally:
        gate.set()
        await clear
        await session.stop(notify=False)
