"""Caption history integration with real sessions and local fake transports."""
import asyncio
from array import array
import json
import struct
import threading

import pytest

from engine.history import CaptionHistory
from engine.local_streaming import ASRWord, LocalWhisperStreaming
from engine.server import create_app
from engine.sessions import SessionManager, StreamSession
from engine.settings import Settings
from engine.streaming_sentences import SentenceUpdate
from engine.tests.test_gemini import ClientSocket


class HistoryRuntime:
    def __init__(self):
        self.ready = False
        self.released = False

    async def prepare(self, settings):
        self.ready = True

    async def release(self):
        self.released = True

    async def translate(self, text, language, context, *, target_language="ko"):
        return "한국어 번역: " + {"First.": "첫 문장입니다.",
                              "Corrected.": "고친 문장입니다.",
                              "Second.": "두 번째 문장입니다."}.get(text, "새 문장입니다.")


async def start_session(root, manager=None, runtime=None, settings=None, client=None):
    manager = manager or SessionManager(CaptionHistory(root))
    runtime = runtime or HistoryRuntime()
    client = client or ClientSocket()
    session = StreamSession(client, runtime, manager, settings or Settings(root), "local", "en")
    await session.start()
    await client.wait_type("ready")
    return session, client, manager


def saved_rows(root):
    return json.loads((root / "logs" / "caption-history.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_successful_session_caption_persists_source_translation_and_identity(tmp_path):
    session, client, manager = await start_session(tmp_path)
    try:
        await session._final("First.", "en")
        caption = await client.wait_type("caption")
        await manager.flush_history()
        rows = saved_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["source_text"] == "First."
        assert rows[0]["text"] == caption["text"] == "한국어 번역: 첫 문장입니다."
        assert rows[0]["session_id"] == session.id
        assert rows[0]["segment_id"] == caption["segment_id"] == 1
        assert rows[0]["timestamp"] and rows[0]["status"] == "ok"
        assert rows[0]["is_final"] is True
        assert session.active
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_same_id_correction_updates_record_and_retraction_removes_it(tmp_path):
    session, client, manager = await start_session(tmp_path)
    try:
        await session._apply_sentences([SentenceUpdate(1, "First.", False)])
        await client.wait_type("caption")
        await manager.flush_history()
        timestamp = saved_rows(tmp_path)[0]["timestamp"]

        await session._apply_sentences([SentenceUpdate(1, "Corrected.", True)])
        corrected = await client.wait_type("caption")
        await manager.flush_history()
        assert len(saved_rows(tmp_path)) == 1
        assert saved_rows(tmp_path)[0] == {
            **saved_rows(tmp_path)[0], "timestamp": timestamp, "segment_id": 1,
            "source_text": "Corrected.", "text": corrected["text"],
            "revision": 2, "is_final": True,
        }

        await session._apply_sentences([SentenceUpdate(2, "Second.", True)])
        await client.wait_type("caption")
        await session._apply_sentences([SentenceUpdate(1, "Corrected.", True, removed=True)])
        removed = await client.wait_type("caption_remove")
        await manager.flush_history()
        assert removed["segment_id"] == 1
        assert [row["segment_id"] for row in saved_rows(tmp_path)] == [2]
        assert saved_rows(tmp_path)[0]["source_text"] == "Second."
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_restart_loads_disk_history_without_replaying_it_to_socket(tmp_path):
    history = CaptionHistory(tmp_path)
    history.upsert({"session_id": "earlier-session", "segment_id": 1,
                    "source_text": "Earlier.", "text": "이전 번역입니다."})
    runtime = HistoryRuntime()
    app = create_app(tmp_path, runtime)
    assert app.state.sessions.history.snapshot()[0]["session_id"] == "earlier-session"
    async with app.router.lifespan_context(app):
        session, client, manager = await start_session(
            tmp_path, app.state.sessions, runtime, app.state.settings)
        # Reading history during create_app/start must not synthesize UI events.
        assert [event["type"] for event in client.events] == ["status", "ready"]
        assert not any(event["type"] in {"caption", "transcript"} for event in client.events)
        await session._final("First.", "en")
        await client.wait_type("caption")
        await manager.flush_history()
        assert [row["session_id"] for row in saved_rows(tmp_path)] == [session.id, "earlier-session"]
        assert [event["source_text"] for event in client.events if event["type"] == "caption"] == ["First."]
    assert runtime.released
    assert not session.active


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError, ValueError])
async def test_storage_failure_warns_once_and_following_caption_continues(tmp_path, failure):
    class UnwritableHistory(CaptionHistory):
        def _save(self, rows):
            raise failure("simulated storage failure")

    manager = SessionManager(UnwritableHistory(tmp_path))
    session, client, _ = await start_session(tmp_path, manager)
    try:
        await session._final("First.", "en")
        assert (await client.wait_type("caption"))["source_text"] == "First."
        await manager.flush_history()
        warnings = [event for event in client.events if event["type"] == "warning"]
        assert len(warnings) == 1 and warnings[0]["code"] == "caption_log_failed"
        assert session.active and manager.active is session

        await session._final("Second.", "en")
        assert (await client.wait_type("caption"))["source_text"] == "Second."
        await manager.flush_history()
        assert len([event for event in client.events if event["type"] == "warning"]) == 1
        assert not any(event["type"] in {"error", "stopped"} for event in client.events)
        assert session.active
    finally:
        await manager.stop()


