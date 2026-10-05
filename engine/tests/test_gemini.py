"""Provider protocol tests: a fake WebSocket replaces every external connection."""
import asyncio
import base64
import json

import pytest

from engine.streaming_sentences import SentenceUpdate, StreamingSentences
from engine.sessions import SessionManager, StreamSession
from engine.settings import EngineError, Settings


class ProviderSocket:
    def __init__(self, setup_response=None):
        self.setup_response = {"setupComplete": {}} if setup_response is None else setup_response
        self.received = asyncio.Queue()
        self.sent = []
        self.sent_events = asyncio.Queue()
        self.audio_sent = asyncio.Event()
        self.closed = False
        self.active_readers = 0
        self.read_waiting = asyncio.Event()
        self.reader_cancelled = asyncio.Event()

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        self.sent_events.put_nowait(message)
        if "audio" in message.get("realtimeInput", {}):
            self.audio_sent.set()

    async def wait_input(self, key):
        while True:
            message = await asyncio.wait_for(self.sent_events.get(), 2)
            if key in message.get("realtimeInput", {}):
                return message["realtimeInput"][key]

    def ends(self):
        return [message for message in self.sent if message.get("realtimeInput", {}).get("audioStreamEnd")]

    async def recv(self):
        return json.dumps(self.setup_response)

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.active_readers += 1
        self.read_waiting.set()
        try:
            message = await self.received.get()
        except asyncio.CancelledError:
            self.reader_cancelled.set()
            raise
        finally:
            self.active_readers -= 1
            self.read_waiting.clear()
        if message is None:
            raise StopAsyncIteration
        return json.dumps(message)

    async def close(self):
        self.closed = True


class ClientSocket:
    def __init__(self):
        self.events = []
        self.pending = asyncio.Queue()

    async def send_json(self, event):
        self.events.append(event)
        self.pending.put_nowait(event)

    async def wait_type(self, kind):
        while True:
            event = await asyncio.wait_for(self.pending.get(), 2)
            if event["type"] == kind:
                return event

    async def wait_matching(self, predicate, after=0):
        while True:
            for event in self.events[after:]:
                if predicate(event):
                    return event
            await asyncio.wait_for(self.pending.get(), 2)


