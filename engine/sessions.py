"""Authenticated client sessions, bounded audio work, and transcription adapters."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote

from .audio import AudioQueue, PCMChunker, remove_overlap
from .history import CaptionHistory
from .local_captions import LocalCaptions
from .local_streaming import LocalWhisperStreaming
from .sentences import split_sentences
from .streaming_sentences import StreamingSentences, SentenceUpdate
from .settings import EngineError, normalize_language, normalize_target_language
from .recovery_log import RecoveryLog


@dataclass
class Translation:
    source: str
    language: str
    is_final: bool
    revision: int = 1
    result: str | None = None
    failed: bool = False
    context_epoch: int = 0


class SessionManager:
    def __init__(self, history: CaptionHistory | None = None):
        self.active: StreamSession | None = None
        self.lock = asyncio.Lock()
        self.history = history
        self._history_task: asyncio.Task | None = None
        self.log_epoch = 0

    def record_caption(self, session: StreamSession, event: dict) -> None:
        if self.history is None or event.get("type") not in {"caption", "caption_remove"}:
            return
        # Schedule in socket order, but never hold the socket lock for disk I/O.
        previous = self._history_task
        self._history_task = asyncio.create_task(
            self._record_caption(previous, session, dict(event)), name="caption-log")

    async def _record_caption(self, previous, session: StreamSession, event: dict) -> None:
        if previous is not None:
            await asyncio.shield(previous)
        try:
            if event["type"] == "caption_remove":
                await asyncio.to_thread(self.history.remove, event["session_id"], event["segment_id"])
            else:
                await asyncio.to_thread(self.history.upsert, event)
        except (OSError, ValueError):
            # A full/read-only disk must not stop recognition or translation.
            if not session._history_warning_sent:
                session._history_warning_sent = True
                try:
                    await session.emit({"type": "warning", "code": "caption_log_failed",
                        "message": "번역 로그 파일을 저장하지 못했습니다. 자막은 계속 표시됩니다."})
                except Exception:
                    pass

    async def flush_history(self) -> None:
        if self._history_task is not None:
            await asyncio.shield(self._history_task)

    async def clear_logs(self, log_lock) -> dict:
        self.log_epoch += 1  # Pending diagnostic writes must not restore cleared records.
        # Install the barrier before yielding. Earlier queued writes finish
        # before deletion; subsequent captions wait for it and start a fresh log.
        previous = self._history_task

        async def clear_after_pending():
            if previous is not None:
                await asyncio.shield(previous)
            try:
                return await asyncio.to_thread(self.history.clear_logs, log_lock)
            except OSError:
                # Don't leave a failed predecessor that poisons future logging.
                return {"ok": False, "deleted_count": 0, "failed_files": ["logs/"]}

        task = asyncio.create_task(clear_after_pending(), name="clear-caption-logs")
        self._history_task = task
        return await asyncio.shield(task)

    async def claim(self, session: StreamSession) -> None:
        async with self.lock:
            if self.active is not None:
                raise EngineError("session_busy", "다른 창이나 브라우저에서 이미 자막을 처리 중입니다.", 409)
            self.active = session

    async def remove(self, session: StreamSession) -> None:
        async with self.lock:
            if self.active is session:
                self.active = None

    async def stop(self) -> None:
        session = self.active
        if session:
            await session.stop()
        await self.flush_history()


class StreamSession:
    def __init__(self, websocket, runtime, manager: SessionManager, settings, mode: str, language: str | None,
                 *, target_language: str | None = None):
        self.ws, self.runtime, self.manager, self.settings = websocket, runtime, manager, settings
        self.mode, self.language = mode, normalize_language(language)
        # Snapshot the requested target for queued work and revisions in this run.
        self.target_language = normalize_target_language(
            settings.data.get("target_language", "ko") if target_language is None else target_language)
        self.id = uuid.uuid4().hex
        self.active = False
        self.audio = AudioQueue()
        self.texts: asyncio.Queue[tuple[int, Translation]] = asyncio.Queue(6)
        self._translations: dict[int, Translation] = {}
        self._final_segments: set[int] = set()
        self._removed_segments: set[int] = set()
        self._revisions: dict[int, int] = {}
        self._context: dict[int, str] = {}
        self._context_epoch = 0
        self._sentence_order: dict[int, tuple[int, int]] = {}
        self._turn = 0
        self.tasks: list[asyncio.Task] = []
        self.send_lock = asyncio.Lock()
        self.segment = 0
        self.gemini = None
        self._history_warning_sent = False
        self._deferred_audio: bytes | None = None
        self._asr_generation = 0
        self._translation_inflight: tuple[int, Translation] | None = None
        self._timings = {"asr_seconds": 0.0, "translation_seconds": 0.0}
        self._recovery_pending: dict[str, dict] = {}
        self._recovery_ready = asyncio.Event()
        self._recovery_log = (RecoveryLog(runtime.root, lock=getattr(runtime, "log_lock", None))
                              if hasattr(runtime, "root") else None)

    @property
    def audio_generation(self):
        return self.audio.generation

    @property
    def audio_dropped_samples(self):
        return self.audio.dropped_bytes // 2

    async def next_audio(self):
        if self._deferred_audio is not None:
            pcm, self._deferred_audio = self._deferred_audio, None
            return pcm
        return await self.audio.get()

    def defer_audio(self, pcm):
        if self._deferred_audio is not None:
            raise RuntimeError("Only one audio packet may be deferred.")
        self._deferred_audio = pcm

    def _check_audio_generation(self):
        if self.audio_generation != self._asr_generation:
            raise EngineError("audio_gap_restart", "음성 입력 구간을 건너뛰고 인식을 다시 연결합니다.")

    async def _next_local_audio(self):
        pcm = await self.next_audio()
        if self.audio_generation != self._asr_generation:
            self.defer_audio(pcm)
            self._check_audio_generation()
        return pcm

    def record_timing(self, stage, seconds):
        key = stage + "_seconds"
        if key in self._timings:
            self._timings[key] = max(0.0, seconds)

    def break_audio_context(self):
        self._context.clear()
        self._context_epoch += 1

    def report_overload(self, reason, *, dropped_audio_seconds=0.0, dropped_items=0,
                        buffer_seconds=0.0):
        """Coalesce diagnostics without blocking capture, inference or creating tasks."""
        stage = "input" if reason == "input" else "translation" if reason == "translation" else "asr"
        epoch = self.manager.log_epoch
        previous = self._recovery_pending.get(stage)
        if previous is None or previous["epoch"] != epoch:
            previous = {"epoch": epoch, "session_id": self.id, "stage": stage,
                        "code": {"input": "input_backlog_dropped", "asr": "asr_backlog_dropped",
                                 "translation": "translation_backlog_dropped"}[stage],
                        "dropped_audio_seconds": 0.0, "dropped_items": 0, "buffer_seconds": 0.0}
            self._recovery_pending[stage] = previous
        previous["dropped_audio_seconds"] += max(0.0, dropped_audio_seconds)
        previous["dropped_items"] += max(0, dropped_items)
        previous["buffer_seconds"] = max(previous["buffer_seconds"], buffer_seconds)
        previous.update(self._timings, queue_items=(self.audio.queue.qsize() if stage == "input" else self.texts.qsize()))
        self._recovery_ready.set()

    async def _recovery_reports(self):
        last_notice = float("-inf")
        while self.active:
            await self._recovery_ready.wait()
            self._recovery_ready.clear()
            reports = list(self._recovery_pending.values())
            self._recovery_pending.clear()
            for report in reports:
                epoch = report.pop("epoch")
                if self._recovery_log is not None:
                    # Respect the same clear/write barrier as caption history.
                    barrier = self.manager._history_task
                    if barrier is not None:
                        await asyncio.shield(barrier)
                    def write_current(report=report, epoch=epoch):
                        with self._recovery_log.lock:
                            if epoch == self.manager.log_epoch:
                                self._recovery_log.write(report)
                    try:
                        await asyncio.to_thread(write_current)
                    except (OSError, ValueError):
                        pass  # Diagnostics must not stop captions or disclose payloads.
            if self.active and time.monotonic() - last_notice >= 2:
                last_notice = time.monotonic()
                await self.emit({"type": "warning", "code": "buffer_recovered",
                    "message": "처리가 지연되어 일부 구간을 건너뛰었습니다. 최신 입력부터 자막을 계속 처리합니다."})

    async def _send_locked(self, event: dict) -> None:
        payload = {"session_id": self.id, **event, "target_language": self.target_language}
        await asyncio.wait_for(self.ws.send_json(payload), 5)
        self.manager.record_caption(self, payload)

    async def emit(self, event: dict) -> None:
        async with self.send_lock:
            if self.active:
                await self._send_locked(event)

    async def start(self) -> None:
        await self.manager.claim(self)
        self.active = True
        try:
            await self.emit({"type": "status", "message": "음성 인식·번역 모델을 준비하고 있습니다."})
            if self.mode == "gemini":
                self.settings.key()  # Fail before spending time loading translation model.
            await self.runtime.prepare({**self.settings.data, "mode": self.mode, "language": self.language,
                                        "target_language": self.target_language})
            for warning in getattr(self.runtime, "translation_warnings", []):
                await self.emit({"type": "warning", "code": "translation_glossary_invalid", "message": warning})
            if self.mode == "gemini":
                await self._open_gemini()
            await self.emit({"type": "ready"})
            self.tasks.append(asyncio.create_task(self._guard(self._recovery_reports), name="buffer-recovery-log"))
            self.tasks.append(asyncio.create_task(self._guard(self._translate), name="subtitle-translation"))
            if self.mode == "local":
                self.tasks.append(asyncio.create_task(self._guard(self._local), name="local-audio"))
            else:
                self.tasks.append(asyncio.create_task(self._guard(self._gemini_send), name="gemini-send"))
                self.tasks.append(asyncio.create_task(self._guard(self._gemini_receive), name="gemini-receive"))
        except BaseException:
            await self.stop(notify=False)
            raise

    async def _guard(self, coroutine) -> None:
        try:
            await (coroutine() if callable(coroutine) else coroutine)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, EngineError) else EngineError("stream_failed", "음성 처리 연결에 문제가 발생했습니다. 다시 시작해 주세요.", 503)
            try:
                await self.emit({"type": "error", "code": error.code, "message": error.message})
            finally:
                await self.stop()

    async def feed(self, data: bytes) -> None:
        if not self.active:
            raise EngineError("session_stopped", "자막 세션이 중지되어 있습니다.", 409)
        if not data or len(data) % 2 or len(data) > 32000:
            raise EngineError("invalid_audio", "오디오는 16kHz mono PCM16LE, 메시지당 최대 1초여야 합니다.")
        dropped = self.audio.put_latest(data)
        if dropped:
            # A restart may have parked an older packet outside AudioQueue.
            # A second gap must not splice that packet into the newest audio.
            deferred = len(self._deferred_audio or b"")
            self._deferred_audio = None
            self.audio.note_gap(deferred)
            dropped += deferred
            self.report_overload("input", dropped_audio_seconds=dropped / 32000,
                                 buffer_seconds=self.audio.bytes / 32000)

    def input_gap(self, dropped_samples: int) -> None:
        if type(dropped_samples) is not int or not 0 < dropped_samples <= 16000 * 3600:
            raise EngineError("invalid_audio_gap", "오디오 건너뛰기 길이가 올바르지 않습니다.")
        if not self.active:
            raise EngineError("session_stopped", "자막 세션이 중지되어 있습니다.", 409)
        # Pending server-side PCM precedes the client-side gap. Do not splice it
        # into the fresh input after a controller reset.
        queued = self.audio.discard_pending()
        deferred = len(self._deferred_audio or b"")
        self._deferred_audio = None
        self.audio.note_gap(dropped_samples * 2 + deferred)
        self.report_overload("input", dropped_audio_seconds=(queued + deferred) / 32000 + dropped_samples / 16000)

    async def _final(self, text: str, language: str) -> None:
        context_epoch = self._context_epoch
        text = text.strip()
        if not text or not self.active:
            return
        if len(text) > 6000:
            raise EngineError("transcript_too_long", "음성 구간이 너무 깁니다. 자막을 다시 시작해 주세요.")
        # Preserve explicit selection through translation and caption metadata;
        # only auto mode accepts the recognizer's detected language.
        language = language if self.language == "auto" else self.language
        for sentence in split_sentences(text):
            if not self.active:
                return
            self.segment += 1
            self._sentence_order[self.segment] = (self.segment, 0)
            await self._queue_translation(self.segment, sentence, language, True,
                                          context_epoch=context_epoch)

    async def _queue_translation(self, segment: int, source: str, language: str, is_final: bool,
                                 *, context_epoch: int | None = None) -> bool:
        # Capture before any send/queue await. Recovery may happen while this
        # accepted ASR result is being split or published; its age cannot change.
        if context_epoch is None:
            context_epoch = self._context_epoch
        if segment in self._removed_segments or (not is_final and segment in self._final_segments):
            return True  # Retired updates must not be retried.
        previous = self._translations.get(segment)
        await self.emit({"type": "transcript", "segment_id": segment, "text": source,
                         "is_final": is_final, "preview": self.mode == "local"})
        if not self.active or segment in self._removed_segments:
            return True
        if (self.mode == "local" and not is_final
                and (self.texts.full() or any(
                    key != segment and work.is_final and work.result is None
                    for key, work in self._translations.items()))):
            # Optional early captions must never hold up definitive speech or
            # invalidate a preview already being translated. Finals always enter
            # the normal queue, including after a preview was skipped here.
            return False
        if is_final:
            self._final_segments.add(segment)
        if (previous is not None and previous.source == source and previous.language == language
                and not (previous.failed and is_final)):
            previous.is_final |= is_final
            if is_final and previous.result is not None:
                # Final confirmation reuses both the translation and caption ID.
                if not previous.failed:
                    self._remember_context(segment, previous)
                await self._caption(segment, previous)
                if self._translations.get(segment) is previous:
                    self._translations.pop(segment, None)
            return True
        revision = self._revisions.get(segment, 0) + 1
        self._revisions[segment] = revision
        work = Translation(source, language, is_final, revision, context_epoch=context_epoch)
        self._translations[segment] = work
        self._context.pop(segment, None)
        self._prune_stale_translations()
        # A provider can deliver several sentences at once. Give an ordinary
        # short burst one scheduling/translation interval before losing speech.
        if self.texts.full():
            try:
                await asyncio.wait_for(self.texts.put((segment, work)), .25)
                return True
            except TimeoutError:
                if not self.active or self._translations.get(segment) is not work:
                    return True
                self._prune_stale_translations()
        retired = []
        while self.texts.full():
            old_segment, old_work = self.texts.get_nowait()
            self.texts.task_done()
            if self._translations.get(old_segment) is old_work:
                self._retire_translation(old_segment, old_work)
                retired.append(old_segment)
                inflight = self._translation_inflight
                if (inflight is not None and self._translations.get(inflight[0]) is inflight[1]
                        and (inflight[1].context_epoch, self._sentence_order.get(inflight[0], (inflight[0], 0)))
                        <= (old_work.context_epoch, self._sentence_order.get(old_segment, (old_segment, 0)))):
                    self._retire_translation(*inflight)
                    retired.append(inflight[0])
        self.texts.put_nowait((segment, work))
        if retired:
            self.report_overload("translation", dropped_items=len(retired))
            for identifier in retired:
                await self.emit({"type": "caption_remove", "segment_id": identifier})
        return True

    def _retire_translation(self, segment, work):
        if self._translations.get(segment) is work:
            self._translations.pop(segment, None)
            self._context.pop(segment, None)
            self._removed_segments.add(segment)

    def _prune_stale_translations(self) -> None:
        # Revisions of one sentence must not occupy all six queue slots. This
        # section has no await: retain live jobs in order, remove superseded
        # versions only. Distinct spoken sentences are never dropped here.
        retained = []
        while not self.texts.empty():
            segment, work = self.texts.get_nowait()
            self.texts.task_done()
            if self._translations.get(segment) is work:
                retained.append((segment, work))
        for item in retained:
            self.texts.put_nowait(item)

    async def _apply_sentences(self, updates: list[SentenceUpdate], language: str | None = None) -> list[SentenceUpdate]:
        context_epoch = self._context_epoch
        language = language if self.language == "auto" and language else self.language
        deferred = []
        for update in updates:
            if not self.active:
                return deferred
            self.segment = max(self.segment, update.segment_id)
            self._sentence_order.setdefault(update.segment_id, (self._turn, update.segment_id))
            if update.removed:
                self._removed_segments.add(update.segment_id)
                self._translations.pop(update.segment_id, None)
                self._context.pop(update.segment_id, None)
                self._prune_stale_translations()
                await self.emit({"type": "caption_remove", "segment_id": update.segment_id})
            else:
                if not await self._queue_translation(update.segment_id, update.text, language, update.is_final,
                                                     context_epoch=context_epoch):
                    deferred.append(update)
        return deferred

    async def _caption(self, segment: int, work: Translation) -> None:
        async with self.send_lock:
            if self.active and self._translations.get(segment) is work:
                await self._send_locked({
                    "type": "caption", "segment_id": segment,
                    "source_text": work.source, "text": work.result, "language": work.language,
                    "is_final": work.is_final, "revision": work.revision,
                    "translation_status": "failed" if work.failed else "ok"})

    async def _translation_warning(self, segment: int, work: Translation, code: str) -> None:
        async with self.send_lock:
            if self.active and self._translations.get(segment) is work:
                await asyncio.wait_for(self.ws.send_json({"session_id": self.id,
                    "type": "warning", "code": code, "segment_id": segment,
                    "message": "한 문장의 번역을 완료하지 못했습니다. 원문은 최근 자막에 남기고 다음 문장을 계속 처리합니다."}), 5)

    async def _local(self) -> None:
        recoverable = {"audio_gap_restart", "local_streaming_overrun", "qwen_streaming_overrun",
                       "alignatt_audio_overrun", "fast_qwen_overrun"}
        while self.active:
            self._asr_generation = self.audio_generation
            try:
                await self._local_once()
                return
            except EngineError as exc:
                if exc.code not in recoverable or not self.active:
                    raise
                self.break_audio_context()
                provisional = [(identifier, work) for identifier, work in self._translations.items()
                               if not work.is_final]
                for identifier, work in provisional:
                    self._retire_translation(identifier, work)
                self._prune_stale_translations()
                for identifier, _ in provisional:
                    await self.emit({"type": "caption_remove", "segment_id": identifier})
                seconds = getattr(exc, "buffer_seconds", 0.0)
                self.report_overload("asr", dropped_audio_seconds=seconds, buffer_seconds=seconds)
                await asyncio.sleep(0)

    async def _local_once(self) -> None:
        profile = self.settings.data.get("asr_profile", "legacy")
        if profile == "alignatt":
            from .stable_sessions import alignatt
            await alignatt(self)
            return
        if profile == "stable":
            from .stable_sessions import qwen, whisper
            backend = getattr(self.runtime, "asr_backend", None)
            if backend == "whisper":
                await whisper(self)
                return
            if backend == "qwen3_asr":
                await qwen(self)
                return
        if getattr(self.runtime, "asr_backend", None) == "whisper":
            await self._local_whisper()
            return
        if (getattr(self.runtime, "asr_backend", None) == "qwen3_asr"
                and self.settings.data.get("qwen_boundary_recheck", False)):
            await self._local_qwen_recheck()
            return
        if getattr(self.runtime, "asr_backend", None) == "qwen3_asr":
            from .fast_qwen_session import run
            await run(self)
            return
        chunker, previous = PCMChunker(), ""
        while self.active:
            pcm = await self._next_local_audio()
            for piece in chunker.feed(pcm):
                if not self.active:
                    return
                text, language = await asyncio.to_thread(self.runtime.transcribe, piece.pcm, self.language)
                self._check_audio_generation()
                if not self.active:
                    return
                raw_text = text
                if piece.overlaps_previous:
                    text = remove_overlap(previous, text)
                previous = raw_text
                await self._final(text, language)

    async def _local_qwen_recheck(self) -> None:
        from .qwen_boundary import BoundaryChunker
        from difflib import SequenceMatcher

        chunker = BoundaryChunker()
        previous = ""
        draft = ""
        draft_language = self.language
        while self.active:
            pcm = await self._next_local_audio()
            if not self.active:
                return
            chunker.feed(pcm)
            while self.active and (request := chunker.next_request()) is not None:
                try:
                    text, language = await asyncio.to_thread(
                        self.runtime.transcribe, request.pcm, self.language)
                    self._check_audio_generation()
                except EngineError as exc:
                    # A bounded second decode may fail even though the first
                    # result was usable. Never turn a stopped/failed session
                    # into a new caption, and never swallow a device failure.
                    if not request.rechecked or not draft or exc.code not in {
                            "asr_truncated", "asr_invalid_output"}:
                        raise
                    text, language = "", draft_language
                if not self.active:
                    return
                if request.probe:
                    draft, draft_language = text, language
                    await self.emit({"type": "transcript", "segment_id": self.segment + 1,
                                     "text": text, "is_final": False, "preview": True})
                    chunker.resolve(request)
                    continue
                use_extension = True
                if request.rechecked and draft and not text.strip():
                    text, language = draft, draft_language
                    use_extension = False
                    await self.emit({"type": "warning", "code": "qwen_boundary_fallback",
                        "message": "문장 재확인 결과가 비어 이전 인식을 사용하고 추가 음성은 다음 구간에서 다시 처리합니다."})
                elif request.rechecked and draft and text:
                    # Only a diagnostic: do not combine contradictory ASR
                    # strings or discard acoustics based on character ratios.
                    common = SequenceMatcher(None, draft, text, autojunk=False).ratio()
                    if len(draft) >= 12 and common < .45:
                        await self.emit({"type": "warning", "code": "qwen_boundary_revised",
                            "message": "뒤 음성과 함께 재확인하면서 원문 인식이 크게 달라졌습니다."})
                raw_text = text
                if request.overlaps_previous:
                    text = remove_overlap(previous, text)
                previous = raw_text
                await self._final(text, language)
                chunker.resolve(request, use_extension=use_extension)
                draft = ""

    async def _local_whisper(self) -> None:
        streaming = LocalWhisperStreaming()
        captions = LocalCaptions(start_id=self.segment + 1)
        loop = asyncio.get_running_loop()
        prompt, window_id = "", None
        while self.active:
            pcm = await self._next_local_audio()
            if not self.active:
                return
            try:
                streaming.feed(pcm)
            except EngineError as exc:
                exc.buffer_seconds = streaming.buffered_seconds
                raise
            while self.active and (snapshot := streaming.snapshot()) is not None:
                if snapshot.window_id != window_id:
                    prompt, window_id = "", snapshot.window_id
                words, language = await asyncio.to_thread(
                    self.runtime.transcribe_stream, snapshot.pcm, self.language,
                    "" if getattr(snapshot, "context_in_audio", False) else prompt)
                self._check_audio_generation()
                if not self.active:
                    return
                try:
                    result = streaming.accept(snapshot, words)
                except EngineError as exc:
                    exc.buffer_seconds = streaming.buffered_seconds
                    raise
                if result.committed_text:
                    prompt = (prompt + " " + result.committed_text).strip()[-500:]
                if getattr(result, "reset_prompt", False):
                    prompt = ""
                for code in getattr(result, "warnings", ()):
                    if code == "local_streaming_deadline":
                        await self.emit({"type": "warning", "code": code,
                            "message": "음성 인식이 지연된 구간은 마지막 인식 결과로 처리하고 계속 진행합니다."})
                    elif code == "local_streaming_empty_final":
                        await self.emit({"type": "warning", "code": code,
                            "message": "추가 오디오 구간에서 대사를 인식하지 못했습니다. 다음 음성을 계속 처리합니다."})
                    elif code == "local_streaming_pending_revised":
                        await self.emit({"type": "warning", "code": code,
                            "message": "이전 잠정 인식이 최신 결과에서 확인되지 않아 정정했습니다. 다음 음성 인식을 계속합니다."})
                # Show a short translated hypothesis early, then revise/finalize
                # that same ID. The ASR controller remains the authority for
                # committed speech and retains its original audio context.
                updates = captions.accept(snapshot, result, loop.time())
                for update in await self._apply_sentences(updates, language):
                    captions.defer_preview(update.segment_id, update.text)
                # Keep the main window's raw recognition preview responsive,
                # including the first second and translation-throttled updates.
                if result.pending_text and not any(
                        not update.is_final and update.text == result.pending_text for update in updates):
                    await self.emit({"type": "transcript", "segment_id": self.segment + 1,
                                     "text": result.pending_text, "is_final": False, "preview": True})

    async def _translate(self) -> None:
        while self.active:
            segment, work = await self.texts.get()
            self._translation_inflight = (segment, work)
            try:
                await self._translate_sentence(segment, work)
            finally:
                self._translation_inflight = None
                self.texts.task_done()

    def _remember_context(self, segment: int, work: Translation) -> None:
        # Provisional recognizer output can still change. Only successful final
        # source text may influence the next sentence, never model-generated text.
        if not work.is_final or work.failed or work.context_epoch != self._context_epoch:
            return
        self._context[segment] = work.source
        ordered = sorted(self._context, key=self._sentence_order.__getitem__)
        self._context = {key: self._context[key] for key in ordered[-3:]}

    async def _translate_sentence(self, segment: int, work: Translation) -> None:
        if self._translations.get(segment) is not work:
            return
        order = self._sentence_order[segment]
        ordered = sorted(self._context, key=self._sentence_order.__getitem__)
        context = ([self._context[key] for key in ordered if self._sentence_order[key] < order][-3:]
                   if work.context_epoch == self._context_epoch else [])
        while self.active and self._translations.get(segment) is work:
            began_final = work.is_final
            began = time.monotonic()
            try:
                work.result = await self.runtime.translate(work.source, work.language, context,
                                                           target_language=self.target_language)
                work.failed = False
            except EngineError as exc:
                # Model/transport failures still stop the session. A single
                # sentence's output quality must not cancel ongoing recognition.
                if exc.code not in {"translation_language", "translation_empty", "translation_truncated"}:
                    raise
                if not self.active or self._translations.get(segment) is not work:
                    return
                if not began_final and work.is_final:
                    # The final arrived during a failed provisional attempt.
                    # Retry it once now: no further final event may arrive.
                    continue
                work.failed = True
                self._context.pop(segment, None)
                if self.mode == "local" and not work.is_final:
                    # An incomplete phrase can fail the output language check.
                    # Keep the last readable caption; retry on finalization.
                    work.result = None
                    break
                work.result = "[번역하지 못했습니다]"
                await self._caption(segment, work)
                await self._translation_warning(segment, work, exc.code)
                break
            finally:
                self.record_timing("translation", time.monotonic() - began)
            if not self.active or self._translations.get(segment) is not work:
                return
            self._remember_context(segment, work)
            await self._caption(segment, work)
            break
        if work.is_final and self._translations.get(segment) is work:
            self._translations.pop(segment, None)

    async def _open_gemini(self) -> None:
        # No provider URI, payload, or exceptions are logged; URI contains API key.
        from websockets.asyncio.client import connect
        quiet = logging.getLogger("livesubtitle.gemini.transport")
        quiet.handlers = [logging.NullHandler()]
        quiet.propagate = False
        quiet.setLevel(logging.CRITICAL)
        key = quote(self.settings.key(), safe="")
        uri = ("wss://generativelanguage.googleapis.com/ws/"
               "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=" + key)
        languages = {"en": "en-US", "zh": "cmn-Hans-CN", "ja": "ja-JP", "ko": "ko-KR"}
        try:
            self.gemini = await connect(uri, open_timeout=20, max_size=1024 * 1024,
                                        max_queue=16, ping_interval=20, logger=quiet, proxy=None)
            await self.gemini.send(json.dumps({"setup": {"model": "models/gemini-3.5-transcribe-live",
                "generationConfig": {"responseModalities": ["TEXT"]},
                "realtimeInputConfig": {"automaticActivityDetection": {"silenceDurationMs": 500}},
                "inputAudioTranscription": {"languageCodes": [] if self.language == "auto" else [languages[self.language]]}}}))
            response = json.loads(await asyncio.wait_for(self.gemini.recv(), 20))
            if "setupComplete" not in response and "setup_complete" not in response:
                raise ValueError("setup rejected")
        except Exception as exc:
            if self.gemini:
                await self.gemini.close()
                self.gemini = None
            raise EngineError("gemini_connect_failed", "Gemini 연결 실패: API 키, 모델 사용 권한, 네트워크를 확인해 주세요.", 503) from exc

    async def _gemini_send(self) -> None:
        while self.active:
            data = await self.audio.get()
            if not self.active:
                return
            # Keep recognition context continuous. Punctuation and timers belong
            # to text presentation; neither is an audio-stream boundary.
            await self.gemini.send(json.dumps({"realtimeInput": {"audio": {
                "data": base64.b64encode(data).decode("ascii"), "mimeType": "audio/pcm;rate=16000"}}}))

    async def _gemini_receive(self) -> None:
        loop = asyncio.get_running_loop()
        iterator = self.gemini.__aiter__()
        reader = asyncio.create_task(anext(iterator))
        sentences = StreamingSentences()
        try:
            while self.active:
                ready, _ = await asyncio.wait({reader}, timeout=.1)
                if not self.active:
                    return
                if not ready:
                    await self._apply_sentences(sentences.flush_due(loop.time()))
                    continue
                try:
                    raw = reader.result()
                except StopAsyncIteration:
                    break
                await self._gemini_message(raw, sentences, loop.time())
                await self._apply_sentences(sentences.flush_due(loop.time()))
                reader = asyncio.create_task(anext(iterator))
        finally:
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if self.active:
            raise EngineError("gemini_disconnected", "Gemini 연결이 종료되었습니다. 자막을 다시 시작해 주세요.", 503)

    async def _gemini_message(self, raw: str, sentences: StreamingSentences, now: float) -> None:
        message = json.loads(raw)
        if "error" in message:
            raise EngineError("gemini_provider_error", "Gemini가 처리를 거부했습니다. 사용량 제한과 모델 권한을 확인해 주세요.", 503)
        if "goAway" in message or "go_away" in message:
            raise EngineError("gemini_session_expiring", "Gemini 세션 시간이 만료됩니다. 자막을 다시 시작해 주세요.", 409)
        content = message.get("serverContent", message.get("server_content", {}))
        interim = content.get("interimInputTranscription", content.get("interim_input_transcription", {}))
        final = content.get("inputTranscription", content.get("input_transcription", {}))
        if "inputTranscription" in content or "input_transcription" in content:
            updates = sentences.finalize(final.get("text") or "")
            # A final can insert a sentence before an already allocated ID.
            # Translation context follows source order, never numeric ID order.
            for index, update in enumerate(item for item in updates if not item.removed):
                self._sentence_order[update.segment_id] = (self._turn, index)
            await self._apply_sentences(updates)
            self._turn += 1
        # Both fields can be present. The next hypothesis must not be discarded
        # merely because the preceding finalized transcript shares its envelope.
        if "interimInputTranscription" in content or "interim_input_transcription" in content:
            interim_text = interim.get("text") or ""
            if len(interim_text) > 6000:
                raise EngineError("transcript_too_long", "음성 구간이 너무 깁니다. 자막을 다시 시작해 주세요.")
            await self._apply_sentences(sentences.observe(interim_text, now))
            if interim_text:
                await self.emit({"type": "transcript", "segment_id": self.segment + 1,
                                 "text": interim_text, "is_final": False, "preview": True})

    async def stop(self, notify: bool = True) -> None:
        if not self.active:
            await self.manager.remove(self)
            return
        self.active = False  # Invalidate results before cancelling in-flight inference.
        current = asyncio.current_task()
        pending = [task for task in self.tasks if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.gemini:
            try:
                await asyncio.wait_for(self.gemini.close(), 3)
            except Exception:
                pass
            self.gemini = None
        await self.manager.remove(self)
        if notify:
            try:
                async with self.send_lock:
                    await asyncio.wait_for(self.ws.send_json({"type": "stopped", "session_id": self.id}), 2)
            except Exception:
                pass
