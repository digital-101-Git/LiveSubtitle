"""Recoverable sentence failures use fake translation and provider transports."""
import asyncio
import base64

import pytest

from engine.settings import EngineError
from engine.streaming_sentences import SentenceUpdate, StreamingSentences
from engine.tests.test_gemini import (
    TranslationRuntime,
    make_gemini_session,
    provider_final,
    stable_interim,
)


RECOVERABLE = ("translation_language", "translation_empty", "translation_truncated")


def failure(code="translation_language"):
    return EngineError(code, "모의 번역 오류입니다.", 503)


class ScriptedRuntime(TranslationRuntime):
    def __init__(self, outcomes, *, block_first=False, fail_on_cancel=False):
        super().__init__()
        self.outcomes = outcomes
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.fail_on_cancel = fail_on_cancel
        if not block_first:
            self.release.set()

    async def translate(self, text, language, context, *, target_language="ko"):
        index = len(self.translations)
        self.translations.append((text, language, list(context)))
        assert index < len(self.outcomes), "unexpected translation retry"
        if index == 0:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                if self.fail_on_cancel:
                    # Emulate a provider/inference completion racing with stop.
                    raise failure() from None
                raise
        outcome = self.outcomes[index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def quick_stability(monkeypatch):
    monkeypatch.setattr(StreamingSentences, "stability_seconds", .01)


async def wait_until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 2)


async def settled(session):
    await asyncio.wait_for(session.texts.join(), 2)


def events(client, kind):
    return [event for event in client.events if event["type"] == kind]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", RECOVERABLE)
