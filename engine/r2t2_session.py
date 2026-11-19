"""Incremental R2T2 transcripts and revisable translations for local live audio."""
from __future__ import annotations

import asyncio

from .revisable_captions import RevisableCaptions as R2T2Captions
from .asr_warmup import finish_thread_before_cancel
from .stable_sessions import AlignPacket, AlignPackets, drive


class R2T2Packets(AlignPackets):
    packet_bytes = 10240  # 320 ms of mono PCM16 at 16 kHz
    overrun_code = "local_streaming_overrun"
    overrun_message = "R2T2 처리가 음성 속도를 따라가지 못해 인식을 다시 연결합니다."

    @property
    def buffered_seconds(self):
        prefix_bytes = sum(len(frame) for frame in self.prefix)
        return (self.queued_bytes + len(self.pending) + len(self.frames) + prefix_bytes) / 32000

    def snapshot(self):
        packet = super().snapshot()
        if packet is None or packet.is_final or not self.ready:
            return packet
        # Native decoding works over the growing utterance. If the GPU ran
        # behind, skip outdated preview points while preserving every sample.
        parts = [packet.pcm]
        while self.ready and not packet.is_final:
            packet = super().snapshot()
            parts.append(packet.pcm)
        return AlignPacket(b"".join(parts), packet.is_final)


async def run(session):
    packets = R2T2Packets()
    captions = R2T2Captions()
    stream = None
    clock = asyncio.get_running_loop().time

    def close():
        nonlocal stream
        if stream is not None:
            finished, stream = stream, None
            session.runtime.close_r2t2_stream(finished)

    def decode(packet):
        nonlocal stream
        if stream is None:
            stream = session.runtime.create_r2t2_stream(session.language)
        hypothesis, stable = session.runtime.r2t2_step(stream, packet.pcm, final=packet.is_final)
        detected = getattr(stream, "language", None)
        language = detected if session.language == "auto" and detected else session.language
        if packet.is_final:
            close()
        return hypothesis, stable, language

    async def recognize(packet):
        # Closing or reusing a stream before its native call returns can race
        # with a GPU worker after a stop or an audio gap.
        return await finish_thread_before_cancel(decode, packet)

    async def accept(packet, recognized, duration):
        hypothesis, stable, language = recognized
        await captions.publish(session, hypothesis, stable, language, packet.is_final, clock())

    try:
        await drive(session, packets, packets.snapshot, recognize, accept)
    finally:
        await finish_thread_before_cancel(close)
