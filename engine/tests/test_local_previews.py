"""Local provisional translation lifecycle; fake ASR/LLM, real queue and log."""
import asyncio
from collections import deque
from copy import deepcopy
import struct
from types import SimpleNamespace

import pytest

from engine.history import CaptionHistory
from engine.local_captions import LocalCaptions
from engine.local_streaming import ASRSnapshot, ASRWord, StreamingResult
from engine.sessions import SessionManager, StreamSession
from engine.settings import EngineError


class Client:
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
                await self.changed.wait_for(lambda: any(predicate(event) for event in self.events))
            return next(event for event in self.events if predicate(event))
        return await asyncio.wait_for(wait(), 3)

    def of_type(self, kind):
        return [event for event in self.events if event["type"] == kind]


class FakeRuntime:
    asr_backend = "whisper"
    translation_warnings = []

    def __init__(self, outcomes=(), *, block_first=False):
        self.outcomes = deque(outcomes)
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not block_first:
            self.release.set()

    async def prepare(self, settings):
        pass

    async def translate(self, source, language, context, *, target_language="ko"):
        first = not self.calls
        self.calls.append((source, language, list(context)))
        if first:
            self.started.set()
            await self.release.wait()
        outcome = self.outcomes.popleft() if self.outcomes else "번역 " + source
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def make_session(runtime, history=None, *, worker=True):
    client = Client()
    session = StreamSession(client, runtime, SessionManager(history), SimpleNamespace(data={}), "local", "zh")
    await session.manager.claim(session)
    session.active = True
    if worker:
        start_worker(session)
    return session, client


def start_worker(session):
    session.tasks.append(asyncio.create_task(session._guard(session._translate())))


async def queue(session, segment, source, final):
    session.segment = max(session.segment, segment)
    session._sentence_order.setdefault(segment, (segment, 0))
    return await session._queue_translation(segment, source, "zh", final)


async def settled(session):
    await asyncio.wait_for(session.texts.join(), 3)
    await session.manager.flush_history()


async def close(session):
    await session.stop(notify=False)
    await session.manager.flush_history()


def snapshot(sequence, seconds, *, window=1, offset=0, final=False):
    return ASRSnapshot(window, sequence, b"\0\0" * round(seconds * 16000), offset, final,
                       round(seconds * 50))


@pytest.mark.asyncio
async def test_pending_correction_replaces_only_its_own_job_and_keeps_other_finals():
    runtime = FakeRuntime()
    session, client = await make_session(runtime, worker=False)
    try:
        await queue(session, 1, "旧的识别", False)
        await queue(session, 2, "第二句。", True)
        await queue(session, 3, "第三句。", True)
        await queue(session, 1, "修正的识别", True)
        assert session.texts.qsize() == 3
        start_worker(session)
        await settled(session)
        assert [call[0] for call in runtime.calls] == ["第二句。", "第三句。", "修正的识别"]
        captions = client.of_type("caption")
        assert {event["segment_id"] for event in captions} == {1, 2, 3}
        assert next(event for event in captions if event["segment_id"] == 1)["revision"] == 2
        assert all(event["source_text"] != "旧的识别" for event in captions)
    finally:
        await close(session)