class TranslationRuntime:
    def __init__(self):
        self.settings = None
        self.translations = []

    async def prepare(self, settings):
        self.settings = settings

    async def translate(self, text, language, context, *, target_language="ko"):
        self.translations.append((text, language, context))
        return "경기는 5분 후에 시작합니다."


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal,code", [
    ({"goAway": {"timeLeft": "5s"}}, "gemini_session_expiring"),
    (None, "gemini_disconnected"),
    ({"error": {"message": "private-test-key"}}, "gemini_provider_error"),
])
async def test_gemini_setup_pcm_interim_final_and_terminal(tmp_path, monkeypatch, terminal, code):
    provider = ProviderSocket()
    connections = []

    async def fake_connect(uri, **kwargs):
        connections.append((uri, kwargs))
        return provider

    # Patching the sole transport factory guarantees no network is contacted.
    monkeypatch.setattr("websockets.asyncio.client.connect", fake_connect)
    settings = Settings(tmp_path)
    settings.update({"gemini_api_key": "private-test-key"})
    client, runtime, manager = ClientSocket(), TranslationRuntime(), SessionManager()
    session = StreamSession(client, runtime, manager, settings, "gemini", "zh")
    try:
        await session.start()
        assert runtime.settings["mode"] == "gemini"
        assert len(connections) == 1
        assert connections[0][0].startswith("wss://generativelanguage.googleapis.com/ws/")
        assert connections[0][1]["proxy"] is None
        assert provider.sent[0] == {"setup": {
            "model": "models/gemini-3.5-transcribe-live",
            "generationConfig": {"responseModalities": ["TEXT"]},
            "realtimeInputConfig": {"automaticActivityDetection": {"silenceDurationMs": 500}},
            "inputAudioTranscription": {"languageCodes": ["cmn-Hans-CN"]},
        }}
        ready = await client.wait_type("ready")
        assert ready["session_id"] == session.id

        pcm = b"\x01\x00" * 1600
        await session.feed(pcm)
        await asyncio.wait_for(provider.audio_sent.wait(), 2)
        audio = provider.sent[1]["realtimeInput"]["audio"]
        assert audio["mimeType"] == "audio/pcm;rate=16000"
        assert base64.b64decode(audio["data"]) == pcm

        provider.received.put_nowait({"serverContent": {"interimInputTranscription": {"text": "比赛将在"}}})
        interim = await client.wait_type("transcript")
        assert interim["text"] == "比赛将在" and interim["is_final"] is False
        assert runtime.translations == []
        provider.received.put_nowait({"serverContent": {"inputTranscription": {"text": "比赛将在五分钟后开始。"}}})
        final = await client.wait_type("transcript")
        assert final["is_final"] is True
        caption = await client.wait_type("caption")
        assert caption["segment_id"] == final["segment_id"]
        assert caption["is_final"] is True
        assert caption["source_text"] == "比赛将在五分钟后开始。"
        assert caption["text"] == "경기는 5분 후에 시작합니다."
        assert runtime.translations == [("比赛将在五分钟后开始。", "zh", [])]

        provider.received.put_nowait(terminal)
        error = await client.wait_type("error")
        assert error["code"] == code
        await client.wait_type("stopped")
        assert provider.closed and not session.active and manager.active is None
        assert "private-test-key" not in json.dumps(client.events)
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_gemini_setup_rejection_closes_transport_without_exposing_key(tmp_path, monkeypatch):
    provider = ProviderSocket({"error": {"message": "private-test-key"}})

    async def fake_connect(uri, **kwargs):
        return provider

    monkeypatch.setattr("websockets.asyncio.client.connect", fake_connect)
    settings = Settings(tmp_path)
    settings.update({"gemini_api_key": "private-test-key"})
    client, manager = ClientSocket(), SessionManager()
    session = StreamSession(client, TranslationRuntime(), manager, settings, "gemini", "auto")
    with pytest.raises(EngineError) as error:
        await session.start()
    assert error.value.code == "gemini_connect_failed"
    assert "private-test-key" not in error.value.message
    assert provider.closed and manager.active is None
    assert not any(event["type"] == "ready" for event in client.events)


async def make_gemini_session(tmp_path, monkeypatch, runtime=None, language="zh"):
    provider = ProviderSocket()

    async def fake_connect(uri, **kwargs):
        return provider

    monkeypatch.setattr("websockets.asyncio.client.connect", fake_connect)
    settings = Settings(tmp_path)
    settings.update({"gemini_api_key": "private-test-key"})
    client, manager = ClientSocket(), SessionManager()
    runtime = runtime or TranslationRuntime()
    session = StreamSession(client, runtime, manager, settings, "gemini", language)
    await session.start()
    await client.wait_type("ready")
    return session, provider, client, runtime


async def send_interim(provider, client, text):
    after = len(client.events)
    provider.received.put_nowait({"serverContent": {"interimInputTranscription": {"text": text}}})
    event = await client.wait_matching(lambda e: e["type"] == "transcript" and e.get("text") == text and not e.get("is_final"), after)
    assert event["text"] == text and event["is_final"] is False


@pytest.fixture
def quick_stability(monkeypatch):
    assert StreamingSentences.stability_seconds == .6
    assert StreamingSentences.minimum_observations == 2
    monkeypatch.setattr(StreamingSentences, "stability_seconds", .05)


def provider_final(provider, text):
    provider.received.put_nowait({"serverContent": {"inputTranscription": {"text": text}}})


async def stable_interim(provider, client, text):
    await send_interim(provider, client, text)
    await send_interim(provider, client, text)