async def test_sentence_quality_failure_preserves_source_and_continues_audio_and_next_sentence(tmp_path, monkeypatch, code):
    runtime = ScriptedRuntime([failure(code), "다음 문장을 계속 번역합니다."])
    session, provider, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        source = "不能正确翻译的句子。"
        provider_final(provider, source)
        warning = await client.wait_type("warning")
        failed = await client.wait_matching(lambda e: e["type"] == "caption" and e.get("translation_status") == "failed")
        assert warning["code"] == code and warning["segment_id"] == failed["segment_id"]
        assert failed["source_text"] == source and failed["text"] == "[번역하지 못했습니다]"
        assert failed["language"] == "zh" and failed["is_final"] is True
        assert failed["session_id"] == session.id and warning["session_id"] == session.id
        assert session.active and session.manager.active is session and not provider.closed

        pcm = b"\xe8\x03" * 1600
        await session.feed(pcm)
        assert base64.b64decode((await provider.wait_input("audio"))["data"]) == pcm
        next_source = "下一句仍然可以翻译。"
        provider_final(provider, next_source)
        caption = await client.wait_matching(lambda e: e["type"] == "caption" and e.get("source_text") == next_source)
        await settled(session)
        assert caption["translation_status"] == "ok" and caption["text"] == "다음 문장을 계속 번역합니다."
        assert caption["segment_id"] != failed["segment_id"] and caption["is_final"] is True
        assert [text for text, _, _ in runtime.translations] == [source, next_source]
        assert runtime.translations[1][2] == []  # A failed sentence is not context.
        assert session._translations == {} and provider.ends() == []
        assert not events(client, "error") and not events(client, "stopped")
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_succeeds", [True, False])
async def test_failed_provisional_retries_same_source_final_once(tmp_path, monkeypatch, quick_stability, retry_succeeds):
    runtime = ScriptedRuntime([failure(), "최종 번역입니다." if retry_succeeds else failure()])
    session, provider, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        source = "相同的原文。"
        await stable_interim(provider, client, source)
        await client.wait_type("warning")
        provisional = await client.wait_matching(lambda e: e["type"] == "caption" and e.get("translation_status") == "failed")
        assert provisional["is_final"] is False
        provider_final(provider, source)
        final = await client.wait_matching(lambda e: e["type"] == "caption" and e.get("is_final") is True)
        await settled(session)
        assert final["segment_id"] == provisional["segment_id"] and final["source_text"] == source
        assert final["revision"] > provisional["revision"]
        assert final["translation_status"] == ("ok" if retry_succeeds else "failed")
        assert final["text"] == ("최종 번역입니다." if retry_succeeds else "[번역하지 못했습니다]")
        assert [text for text, _, _ in runtime.translations] == [source, source]
        assert len(events(client, "warning")) == (1 if retry_succeeds else 2)
        assert session.active and not events(client, "error") and session._translations == {}
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_succeeds", [True, False])
async def test_final_arriving_during_failed_provisional_inference_gets_one_retry(tmp_path, monkeypatch, retry_succeeds):
    runtime = ScriptedRuntime([failure(), "확정된 번역입니다." if retry_succeeds else failure()], block_first=True)
    session, _, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        source = "正在翻译的原文。"
        await session._apply_sentences([SentenceUpdate(1, source, False)])
        await asyncio.wait_for(runtime.started.wait(), 2)
        await session._apply_sentences([SentenceUpdate(1, source, True)])
        assert session._translations[1].is_final
        runtime.release.set()
        caption = await client.wait_type("caption")
        await settled(session)
        assert caption["segment_id"] == 1 and caption["source_text"] == source and caption["is_final"] is True
        assert caption["translation_status"] == ("ok" if retry_succeeds else "failed")
        assert len(events(client, "caption")) == 1
        assert len(events(client, "warning")) == (0 if retry_succeeds else 1)
        assert [text for text, _, _ in runtime.translations] == [source, source]
        assert session.active and not events(client, "error")
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_failed_correction_replaces_previous_success_in_same_caption(tmp_path, monkeypatch, quick_stability):
    runtime = ScriptedRuntime(["이전 문장의 번역입니다.", failure(), "다음 번역입니다."])
    session, provider, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        await stable_interim(provider, client, "初步识别的句子。")
        provisional = await client.wait_type("caption")
        assert provisional["translation_status"] == "ok"
        corrected = "修正后的句子。"
        provider_final(provider, corrected)
        failed = await client.wait_matching(lambda e: e["type"] == "caption" and e.get("translation_status") == "failed")
        await settled(session)
        assert failed["segment_id"] == provisional["segment_id"]
        assert failed["revision"] > provisional["revision"] and failed["source_text"] == corrected
        assert failed["text"] == "[번역하지 못했습니다]" and failed["is_final"] is True
        latest = {event["segment_id"]: event for event in events(client, "caption")}
        assert latest[provisional["segment_id"]] == failed
        provider_final(provider, "下一句。")
        await client.wait_matching(lambda e: e["type"] == "caption" and e.get("source_text") == "下一句。")
        assert runtime.translations[-1][2] == [] and session.active
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("retract", [False, True])
async def test_obsolete_inflight_failure_cannot_publish_caption_or_warning(tmp_path, monkeypatch, retract):
    runtime = ScriptedRuntime([failure(), "최신 원문의 번역입니다."], block_first=True)
    session, _, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        await session._apply_sentences([SentenceUpdate(1, "过时的原文。", False)])
        await asyncio.wait_for(runtime.started.wait(), 2)
        segment = 2 if retract else 1
        if retract:
            await session._apply_sentences([SentenceUpdate(1, "过时的原文。", True, removed=True)])
        await session._apply_sentences([SentenceUpdate(segment, "最新的原文。", True)])
        runtime.release.set()
        caption = await client.wait_type("caption")
        await settled(session)
        assert caption["source_text"] == "最新的原文。" and caption["segment_id"] == segment
        assert caption["translation_status"] == "ok" and caption["is_final"] is True
        assert len(events(client, "caption")) == 1 and not events(client, "warning")
        assert len(events(client, "caption_remove")) == int(retract)
        assert session.active and not events(client, "error")
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_final_waiting_on_failed_caption_send_retries_and_suppresses_stale_warning(tmp_path, monkeypatch):
    runtime = ScriptedRuntime([failure(), "최종 번역입니다."], block_first=True)
    session, _, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    lock_held = False
    final_task = None
    try:
        source = "相同的原文。"
        await session._apply_sentences([SentenceUpdate(1, source, False)])
        await asyncio.wait_for(runtime.started.wait(), 2)
        await session.send_lock.acquire()
        lock_held = True
        runtime.release.set()
        await wait_until(lambda: session._translations[1].failed)
        final_task = asyncio.create_task(session._apply_sentences([SentenceUpdate(1, source, True)]))
        await asyncio.sleep(0)  # Place the final behind the failed caption send.
        assert not final_task.done()
        session.send_lock.release()
        lock_held = False
        await asyncio.wait_for(final_task, 2)
        final = await client.wait_matching(lambda e: e["type"] == "caption" and e.get("is_final") is True)
        await settled(session)
        assert final["translation_status"] == "ok" and final["revision"] == 2
        assert final["segment_id"] == 1 and final["source_text"] == source
        assert len(runtime.translations) == 2 and not events(client, "warning")
        assert session.active
    finally:
        if lock_held:
            session.send_lock.release()
        if final_task is not None and not final_task.done():
            final_task.cancel()
            await asyncio.gather(final_task, return_exceptions=True)
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_stop_suppresses_a_failure_returned_during_cancellation(tmp_path, monkeypatch):
    runtime = ScriptedRuntime([failure()], block_first=True, fail_on_cancel=True)
    session, provider, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        provider_final(provider, "停止时完成的原文。")
        await asyncio.wait_for(runtime.started.wait(), 2)
        await asyncio.wait_for(session.stop(), 2)
        assert runtime.cancelled.is_set() and all(task.done() for task in session.tasks)
        assert provider.closed and provider.active_readers == 0 and session.manager.active is None
        assert len(events(client, "stopped")) == 1
        assert not events(client, "caption") and not events(client, "warning") and not events(client, "error")
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["translation_failed", "translation_not_ready", "unknown_engine_error"])
async def test_transport_model_and_unclassified_errors_remain_fatal(tmp_path, monkeypatch, code):
    runtime = ScriptedRuntime([failure(code)])
    session, provider, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        provider_final(provider, "无法连接到翻译引擎。")
        error = await client.wait_type("error")
        await client.wait_type("stopped")
        assert error["code"] == code and not session.active and session.manager.active is None
        assert provider.closed and len(runtime.translations) == 1
        assert not events(client, "caption") and not events(client, "warning")
    finally:
        await session.stop(notify=False)