@pytest.mark.asyncio
async def test_inflight_old_preview_cannot_overwrite_corrected_final_or_create_second_log_row(tmp_path):
    history = CaptionHistory(tmp_path)
    runtime = FakeRuntime(["오래된 번역", "수정된 번역"], block_first=True)
    session, client = await make_session(runtime, history)
    try:
        await queue(session, 1, "旧的识别", False)
        await asyncio.wait_for(runtime.started.wait(), 3)
        await queue(session, 1, "中间识别", False)
        await queue(session, 1, "最后的识别。", True)
        runtime.release.set()
        await settled(session)
        captions = client.of_type("caption")
        assert [(event["source_text"], event["is_final"], event["revision"]) for event in captions] == [
            ("最后的识别。", True, 3)]
        assert [call[0] for call in runtime.calls] == ["旧的识别", "最后的识别。"]
        rows = history.snapshot()
        assert len(rows) == 1 and rows[0]["segment_id"] == 1
        assert rows[0]["text"] == "수정된 번역" and rows[0]["is_final"]
    finally:
        runtime.release.set()
        await close(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_during_inference", [False, True])
async def test_identical_final_upgrades_existing_translation_without_an_extra_model_call(tmp_path, finish_during_inference):
    history = CaptionHistory(tmp_path)
    runtime = FakeRuntime(["같은 구절의 번역"], block_first=finish_during_inference)
    session, client = await make_session(runtime, history)
    try:
        await queue(session, 1, "相同的原文。", False)
        await asyncio.wait_for(runtime.started.wait(), 3)
        if not finish_during_inference:
            await settled(session)
            assert client.of_type("caption")[-1]["is_final"] is False
            original_timestamp = history.snapshot()[0]["timestamp"]
        await queue(session, 1, "相同的原文。", True)
        runtime.release.set()
        await settled(session)
        assert len(runtime.calls) == 1
        assert client.of_type("caption")[-1]["is_final"] is True
        assert {event["segment_id"] for event in client.of_type("caption")} == {1}
        assert {event["revision"] for event in client.of_type("caption")} == {1}
        rows = history.snapshot()
        assert len(rows) == 1 and rows[0]["is_final"]
        if not finish_during_inference:
            assert rows[0]["timestamp"] == original_timestamp
    finally:
        runtime.release.set()
        await close(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["queued", "inflight", "completed"])
async def test_final_id_never_downgrades_when_a_late_changed_preview_arrives(when):
    runtime = FakeRuntime(block_first=when == "inflight")
    session, client = await make_session(runtime, worker=when != "queued")
    try:
        await queue(session, 1, "已经确定的原文。", True)
        if when == "inflight":
            await asyncio.wait_for(runtime.started.wait(), 3)
        elif when == "completed":
            await settled(session)
        before = deepcopy(client.events)
        await queue(session, 1, "迟到的不稳定原文", False)
        assert client.events == before  # Not even a downgraded transcript is sent.
        if when == "queued":
            start_worker(session)
        runtime.release.set()
        await settled(session)
        assert [call[0] for call in runtime.calls] == ["已经确定的原文。"]
        assert all(event["is_final"] for event in client.of_type("caption"))
    finally:
        runtime.release.set()
        await close(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("final_during_failure", [False, True])
async def test_failed_local_preview_gets_final_retry_on_same_caption_id(tmp_path, final_during_failure):
    runtime = FakeRuntime([EngineError("translation_language", "합성 번역 실패", 503), "최종 한국어"],
                          block_first=final_during_failure)
    history = CaptionHistory(tmp_path)
    session, client = await make_session(runtime, history)
    try:
        await queue(session, 1, "相同原文", False)
        await asyncio.wait_for(runtime.started.wait(), 3)
        if not final_during_failure:
            await settled(session)
            assert session._translations[1].failed and session._translations[1].result is None
            assert not client.of_type("caption") and not client.of_type("warning")
            assert history.snapshot() == []
        await queue(session, 1, "相同原文", True)
        runtime.release.set()
        await settled(session)
        assert len(runtime.calls) == 2
        last = client.of_type("caption")[-1]
        assert (last["segment_id"], last["is_final"], last["translation_status"], last["text"]) == (1, True, "ok", "최종 한국어")
        assert len(history.snapshot()) == 1 and history.snapshot()[0]["status"] == "ok"
        assert session.active and not client.of_type("error")
    finally:
        runtime.release.set()
        await close(session)


@pytest.mark.asyncio
async def test_same_id_corrections_rotate_as_one_row_per_phrase_and_keep_latest_100(tmp_path):
    history = CaptionHistory(tmp_path)
    runtime = FakeRuntime()
    session, client = await make_session(runtime, history)
    try:
        for segment in range(1, 106):
            admitted = await queue(session, segment, f"初步短句 {segment}", False)
            assert admitted is not False, (segment, "preview unexpectedly deferred", session._translations)
            await settled(session)
            preview = history.snapshot()[0]
            assert (preview["segment_id"], preview["is_final"], preview["revision"]) == (segment, False, 1)
            await queue(session, segment, f"确定短句 {segment}。", True)
            await settled(session)
            assert not client.of_type("warning"), client.of_type("warning")
            final = history.snapshot()[0]
            assert (final["segment_id"], final["is_final"], final["revision"]) == (segment, True, 2), (
                segment, final, session._translations, session.texts.qsize(), session.texts._unfinished_tasks)
        rows = history.snapshot()
        assert len(rows) == 100
        assert [row["segment_id"] for row in rows] == list(range(105, 5, -1))
        assert len({(row["session_id"], row["segment_id"]) for row in rows}) == 100
        assert all(row["is_final"] and row["revision"] == 2 for row in rows)
        assert all(row["source_text"].startswith("确定短句") for row in rows)
        assert len(client.of_type("caption")) == 210
    finally:
        await close(session)


@pytest.mark.asyncio
async def test_actual_local_adapter_promotes_short_preview_to_complete_final_and_keeps_spoken_repetition(tmp_path):
    runtime = FakeRuntime()
    history = CaptionHistory(tmp_path)
    session, client = await make_session(runtime, history)
    session.language = "auto"
    captions = LocalCaptions()
    long_source = "这是最终完整的第一句原文" * 12 + "。"
    try:
        updates = captions.accept(snapshot(1, 2), StreamingResult((), "", long_source))
        assert len(updates) == 1 and len(updates[0].text) <= 96 and not updates[0].is_final
        await session._apply_sentences(updates, language="ja")
        await settled(session)
        provisional = client.of_type("caption")[-1]
        assert provisional["language"] == "ja" and not provisional["is_final"]
        updates = captions.accept(snapshot(2, 3, final=True),
                                  StreamingResult((long_source, "重复。", "重复。"), long_source, ""))
        await session._apply_sentences(updates, language="ja")
        await settled(session)
        final = [event for event in client.of_type("caption") if event["is_final"]]
        assert [event["source_text"] for event in final] == [long_source, "重复。", "重复。"]
        assert final[0]["segment_id"] == provisional["segment_id"]
        assert len({event["segment_id"] for event in final}) == 3
        assert all(event["language"] == "ja" for event in final)
        rows = history.snapshot()
        assert len(rows) == 3 and all(row["is_final"] for row in rows)
        assert rows[-1]["source_text"] == long_source
        # Empty/window transitions may retract a preview but never this final.
        updates = captions.accept(snapshot(3, 1.3, window=2, offset=3), StreamingResult((), "", ""))
        await session._apply_sentences(updates, language="ja")
        await settled(session)
        assert not client.of_type("caption_remove") and len(history.snapshot()) == 3
    finally:
        await close(session)


@pytest.mark.asyncio
async def test_adapter_empty_final_retracts_only_inflight_preview_and_next_window_gets_new_id(tmp_path):
    runtime = FakeRuntime(block_first=True)
    history = CaptionHistory(tmp_path)
    session, client = await make_session(runtime, history)
    captions = LocalCaptions()
    try:
        updates = captions.accept(snapshot(1, 2), StreamingResult((), "", "未确定的原文。"))
        old_id = updates[0].segment_id
        await session._apply_sentences(updates, language="zh")
        await asyncio.wait_for(runtime.started.wait(), 3)
        updates = captions.accept(snapshot(2, 2.5, final=True), StreamingResult((), "", ""))
        await session._apply_sentences(updates, language="zh")
        assert [event["segment_id"] for event in client.of_type("caption_remove")] == [old_id]
        updates = captions.accept(snapshot(3, .6, window=2, offset=2.5, final=True),
                                  StreamingResult(("下一句。",), "下一句。", ""))
        await session._apply_sentences(updates, language="zh")
        runtime.release.set()
        await settled(session)
        emitted = client.of_type("caption")
        assert len(emitted) == 1 and emitted[0]["source_text"] == "下一句。"
        assert emitted[0]["segment_id"] != old_id and emitted[0]["is_final"]
        rows = history.snapshot()
        assert len(rows) == 1 and rows[0]["segment_id"] != old_id
    finally:
        runtime.release.set()
        await close(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting_finals", [1, 6])
async def test_backlog_skips_only_preview_correction_and_preserves_inflight_work_and_all_finals(waiting_finals):
    runtime = FakeRuntime(block_first=True)
    session, client = await make_session(runtime)
    final_task = None
    try:
        await queue(session, 1, "已经开始的暂定原文", False)
        await asyncio.wait_for(runtime.started.wait(), 3)
        existing = session._translations[1]
        for segment in range(2, waiting_finals + 2):
            await queue(session, segment, f"等待的确定句 {segment}。", True)
        await asyncio.wait_for(queue(session, 1, "繁忙时的新暂定原文", False), 1)
        assert session._translations[1] is existing
        assert session.texts.qsize() == waiting_finals
        assert client.of_type("transcript")[-1]["text"] == "繁忙时的新暂定原文"
        final_task = asyncio.create_task(queue(session, 1, "第一句的最终原文。", True))
        await asyncio.sleep(0)
        runtime.release.set()
        await asyncio.wait_for(final_task, 3)
        await settled(session)
        sources = [call[0] for call in runtime.calls]
        assert "繁忙时的新暂定原文" not in sources
        assert "第一句的最终原文。" in sources
        emitted = client.of_type("caption")
        finals = [event for event in emitted if event["is_final"]]
        assert {event["segment_id"] for event in finals} == set(range(1, waiting_finals + 2))
        assert all(event["source_text"] != "繁忙时的新暂定原文" for event in emitted)
        assert session.active and not client.of_type("error")
    finally:
        runtime.release.set()
        if final_task is not None and not final_task.done():
            final_task.cancel()
            await asyncio.gather(final_task, return_exceptions=True)
        await close(session)


@pytest.mark.asyncio
async def test_adapter_preview_skipped_for_backlog_is_still_emitted_on_final_with_its_original_id(tmp_path):
    history = CaptionHistory(tmp_path)
    runtime = FakeRuntime()
    session, client = await make_session(runtime, history, worker=False)
    captions = LocalCaptions(start_id=2)
    try:
        await queue(session, 1, "先前确定的句子。", True)
        preview = captions.accept(snapshot(1, 2), StreamingResult((), "", "暂时跳过的预览"))
        preview_id = preview[0].segment_id
        await session._apply_sentences(preview, language="zh")
        assert session.texts.qsize() == 1 and preview_id not in session._translations
        final = captions.accept(snapshot(2, 2.5, final=True),
                                StreamingResult(("实际确定的句子。",), "实际确定的句子。", ""))
        assert final[0].segment_id == preview_id
        await session._apply_sentences(final, language="zh")
        start_worker(session)
        await settled(session)
        assert [call[0] for call in runtime.calls] == ["先前确定的句子。", "实际确定的句子。"]
        assert {event["segment_id"] for event in client.of_type("caption")} == {1, preview_id}
        assert len(history.snapshot()) == 2 and all(row["is_final"] for row in history.snapshot())
    finally:
        await close(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("selected,detected,expected", [("auto", "ja", "ja"), ("zh", "ja", "zh"), ("en", "zh", "en")])
async def test_real_local_whisper_loop_translates_preview_then_reuses_same_final_with_correct_language(tmp_path, selected, detected, expected):
    loop = asyncio.get_running_loop()

    class StreamingRuntime(FakeRuntime):
        def __init__(self):
            super().__init__(["확정 전부터 보이는 한국어"])
            self.asr_calls = []
            self.observed = asyncio.Queue()
            self.hypotheses = deque([
                [(0.1, 0.7, "最初的不稳定识别")],
                [(0.1, 0.7, "修正后的完整原文。")],
                [(0.1, 0.7, "修正后的完整原文。")],
            ])

        def transcribe_stream(self, audio, language, prompt):
            self.asr_calls.append((len(audio), language, prompt))
            words = [ASRWord(*word) for word in self.hypotheses.popleft()]
            loop.call_soon_threadsafe(self.observed.put_nowait, len(self.asr_calls))
            return words, detected

    runtime = StreamingRuntime()
    client = Client()
    history = CaptionHistory(tmp_path)
    session = StreamSession(client, runtime, SessionManager(history), SimpleNamespace(data={}), "local", selected)
    await session.start()
    try:
        voice = struct.pack("<h", 1000) * 16000
        await session.feed(voice)
        assert await asyncio.wait_for(runtime.observed.get(), 3) == 1
        assert runtime.calls == []  # The initial sub-1.2-second hypothesis waits.
        await session.feed(voice)
        provisional = await client.wait_for(lambda event: event["type"] == "caption" and not event["is_final"])
        assert provisional["source_text"] == "修正后的完整原文。" and provisional["language"] == expected
        await session.feed(b"\0\0" * 8000)
        final = await client.wait_for(lambda event: event["type"] == "caption" and event["is_final"])
        async with session.send_lock:
            pass  # Ensure the observed socket event has also reached history.
        await settled(session)
        assert (final["segment_id"], final["revision"]) == (provisional["segment_id"], provisional["revision"])
        assert runtime.calls == [("修正后的完整原文。", expected, [])]
        assert len(runtime.asr_calls) == 3 and all(call[1] == selected for call in runtime.asr_calls)
        rows = history.snapshot()
        assert len(rows) == 1 and rows[0]["is_final"] and rows[0]["language"] == expected
        assert session.active and not client.of_type("error")
    finally:
        await close(session)


@pytest.mark.asyncio
async def test_deferred_unchanged_preview_is_retried_after_final_backlog_clears(tmp_path):
    runtime = FakeRuntime()
    history = CaptionHistory(tmp_path)
    session, client = await make_session(runtime, history, worker=False)
    captions = LocalCaptions(start_id=2)
    source = "等待队列清空的同一原文"
    try:
        await queue(session, 1, "先处理的确定句。", True)
        updates = captions.accept(snapshot(1, 2), StreamingResult((), "", source))
        original_id = updates[0].segment_id
        deferred = await session._apply_sentences(updates, language="zh")
        assert len(deferred) == 1 and deferred[0].segment_id == original_id
        for update in deferred:
            captions.defer_preview(update.segment_id, update.text)
        assert original_id not in session._translations and session.texts.qsize() == 1
        start_worker(session)
        await settled(session)

        retry = captions.accept(snapshot(2, 3), StreamingResult((), "", source))
        assert len(retry) == 1 and retry[0].segment_id == original_id and not retry[0].is_final
        assert await session._apply_sentences(retry, language="zh") == []
        await settled(session)
        provisional = client.of_type("caption")[-1]
        assert provisional["segment_id"] == original_id and not provisional["is_final"]
        assert provisional["source_text"] == source

        final = captions.accept(snapshot(3, 3.5, final=True), StreamingResult((source,), source, ""))
        await session._apply_sentences(final, language="zh")
        await settled(session)
        assert [call[0] for call in runtime.calls] == ["先处理的确定句。", source]
        assert client.of_type("caption")[-1]["segment_id"] == original_id
        assert client.of_type("caption")[-1]["is_final"]
        rows = history.snapshot()
        assert len(rows) == 2 and all(row["is_final"] for row in rows)
        assert next(row for row in rows if row["segment_id"] == original_id)["revision"] == 1
    finally:
        await close(session)


@pytest.mark.asyncio
async def test_unstable_short_prefix_never_reaches_translator_before_complete_phrase(tmp_path):
    runtime = FakeRuntime()
    history = CaptionHistory(tmp_path)
    session, client = await make_session(runtime, history)
    captions = LocalCaptions()
    source = "她是我最好的朋友。"
    try:
        early = captions.accept(snapshot(1, 2), StreamingResult((), "", "她是我"))
        assert early == []
        await session._apply_sentences(early, language="zh")
        await settled(session)
        assert runtime.calls == [] and not client.of_type("caption") and history.snapshot() == []

        complete = captions.accept(snapshot(2, 3), StreamingResult((), "", source))
        await session._apply_sentences(complete, language="zh")
        await settled(session)
        preview = client.of_type("caption")[-1]
        assert not preview["is_final"] and preview["source_text"] == source
        final = captions.accept(snapshot(3, 3.1, final=True), StreamingResult((source,), source, ""))
        await session._apply_sentences(final, language="zh")
        await settled(session)
        assert [call[0] for call in runtime.calls] == [source]
        assert client.of_type("caption")[-1]["is_final"]
        assert {event["segment_id"] for event in client.of_type("caption")} == {preview["segment_id"]}
        assert len(history.snapshot()) == 1 and history.snapshot()[0]["is_final"]
        assert session.active and not client.of_type("error") and not client.of_type("warning")
    finally:
        await close(session)


@pytest.mark.asyncio
async def test_short_final_can_preempt_stability_wait_without_an_extra_translation(tmp_path):
    runtime = FakeRuntime()
    history = CaptionHistory(tmp_path)
    session, client = await make_session(runtime, history)
    session.language = "auto"
    captions = LocalCaptions()
    try:
        assert captions.accept(snapshot(1, 2), StreamingResult((), "", "はい")) == []
        final = captions.accept(snapshot(2, 2.1, final=True), StreamingResult(("はい",), "はい", ""))
        assert len(final) == 1 and final[0].is_final
        await session._apply_sentences(final, language="ja")
        await settled(session)
        assert runtime.calls == [("はい", "ja", [])]
        emitted = client.of_type("caption")
        assert len(emitted) == 1 and emitted[0]["is_final"] and emitted[0]["language"] == "ja"
        assert len(history.snapshot()) == 1 and history.snapshot()[0]["is_final"]
        assert session.active and not client.of_type("error") and not client.of_type("warning")
    finally:
        await close(session)
