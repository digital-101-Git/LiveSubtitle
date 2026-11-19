"""Streaming caption adapters with audio ingestion independent of inference."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import math
import struct
import time

from .local_captions import LocalCaptions
from .local_streaming import LocalWhisperStreaming
from .qwen_revisions import reconcile
from .qwen_streaming import QwenStreaming
from .sentences import completed_sentence_prefix, split_sentences
from .settings import EngineError
from .streaming_sentences import SentenceUpdate


async def drive(session, controller, next_snapshot, recognize, accept, *, on_audio_gap=None, on_feed=None):
    """Feed while an immutable snapshot is on the GPU; never parallelize ASR.

    Both workers mutate controller state only on this event loop. Cancellation
    discards late inference output; Runtime's ASR lock protects model teardown.
    Translation backpressure cannot pause ingestion until its explicit bound.
    """
    changed = asyncio.Event()
    generation = getattr(session, "audio_generation", 0)
    dropped_samples = getattr(session, "audio_dropped_samples", 0)

    def check_gap(pcm=None):
        nonlocal generation, dropped_samples
        current = getattr(session, "audio_generation", 0)
        if current == generation:
            return
        total = session.audio_dropped_samples
        if on_audio_gap is None:
            if pcm is not None:
                session.defer_audio(pcm)
            error = EngineError("audio_gap_restart", "음성 입력 구간을 건너뛰고 인식을 다시 연결합니다.")
            error.buffer_seconds = getattr(controller, "buffered_seconds", 0)
            raise error
        on_audio_gap(total - dropped_samples)
        generation, dropped_samples = current, total

    async def ingest():
        while session.active:
            pcm = await (session.next_audio() if hasattr(session, "next_audio") else session.audio.get())
            if not session.active:
                return
            check_gap(pcm)
            try:
                controller.feed(pcm)
            except EngineError as exc:
                exc.buffer_seconds = getattr(controller, "buffered_seconds", 0)
                raise
            if on_feed is not None:
                await on_feed()
            changed.set()

    async def infer():
        while session.active:
            await changed.wait()
            changed.clear()
            check_gap()
            while session.active and (snapshot := next_snapshot()) is not None:
                began = time.monotonic()
                result = await recognize(snapshot)
                if not session.active:
                    return
                if hasattr(session, "record_timing"):
                    session.record_timing("asr", time.monotonic() - began)
                check_gap()
                await accept(snapshot, result, time.monotonic() - began)

    workers = [asyncio.create_task(ingest(), name="streaming-pcm-ingest"),
               asyncio.create_task(infer(), name="streaming-asr-infer")]
    try:
        await asyncio.gather(*workers)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        stop = getattr(controller, "stop", None)
        if stop:
            stop()


async def warnings(session, codes):
    for code in codes:
        if code.endswith("deadline"):
            message = "긴 음성 구간을 현재 인식 결과로 확정하고 다음 음성을 계속 처리합니다."
        elif code.endswith("empty_final"):
            message = "이 음성 구간의 인식 결과가 비어 있습니다. 다음 음성을 계속 처리합니다."
        else:
            message = "뒤 음성과 비교하면서 인식 내용을 정정했습니다. 다음 음성 인식을 계속합니다."
        await session.emit({"type": "warning", "code": code, "message": message})


async def whisper(session):
    controller = LocalWhisperStreaming(early_transcription=True)
    captions = LocalCaptions(start_id=session.segment + 1)
    prompt, window_id, cached_language = "", None, session.language

    async def recognize(snapshot):
        nonlocal prompt, window_id, cached_language
        if window_id != snapshot.window_id:
            prompt, window_id, cached_language = "", snapshot.window_id, session.language
        if snapshot.cached_words is not None:
            return snapshot.cached_words, cached_language
        words, language = await asyncio.to_thread(
            session.runtime.transcribe_stream, snapshot.pcm, session.language,
            "" if snapshot.context_in_audio else prompt)
        cached_language = language
        return words, language

    async def accept(snapshot, recognized, duration):
        nonlocal prompt
        words, language = recognized
        result = controller.accept(snapshot, words)
        if result.committed_text:
            prompt = (prompt + " " + result.committed_text).strip()[-500:]
        if result.reset_prompt:
            prompt = ""
        await warnings(session, result.warnings)
        # A speculative silence decode only fills the ASR cache. Its empty
        # result is not a withdrawal of an already visible hypothesis.
        if not snapshot.speculative:
            updates = captions.accept(snapshot, result, time.monotonic(), agreed_pending=result.agreed_pending)
            if captions.preview_id is not None and session.observe_draft(
                    captions.preview_id, result.pending_text, language):
                captions.defer_preview(captions.preview_id)
            for update in await session._apply_sentences(updates, language):
                captions.defer_preview(update.segment_id, update.text)
            if result.pending_text and not snapshot.is_final:
                await session.emit({"type": "transcript", "segment_id": captions.preview_id or session.segment + 1,
                                    "text": result.pending_text, "is_final": False, "preview": True})

    await drive(session, controller, controller.snapshot, recognize, accept)


class QwenWindowCaptions:
    """Keep committed clause IDs while revising the current unfinished clause."""

    def __init__(self):
        self.confirmed = ()
        self.draft = ()
        self.offered = None
        self.last_offer = float("-inf")

    async def publish(self, session, window_id, result, language, now):
        # An authoritative full-window correction may even insert speech before a
        # previously confirmed clause. Ordinary updates may only revise the tail.
        previous = self.confirmed + self.draft
        previous_confirmed = dict(self.confirmed)
        if result.replacement_segments is not None:
            revision = reconcile(previous, result.replacement_segments, session.segment)
            confirmed, draft = revision.captions, ()
        else:
            segments = result.segments
            raw_parts = split_sentences(result.pending_text)
            if self.draft and not segments and not result.window_closed:
                if session.observe_draft(self.draft[0][0], raw_parts[0] if raw_parts else "", language):
                    self.offered = None
            pending = split_sentences(result.agreed_pending)[:1] if not result.window_closed else []
            if raw_parts and not pending and not segments and not result.window_closed:
                # This first/changed observation can update the source preview.
                # Wait for independent agreement before starting translation.
                return
            if (pending and now - self.last_offer < LocalCaptions.update_interval_seconds
                    and not segments):
                return
            revision = reconcile(self.draft, (*segments, *pending), session.segment)
            confirmed = self.confirmed + revision.captions[:len(segments)]
            draft = revision.captions[len(segments):]

        session.segment = revision.last_id
        # Install chronological order and invalidate changed prior context
        # before yielding to the asynchronous translation worker.
        for index, (identifier, source) in enumerate(confirmed + draft):
            session._sentence_order[identifier] = (window_id, index)
            if identifier in previous_confirmed and previous_confirmed[identifier] != source:
                session._context.pop(identifier, None)
        updates = [SentenceUpdate(identifier, source, True, removed=True)
                   for identifier, source in revision.removed]
        updates.extend(SentenceUpdate(identifier, source, True) for identifier, source in confirmed
                       if previous_confirmed.get(identifier) != source)
        offered = (draft[0][0], draft[0][1], language) if draft else None
        if offered and offered != self.offered:
            updates.append(SentenceUpdate(draft[0][0], draft[0][1], False))
            self.last_offer = now
        self.confirmed, self.draft = confirmed, draft
        deferred = await session._apply_sentences(updates, language)
        self.offered = None if deferred else offered


async def qwen(session):
    detector = await asyncio.to_thread(session.runtime.create_voice_detector) if hasattr(session.runtime, "create_voice_detector") else None
    controller = QwenStreaming(voiced_detector=detector)
    captions = QwenWindowCaptions()

    async def recognize(snapshot):
        if snapshot.cached_text is not None:
            return snapshot.cached_text, snapshot.cached_language
        return await asyncio.to_thread(session.runtime.transcribe, snapshot.pcm, session.language)

    async def accept(snapshot, recognized, duration):
        nonlocal captions
        text, language = recognized
        result = controller.accept(snapshot, text, language, inference_seconds=duration)
        language = result.language if session.language == "auto" else session.language
        # An invalidated early-silence observation supplies no new text.
        if not (getattr(snapshot, "speculative", False) and not result.window_closed and not result.pending_text):
            await captions.publish(session, snapshot.window_id, result, language, time.monotonic())
            if result.pending_text and not result.window_closed:
                identifier = captions.draft[0][0] if captions.draft else session.segment + 1
                await session.emit({"type": "transcript", "segment_id": identifier, "text": result.pending_text,
                                    "is_final": False, "preview": True})
        await warnings(session, result.warnings)
        if result.window_closed:
            captions = QwenWindowCaptions()

    await drive(session, controller, controller.next_snapshot, recognize, accept)


@dataclass(frozen=True)
class AlignPacket:
    pcm: bytes
    is_final: bool


class AlignPackets:
    """Incremental packets, actual silence endpoints, no text-based PCM cuts."""
    packet_bytes = 32000
    overrun_code = "alignatt_audio_overrun"
    overrun_message = "Whisper AlignAtt 처리가 음성 속도를 따라가지 못해 중지했습니다."

    def __init__(self, voiced_detector=None):
        self.frames = bytearray()
        self.prefix = deque(maxlen=20)
        self.pending = bytearray()
        self.ready = deque()
        self.started = False
        self.silent = 0
        self.window_frames = 0
        self.queued_bytes = 0
        self.voiced_detector = voiced_detector

    def feed(self, pcm):
        self.frames.extend(pcm)
        while len(self.frames) >= 640:
            frame = bytes(self.frames[:640])
            del self.frames[:640]
            samples = struct.unpack("<320h", frame)
            voiced = (bool(self.voiced_detector(frame)) if self.voiced_detector is not None else
                      math.sqrt(sum(x*x for x in samples) / 320) / 32768 >= .002)
            if not self.started:
                if not voiced:
                    self.prefix.append(frame)
                    continue
                self.pending.extend(b"".join(self.prefix))
                self.prefix.clear()
                self.started = True
            self.pending.extend(frame)
            self.window_frames += 1
            self.silent = 0 if voiced else self.silent + 1
            final = self.silent >= 25 or self.window_frames >= 600
            if final or len(self.pending) >= self.packet_bytes:
                packet = AlignPacket(bytes(self.pending), final)
                self.ready.append(packet)
                self.queued_bytes += len(packet.pcm)
                self.pending.clear()
                if self.queued_bytes > 30 * 32000 or len(self.ready) > 80:
                    raise EngineError(self.overrun_code, self.overrun_message, 409)
            if final:
                self.started = False
                self.silent = self.window_frames = 0

    def snapshot(self):
        if not self.ready:
            return None
        packet = self.ready.popleft()
        self.queued_bytes -= len(packet.pcm)
        return packet

    def stop(self):
        self.frames.clear()
        self.pending.clear()
        self.prefix.clear()
        self.ready.clear()


_CLOSING_MARKS = ".,!?;:、。！？…，；：)]}»›”’」』】）》〉〕〗〙〛"


def _closing_marks_only(text):
    return bool(text.strip()) and all(char.isspace() or char in _CLOSING_MARKS for char in text)


class CommittedPhrases:
    """Group attention-committed words without requiring ASR punctuation.

    Word times are attention estimates. They only select text boundaries; PCM
    ownership and decoder commit positions are never inferred from characters.
    """
    def __init__(self):
        self.words = []

    def accept(self, words, final=False):
        output = []

        def flush():
            text = "".join(word.text for word in self.words).strip()
            self.words.clear()
            if text:
                output.append(text)

        for word in words:
            if self.words:
                text = "".join(item.text for item in self.words)
                substantial = sum(char.isalnum() for char in text) >= 6
                pause = word.start - self.words[-1].end >= .3
                duration = self.words[-1].end - self.words[0].start
                if substantial and (pause or duration >= 3.0) and not _closing_marks_only(word.text):
                    flush()
            self.words.append(word)
            text = "".join(item.text for item in self.words).strip()
            if completed_sentence_prefix(text) == text:
                flush()
        if final:
            flush()
        return output


async def alignatt(session):
    from .revisable_captions import RevisableCaptions

    controller = AlignPackets()
    stream = session.runtime.create_alignatt_stream(session.language)
    phrases = CommittedPhrases()
    captions = RevisableCaptions()
    last_final = None

    async def recognize(snapshot):
        return await asyncio.to_thread(session.runtime.transcribe_alignatt, stream, snapshot.pcm, snapshot.is_final)

    async def accept(snapshot, recognized, duration):
        nonlocal last_final
        words, language = recognized
        now = time.monotonic()
        for complete in phrases.accept(words, snapshot.is_final):
            if last_final and _closing_marks_only(complete):
                identifier, previous, previous_language = last_final
                text = previous + complete
                await session._apply_sentences([SentenceUpdate(identifier, text, True)], previous_language)
                last_final = (identifier, text, previous_language)
            else:
                finalized = await captions.publish(session, complete, "", language, True, now)
                if finalized:
                    last_final = (*finalized[-1], language)
        if phrases.words:
            pending = "".join(word.text for word in phrases.words)
            if not _closing_marks_only(pending):
                await captions.publish(session, "", pending, language, False, now)
        if snapshot.is_final:
            last_final = None

    try:
        await drive(session, controller, controller.snapshot, recognize, accept)
    finally:
        # A cancelled to_thread inference may still own the decoder.
        await asyncio.to_thread(session.runtime.close_alignatt_stream, stream)