class SlowHistory(CaptionHistory):
    def __init__(self, root):
        super().__init__(root)
        self.entered = threading.Event()
        self.release_write = threading.Event()
        self.calls = 0

    def upsert(self, event):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            if not self.release_write.wait(5):
                raise TimeoutError("test did not release the blocked write")
        return super().upsert(event)


@pytest.mark.asyncio
async def test_slow_storage_does_not_hold_caption_socket_lock(tmp_path):
    history = SlowHistory(tmp_path)
    manager = SessionManager(history)
    session, client, _ = await start_session(tmp_path, manager)
    try:
        await session._final("First.", "en")
        await client.wait_type("caption")
        assert await asyncio.to_thread(history.entered.wait, 2)
        # Storage remains blocked while the actual session sends another caption.
        await asyncio.wait_for(session._final("Second.", "en"), 1)
        second = await asyncio.wait_for(client.wait_type("caption"), 1)
        assert second["source_text"] == "Second."
        assert not history.release_write.is_set() and history.calls == 1
        assert session.active
        history.release_write.set()
        await asyncio.wait_for(manager.flush_history(), 2)
        assert [row["source_text"] for row in saved_rows(tmp_path)] == ["Second.", "First."]
    finally:
        history.release_write.set()
        await manager.stop()


@pytest.mark.asyncio
async def test_stop_waits_for_pending_history_write_after_stopping_session(tmp_path):
    history = SlowHistory(tmp_path)
    manager = SessionManager(history)
    session, client, _ = await start_session(tmp_path, manager)
    stopping = None
    try:
        await session._final("First.", "en")
        await client.wait_type("caption")
        assert await asyncio.to_thread(history.entered.wait, 2)
        stopping = asyncio.create_task(manager.stop())
        await client.wait_type("stopped")
        assert not session.active and not stopping.done()
        history.release_write.set()
        await asyncio.wait_for(stopping, 2)
        assert saved_rows(tmp_path)[0]["source_text"] == "First."
        assert all(task.done() for task in session.tasks)
        assert manager.active is None
    finally:
        history.release_write.set()
        if stopping is not None:
            await stopping
        else:
            await manager.stop()


class BackgroundWhisperRuntime(HistoryRuntime):
    """Recognize deterministic sample markers, without a model or real audio."""
    asr_backend = "whisper"
    translation_warnings = []

    def __init__(self, unstable=False):
        super().__init__()
        self.unstable = unstable
        self.snapshot = None
        self.calls = 0
        self.last_source = ""
        self.prompts = []

    def transcribe_stream(self, audio, language, initial_prompt):
        self.calls += 1
        samples = array("h", audio)
        duration = len(samples) / 16000
        self.prompts.append((self.snapshot.offset_seconds + duration, initial_prompt))
        words = []
        start = None
        for index, sample in enumerate(samples):
            if sample == 2100 and start is None:
                start = index
            elif sample != 2100 and start is not None:
                words.append(ASRWord(start / 16000, index / 16000, "Yes."))
                start = None
        if start is not None:
            words.append(ASRWord(start / 16000, len(samples) / 16000, "Yes."))
        if words or not self.unstable:
            self.last_source = " ".join(word.text for word in words)
            return words, "en"
        # No two observations agree; the word is nevertheless safely behind the
        # current audio frontier, so a pressure recovery can preserve it whole.
        self.last_source = ("Alpha" if self.calls % 2 else "Beta") + f" draft {self.calls}"
        return [ASRWord(max(.1, duration - 2), max(.7, duration - 1), self.last_source)], "en"


class BackgroundAuditSocket(ClientSocket):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime
        self.deadline_sources = []

    async def send_json(self, event):
        if event.get("code") == "local_streaming_deadline" and self.runtime.unstable:
            # The session sends the warning before enqueueing that recognition
            # result. Record the decoder's independent last response at that time.
            self.deadline_sources.append(self.runtime.last_source)
        await super().send_json(event)


def track_background_controller(monkeypatch, runtime, profile):
    """Observe real controller completion; never alter its limits/decisions."""
    controllers = []

    class ObservedController(LocalWhisperStreaming):
        def __init__(self, *args, **kwargs):
            # Stable uses early_transcription=True; keep the real constructor
            # options so tracking cannot accidentally change the policy tested.
            super().__init__(*args, **kwargs)
            self.fed_bytes = 0
            self.idle_bytes = 0
            controllers.append(self)

        def feed(self, data):
            super().feed(data)
            self.fed_bytes += len(data)

        def snapshot(self):
            value = super().snapshot()
            if value is not None:
                runtime.snapshot = value
            elif self._outstanding is None:
                self.idle_bytes = self.fed_bytes
            return value

    module = "engine.stable_sessions" if profile == "stable" else "engine.sessions"
    monkeypatch.setattr(f"{module}.LocalWhisperStreaming", ObservedController)
    return controllers