@pytest.mark.asyncio
async def test_stable_interim_is_provisional_and_matching_final_reuses_translation(tmp_path, monkeypatch, quick_stability):
    session, provider, client, runtime = await make_gemini_session(tmp_path, monkeypatch)
    try:
        text = "比赛将在五分钟后开始。"
        await stable_interim(provider, client, text)
        provisional = await client.wait_type("caption")
        assert provisional["source_text"] == text and provisional["is_final"] is False
        assert len(runtime.translations) == 1
        provider_final(provider, text)
        final = await client.wait_matching(lambda e: e["type"] == "caption" and e.get("is_final") is True)
        assert final["segment_id"] == provisional["segment_id"]
        assert final["source_text"] == text and final["text"] == provisional["text"]
        assert len(runtime.translations) == 1 and provider.ends() == []
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_changed_final_revises_same_caption_id(tmp_path, monkeypatch, quick_stability):
    session, provider, client, runtime = await make_gemini_session(tmp_path, monkeypatch)
    try:
        await stable_interim(provider, client, "比赛将在五分钟后开始。")
        provisional = await client.wait_type("caption")
        corrected = "比赛将在十分钟后开始。"
        provider_final(provider, corrected)
        final = await client.wait_matching(lambda e: e["type"] == "caption" and e.get("is_final") is True)
        assert final["segment_id"] == provisional["segment_id"] and final["source_text"] == corrected
        assert final["revision"] > provisional["revision"]
        assert [text for text, _, _ in runtime.translations] == ["比赛将在五分钟后开始。", corrected]
        assert provider.ends() == []
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_single_or_incomplete_interim_never_translates_and_audio_never_ends(tmp_path, monkeypatch, quick_stability):
    session, provider, client, runtime = await make_gemini_session(tmp_path, monkeypatch)
    try:
        await send_interim(provider, client, "只有一次观察。")
        await asyncio.sleep(.15)
        assert runtime.translations == []
        await stable_interim(provider, client, "尚未完成的句子")
        for pcm in (b"\xe8\x03" * 3200, b"\0\0" * 8000):
            await session.feed(pcm)
            assert base64.b64decode((await provider.wait_input("audio"))["data"]) == pcm
        await asyncio.sleep(.15)
        assert runtime.translations == [] and provider.ends() == []
        assert session.active
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_authoritative_final_retracts_surplus_provisional_captions(tmp_path, monkeypatch, quick_stability, empty):
    session, provider, client, runtime = await make_gemini_session(tmp_path, monkeypatch)
    try:
        await stable_interim(provider, client, "第一句。第二句。")
        provisional = [await client.wait_type("caption") for _ in range(2)]
        provider_final(provider, "" if empty else "第一句。")
        expected_removed = [event["segment_id"] for event in provisional[(0 if empty else 1):]]
        for segment in expected_removed:
            removal = await client.wait_matching(lambda e: e["type"] == "caption_remove" and e["segment_id"] == segment)
            assert removal["session_id"] == session.id
        if not empty:
            final = await client.wait_matching(lambda e: e["type"] == "caption" and e.get("is_final") is True)
            assert final["segment_id"] == provisional[0]["segment_id"]
        assert len(runtime.translations) == 2 and provider.ends() == []
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_simultaneous_final_and_new_interim_are_both_processed(tmp_path, monkeypatch, quick_stability):
    session, provider, client, runtime = await make_gemini_session(tmp_path, monkeypatch)
    try:
        provider.received.put_nowait({"serverContent": {
            "inputTranscription": {"text": "前一个发言。"},
            "interimInputTranscription": {"text": "下一个发言。"},
        }})
        first = await client.wait_matching(lambda e: e["type"] == "caption" and e.get("is_final") is True)
        assert first["source_text"] == "前一个发言。"
        await client.wait_matching(lambda e: e["type"] == "transcript" and e.get("text") == "下一个发言。" and not e.get("is_final"))
        await send_interim(provider, client, "下一个发言。")
        provisional = await client.wait_matching(lambda e: e["type"] == "caption" and e.get("source_text") == "下一个发言。")
        assert provisional["is_final"] is False and provisional["segment_id"] != first["segment_id"]
        provider_final(provider, "下一个发言。")
        final = await client.wait_matching(lambda e: e["type"] == "caption" and e.get("source_text") == "下一个发言。" and e.get("is_final") is True)
        assert final["segment_id"] == provisional["segment_id"]
        assert [text for text, _, _ in runtime.translations] == ["前一个发言。", "下一个发言。"]
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_repeated_finals_across_turns_keep_distinct_ids(tmp_path, monkeypatch):
    session, provider, client, runtime = await make_gemini_session(tmp_path, monkeypatch)
    try:
        text = "请再说一次。"
        provider_final(provider, text)
        provider_final(provider, text)
        captions = [await client.wait_type("caption") for _ in range(2)]
        assert len({event["segment_id"] for event in captions}) == 2
        assert [event["source_text"] for event in captions] == [text, text]
        assert all(event["is_final"] for event in captions)
        assert [source for source, _, _ in runtime.translations] == [text, text]
    finally:
        await session.stop(notify=False)


