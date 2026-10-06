"""Final-only streaming adapters with audio ingestion independent of inference."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import math
import struct
import time

from .local_streaming import LocalWhisperStreaming
from .qwen_streaming import QwenStreaming
from .sentences import completed_sentence_prefix
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
                on_feed()
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
        for text in result.segments:
            await session._final(text, language)

    await drive(session, controller, controller.snapshot, recognize, accept)


async def qwen(session):
    from .qwen_revisions import reconcile

    detector = await asyncio.to_thread(session.runtime.create_voice_detector) if hasattr(session.runtime, "create_voice_detector") else None
    controller = QwenStreaming(voiced_detector=detector)
    published = {}

    async def recognize(snapshot):
        if snapshot.cached_text is not None:
            return snapshot.cached_text, snapshot.cached_language
        return await asyncio.to_thread(session.runtime.transcribe, snapshot.pcm, session.language)

    async def accept(snapshot, recognized, duration):
        text, language = recognized
        result = controller.accept(snapshot, text, language, inference_seconds=duration)
        language = result.language if session.language == "auto" else session.language
        old = published.setdefault(snapshot.window_id, [])
        if result.replacement_segments is not None:
            revised = reconcile(old, result.replacement_segments, session.segment)
            previous_sources = dict(old)
            session.segment = revised.last_id
            # Publish the complete final order before any awaited translation.
            # Inserted speech has a fresh ID even when its place is at the front.
            for index, (identifier, source) in enumerate(revised.captions):
                session._sentence_order[identifier] = (snapshot.window_id, index)
                if previous_sources.get(identifier) != source:
                    session._context.pop(identifier, None)
            for identifier, previous in revised.removed:
                await session._apply_sentences([SentenceUpdate(identifier, previous, True, removed=True)], language)
            for identifier, source in revised.captions:
                if previous_sources.get(identifier) != source:
                    await session._queue_translation(identifier, source, language, True)
            old[:] = revised.captions
        else:
            for source in result.segments:
                session.segment += 1
                identifier = session.segment
                session._sentence_order[identifier] = (snapshot.window_id, len(old))
                old.append((identifier, source))
                await session._queue_translation(identifier, source, language, True)
        await warnings(session, result.warnings)
        if result.window_closed:
            published.pop(snapshot.window_id, None)

    await drive(session, controller, controller.next_snapshot, recognize, accept)


@dataclass(frozen=True)
class AlignPacket:
    pcm: bytes
    is_final: bool


class AlignPackets:
    """Incremental packets, actual silence endpoints, no text-based PCM cuts."""
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
            if final or len(self.pending) >= 32000:
                packet = AlignPacket(bytes(self.pending), final)
                self.ready.append(packet)
                self.queued_bytes += len(packet.pcm)
                self.pending.clear()
                if self.queued_bytes > 30 * 32000 or len(self.ready) > 80:
                    raise EngineError("alignatt_audio_overrun", "Whisper AlignAtt 처리가 음성 속도를 따라가지 못해 중지했습니다.", 409)
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
                if substantial and (pause or duration >= 3.0):
                    flush()
            self.words.append(word)
            text = "".join(item.text for item in self.words).strip()
            if completed_sentence_prefix(text) == text:
                flush()
        if final:
            flush()
        return output


async def alignatt(session):
    controller = AlignPackets()
    stream = session.runtime.create_alignatt_stream(session.language)
    phrases = CommittedPhrases()

    async def recognize(snapshot):
        return await asyncio.to_thread(session.runtime.transcribe_alignatt, stream, snapshot.pcm, snapshot.is_final)

    async def accept(snapshot, recognized, duration):
        words, language = recognized
        for complete in phrases.accept(words, snapshot.is_final):
            await session._final(complete, language)

    try:
        await drive(session, controller, controller.snapshot, recognize, accept)
    finally:
        # A cancelled to_thread inference may still own the decoder.
        await asyncio.to_thread(session.runtime.close_alignatt_stream, stream)