def background_pcm(seconds=1, marker=False):
    # Every sample exceeds the RMS gate, including the supposed music periods.
    # A marker only tells our fake recognizer where a known spoken word occurs.
    if marker:
        return struct.pack("<h", 2100) * 9600 + struct.pack("<h", 900) * 6400
    return struct.pack("<h", 900) * round(seconds * 16000)


async def feed_background_until_idle(session, client, controllers, data):
    target = (controllers[0].fed_bytes if controllers else 0) + len(data)
    await session.feed(data)

    async def settled():
        while session.active and (not controllers or controllers[0].idle_bytes < target):
            await asyncio.sleep(.001)

    await asyncio.wait_for(settled(), 3)
    errors = [event for event in client.events if event["type"] == "error"]
    assert session.active, errors
    await asyncio.wait_for(session.texts.join(), 3)
    assert controllers[0].buffered_seconds <= 24


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["legacy", "stable"])
@pytest.mark.parametrize("opening_speech", [False, True])
async def test_positive_rms_music_over_40_seconds_keeps_session_and_repeated_later_speech(
        tmp_path, monkeypatch, opening_speech, profile):
    runtime = BackgroundWhisperRuntime()
    controllers = track_background_controller(monkeypatch, runtime, profile)
    settings = Settings(tmp_path)
    settings.data["asr_profile"] = profile
    session, client, manager = await start_session(tmp_path, runtime=runtime, settings=settings)
    try:
        for second in range(46):
            await feed_background_until_idle(session, client, controllers,
                background_pcm(marker=opening_speech and second == 0))
        before = [event for event in client.events if event["type"] == "caption" and event["is_final"]]
        assert [event["source_text"] for event in before] == (["Yes."] if opening_speech else [])
        if opening_speech:
            assert any(prompt == "Yes." for _, prompt in runtime.prompts)
        assert runtime.prompts[-1][0] >= 40
        assert all(prompt == "" for end, prompt in runtime.prompts if end >= 40)

        # Two genuine repetitions after the long music interval must remain two
        # distinct captions; context matching must not erase the later speech.
        for _ in range(2):
            await feed_background_until_idle(session, client, controllers, background_pcm(marker=True))
            await feed_background_until_idle(session, client, controllers, background_pcm())
        captions = [event for event in client.events if event["type"] == "caption" and event["is_final"]]
        assert [event["source_text"] for event in captions] == ["Yes."] * (2 + int(opening_speech))
        assert len({event["segment_id"] for event in captions}) == len(captions)
        if profile == "stable":
            assert all(event["is_final"] for event in client.events if event["type"] == "caption")
        assert not any(event["type"] in {"error", "stopped"} for event in client.events)
        assert session.active and manager.active is session
        await manager.flush_history()
        rows = saved_rows(tmp_path)
        assert len(rows) == len(captions)
        assert all(row["is_final"] for row in rows)
        assert {row["segment_id"] for row in rows} == {event["segment_id"] for event in captions}
    finally:
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["legacy", "stable"])
async def test_unstable_recognition_over_40_seconds_recovers_final_windows_and_following_speech(
        tmp_path, monkeypatch, profile):
    runtime = BackgroundWhisperRuntime(unstable=True)
    controllers = track_background_controller(monkeypatch, runtime, profile)
    client = BackgroundAuditSocket(runtime)
    settings = Settings(tmp_path)
    settings.data["asr_profile"] = profile
    session, client, manager = await start_session(tmp_path, runtime=runtime, client=client, settings=settings)
    try:
        for _ in range(46):
            await feed_background_until_idle(session, client, controllers, background_pcm())
        assert client.deadline_sources, "Pressure recovery must eventually publish a pending result"
        before = [event["source_text"] for event in client.events if event["type"] == "caption" and event["is_final"]]
        assert before == client.deadline_sources
        assert len(before) == len(set(before))

        runtime.unstable = False
        for _ in range(2):
            await feed_background_until_idle(session, client, controllers, background_pcm(marker=True))
            await feed_background_until_idle(session, client, controllers, background_pcm())
        captions = [event for event in client.events if event["type"] == "caption" and event["is_final"]]
        assert [event["source_text"] for event in captions] == client.deadline_sources + ["Yes.", "Yes."]
        assert len({event["segment_id"] for event in captions}) == len(captions)
        if profile == "stable":
            assert all(event["is_final"] for event in client.events if event["type"] == "caption")
        assert not any(event["type"] in {"error", "stopped"} for event in client.events)
        assert session.active
    finally:
        await manager.stop()