class BlockedTranslationRuntime(TranslationRuntime):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def translate(self, text, language, context, *, target_language="ko"):
        self.translations.append((text, language, context))
        self.started.set()
        await self.release.wait()
        return "번역: " + text


@pytest.mark.asyncio
async def test_many_revisions_coalesce_before_a_blocked_worker_is_released(tmp_path, monkeypatch):
    runtime = BlockedTranslationRuntime()
    session, provider, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        original, corrected = "最初的句子。", "最终正确句子。"
        await session._apply_sentences([SentenceUpdate(1, original, False)])
        await asyncio.wait_for(runtime.started.wait(), 2)

        async def revise_then_finalize():
            for number in range(1, 9):
                await session._apply_sentences([SentenceUpdate(1, f"第{number}次修订。", False)])
            await session._apply_sentences([SentenceUpdate(1, corrected, True)])

        # This must finish with the first inference still blocked: a queue of
        # six superseded revisions otherwise prevents the final from arriving.
        await asyncio.wait_for(revise_then_finalize(), 2)
        assert not runtime.release.is_set()
        assert session.texts.qsize() == 1
        assert [text for text, _, _ in runtime.translations] == [original]
        assert not any(event["type"] == "caption" for event in client.events)
        pcm = b"\xe8\x03" * 1600
        await session.feed(pcm)
        assert base64.b64decode((await provider.wait_input("audio"))["data"]) == pcm
        assert provider.ends() == []

        runtime.release.set()
        caption = await client.wait_type("caption")
        assert caption["source_text"] == corrected and caption["is_final"] is True
        assert caption["segment_id"] == 1 and caption["revision"] == 10
        assert [text for text, _, _ in runtime.translations] == [original, corrected]
        assert [event["source_text"] for event in client.events if event["type"] == "caption"] == [corrected]
        assert session.active
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_revision_pruning_preserves_other_queued_sentences_in_order(tmp_path, monkeypatch):
    runtime = BlockedTranslationRuntime()
    session, _, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        await session._apply_sentences([SentenceUpdate(1, "初稿。", False)])
        await asyncio.wait_for(runtime.started.wait(), 2)
        await session._apply_sentences([
            SentenceUpdate(1, "过时修订。", False),
            SentenceUpdate(2, "第二句。", True),
            SentenceUpdate(3, "第三句。", True),
            SentenceUpdate(1, "修订完成。", True),
        ])
        assert session.texts.qsize() == 3
        runtime.release.set()
        captions = [await client.wait_type("caption") for _ in range(3)]
        assert [event["source_text"] for event in captions] == ["第二句。", "第三句。", "修订完成。"]
        assert [text for text, _, _ in runtime.translations] == ["初稿。", "第二句。", "第三句。", "修订完成。"]
        assert all(event["is_final"] for event in captions)
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_inflight_old_and_queued_superseded_revisions_never_publish(tmp_path, monkeypatch, quick_stability):
    runtime = BlockedTranslationRuntime()
    session, provider, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        await stable_interim(provider, client, "旧的句子。")
        await asyncio.wait_for(runtime.started.wait(), 2)
        await stable_interim(provider, client, "中间修订句子。")
        await asyncio.sleep(.15)
        assert session.texts.qsize() >= 1 and len(runtime.translations) == 1
        corrected = "最终正确句子。"
        provider_final(provider, corrected)
        await client.wait_matching(lambda e: e["type"] == "transcript" and e.get("is_final") is True and e.get("text") == corrected)
        runtime.release.set()
        caption = await client.wait_type("caption")
        assert caption["is_final"] is True and caption["source_text"] == corrected
        assert caption["text"] == "번역: " + corrected
        assert [text for text, _, _ in runtime.translations] == ["旧的句子。", corrected]
        assert [event["source_text"] for event in client.events if event["type"] == "caption"] == [corrected]
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_same_text_final_while_translation_runs_is_not_translated_twice(tmp_path, monkeypatch, quick_stability):
    runtime = BlockedTranslationRuntime()
    session, provider, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        text = "同一个句子。"
        await stable_interim(provider, client, text)
        await asyncio.wait_for(runtime.started.wait(), 2)
        provider_final(provider, text)
        await client.wait_matching(lambda e: e["type"] == "transcript" and e.get("text") == text and e.get("is_final") is True)
        runtime.release.set()
        caption = await client.wait_type("caption")
        assert caption["is_final"] is True and caption["source_text"] == text
        assert len(runtime.translations) == 1
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_retracted_inflight_caption_cannot_reappear(tmp_path, monkeypatch, quick_stability):
    runtime = BlockedTranslationRuntime()
    session, provider, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        await stable_interim(provider, client, "将被撤回。")
        await asyncio.wait_for(runtime.started.wait(), 2)
        provider_final(provider, "")
        await client.wait_type("caption_remove")
        runtime.release.set()
        await asyncio.sleep(.1)
        assert not any(event["type"] == "caption" for event in client.events)
        assert session.active
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_long_final_backpressure_keeps_audio_continuous_and_all_sentences(tmp_path, monkeypatch):
    runtime = BlockedTranslationRuntime()
    session, provider, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        sentences = [f"第{number}句。" for number in range(1, 10)]
        provider_final(provider, "".join(sentences))
        await asyncio.wait_for(runtime.started.wait(), 2)
        for _ in range(8):
            assert (await client.wait_type("transcript"))["is_final"] is True
        assert session.texts.qsize() == session.texts.maxsize == 6
        pcm = b"\xe8\x03" * 1600
        for _ in range(3):
            await session.feed(pcm)
            assert base64.b64decode((await provider.wait_input("audio"))["data"]) == pcm
        assert provider.ends() == [] and session.active
        runtime.release.set()
        captions = [await client.wait_type("caption") for _ in sentences]
        assert [event["source_text"] for event in captions] == sentences
        assert len({event["segment_id"] for event in captions}) == 9
        assert [text for text, _, _ in runtime.translations] == sentences
        assert session.active and not any(event["type"] in ("error", "stopped") for event in client.events)
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_stop_cancels_pending_stability_and_provider_reader(tmp_path, monkeypatch):
    session, provider, client, runtime = await make_gemini_session(tmp_path, monkeypatch)
    try:
        await stable_interim(provider, client, "尚未达到稳定时间。")
        await asyncio.wait_for(provider.read_waiting.wait(), 2)
        await session.stop()
        await client.wait_type("stopped")
        sent_count = len(provider.sent)
        await asyncio.sleep(.65)
        assert runtime.translations == [] and len(provider.sent) == sent_count
        assert provider.ends() == [] and provider.closed and provider.active_readers == 0
        assert provider.reader_cancelled.is_set() and all(task.done() for task in session.tasks)
    finally:
        await session.stop(notify=False)


@pytest.mark.asyncio
async def test_stop_cancels_final_blocked_on_translation_queue(tmp_path, monkeypatch):
    runtime = BlockedTranslationRuntime()
    session, provider, client, _ = await make_gemini_session(tmp_path, monkeypatch, runtime)
    try:
        provider_final(provider, "".join(f"第{number}句。" for number in range(1, 10)))
        await asyncio.wait_for(runtime.started.wait(), 2)
        for _ in range(8):
            await client.wait_type("transcript")
        assert session.texts.full()
        await asyncio.wait_for(session.stop(), 2)
        await client.wait_type("stopped")
        runtime.release.set()
        await asyncio.sleep(0)
        assert all(task.done() for task in session.tasks) and provider.active_readers == 0
        assert provider.closed and not any(event["type"] == "caption" for event in client.events)
    finally:
        await session.stop(notify=False)


