"""Early silence inference never advances the authoritative Whisper frontier."""
import struct

import pytest

from engine.local_streaming import ASRSnapshot, ASRWord, LocalWhisperStreaming
from engine.settings import EngineError


def pcm(seconds, amplitude=1000):
    return struct.pack("<h", amplitude) * round(seconds * 16000)


def early(controller, voice=.4, silence=.2):
    controller.feed(pcm(voice))
    controller.feed(pcm(silence, 0))
    snapshot = controller.snapshot()
    assert snapshot is not None and snapshot.speculative and not snapshot.is_final
    return snapshot


def finish(controller, seconds=.3):
    controller.feed(pcm(seconds, 0))
    snapshot = controller.snapshot()
    assert snapshot is not None and snapshot.is_final and not snapshot.speculative
    return snapshot


def test_old_snapshot_constructor_and_default_policy_remain_compatible():
    snapshot = ASRSnapshot(1, 1, pcm(.4), 0, False, 20)
    assert not snapshot.speculative and snapshot.cached_words is None
    controller = LocalWhisperStreaming()
    controller.feed(pcm(.4) + pcm(.2, 0))
    assert controller.snapshot() is None
    final = finish(controller)
    assert final.cached_words is None
    assert controller.accept(final, [ASRWord(0, .4, "Yes.")]).segments == ("Yes.",)


def test_early_snapshot_at_200ms_bypasses_regular_one_second_interval():
    controller = LocalWhisperStreaming(early_transcription=True)
    controller.feed(pcm(.1) + pcm(.18, 0))
    assert controller.snapshot() is None
    controller.feed(pcm(.02, 0))
    snapshot = controller.snapshot()
    assert snapshot.speculative and snapshot.speech_revision == 5
    assert snapshot.last_voiced_end == 1600
    assert snapshot.pcm == pcm(.1) + pcm(.2, 0)


def test_speculative_accept_preserves_pcm_and_all_stability_state():
    controller = LocalWhisperStreaming(early_transcription=True)
    snapshot = early(controller)
    window = controller._windows[0]
    before = bytes(window.pcm), window.offset_samples, window.last_snapshot_end
    result = controller.accept(snapshot, [ASRWord(0, .4, "Yes.")])
    assert result.segments == () and result.committed_text == result.pending_text == ""
    assert before == (bytes(window.pcm), window.offset_samples, window.last_snapshot_end)
    assert window.previous_words == window.pending_words == ()
    assert window.previous_voiced_frames == 0 and not window.had_recognized_words
    assert controller.snapshot() is None
    controller.feed(pcm(.28, 0))
    assert controller.snapshot() is None
    final = finish(controller, .02)
    assert final.cached_words == (ASRWord(0, .4, "Yes."),)
    result = controller.accept(final, final.cached_words)
    assert result.segments == ("Yes.",) and controller.buffered_seconds == 0
    assert controller.snapshot() is None


def test_final_arriving_while_asr_runs_gets_one_cached_final_after_accept():
    controller = LocalWhisperStreaming(early_transcription=True)
    snapshot = early(controller)
    controller.feed(pcm(.3, 0))
    assert controller.snapshot() is None
    assert controller.accept(snapshot, [ASRWord(0, .4, "Yes.")]).segments == ()
    final = controller.snapshot()
    assert final.is_final and final.cached_words
    assert controller.accept(final, final.cached_words).segments == ("Yes.",)
    with pytest.raises(EngineError, match="지난 음성 인식"):
        controller.accept(final, final.cached_words)


def test_speech_resumes_during_inference_so_late_speculation_is_not_an_observation():
    controller = LocalWhisperStreaming(early_transcription=True)
    snapshot = early(controller)
    controller.feed(pcm(.6, 2000))
    retained = bytes(controller._windows[0].pcm)
    assert controller.accept(snapshot, [ASRWord(0, .4, "Wrong.")]).segments == ()
    current = controller.snapshot()
    assert current and not current.speculative and current.cached_words is None
    assert current.pcm == retained == snapshot.pcm + pcm(.6, 2000)
    # Matching the discarded early text is still only the first observation.
    result = controller.accept(current, [ASRWord(0, .4, "Wrong.")])
    assert result.segments == () and result.pending_text == "Wrong."


def test_speech_resumes_after_cached_result_and_final_retries_full_audio():
    controller = LocalWhisperStreaming(early_transcription=True)
    snapshot = early(controller)
    controller.accept(snapshot, [ASRWord(0, .4, "He")])
    controller.feed(pcm(.28, 0) + pcm(.2, 2000))
    final = finish(controller, .5)
    assert final.cached_words is None
    assert final.pcm == snapshot.pcm + pcm(.28, 0) + pcm(.2, 2000) + pcm(.5, 0)
    result = controller.accept(final, [ASRWord(0, 1.08, "He wasn't.")])
    assert result.segments == ("He wasn't.",)


def test_new_pause_can_speculate_again_after_speech_resumes():
    controller = LocalWhisperStreaming(early_transcription=True)
    first = early(controller)
    controller.accept(first, [ASRWord(0, .4, "He")])
    controller.feed(pcm(.2) + pcm(.2, 0))
    second = controller.snapshot()
    assert second.speculative and second.speech_revision > first.speech_revision
    assert second.pcm.startswith(first.pcm)
    controller.accept(second, [ASRWord(0, .8, "He wasn't.")])
    final = finish(controller)
    assert controller.accept(final, final.cached_words).segments == ("He wasn't.",)


@pytest.mark.parametrize("empty", [[], [ASRWord(0, .1, "   ")]])
def test_empty_speculation_always_retries_final(empty):
    controller = LocalWhisperStreaming(early_transcription=True)
    snapshot = early(controller)
    controller.accept(snapshot, empty)
    final = finish(controller)
    assert final.cached_words is None
    assert controller.accept(final, [ASRWord(0, .4, "はい。")]).segments == ("はい。",)


def test_empty_speculation_finished_during_inference_still_retries():
    controller = LocalWhisperStreaming(early_transcription=True)
    snapshot = early(controller)
    controller.feed(pcm(.3, 0))
    controller.accept(snapshot, [])
    assert controller.snapshot().cached_words is None


def test_next_window_speech_does_not_invalidate_previous_finished_window_cache():
    controller = LocalWhisperStreaming(early_transcription=True)
    first = early(controller)
    controller.feed(pcm(.3, 0) + pcm(.4, 2000) + pcm(.5, 0))
    controller.accept(first, [ASRWord(0, .4, "Yes.")])
    final = controller.snapshot()
    assert final.window_id == first.window_id and final.cached_words
    one = controller.accept(final, final.cached_words)
    second = controller.snapshot()
    assert second.is_final and second.window_id != final.window_id
    assert second.cached_words is None
    two = controller.accept(second, [ASRWord(.4, .8, "Yes.")])
    assert one.segments + two.segments == ("Yes.", "Yes.")
    assert controller.buffered_seconds == 0


def test_cached_timestamps_remain_relative_after_previous_commit_trim():
    controller = LocalWhisperStreaming(early_transcription=True)
    controller.feed(pcm(1))
    controller.accept(controller.snapshot(), [ASRWord(.1, .6, "First.")])
    controller.feed(pcm(1))
    result = controller.accept(controller.snapshot(), [ASRWord(.1, .6, "First."), ASRWord(.8, 1.8, " Second.")])
    assert result.segments == ("First.",)
    assert controller._windows[0].offset_samples == 9600
    controller.feed(pcm(.2, 0))
    snapshot = controller.snapshot()
    assert snapshot.speculative and snapshot.offset_seconds == pytest.approx(.6)
    controller.accept(snapshot, [ASRWord(.2, 1.2, "Second.")])
    final = finish(controller)
    assert final.offset_seconds == snapshot.offset_seconds
    assert final.cached_words[0].start == pytest.approx(.2)
    assert final.cached_words[0].end == pytest.approx(1.2)
    assert controller.accept(final, final.cached_words).segments == ("Second.",)


def test_speculation_does_not_supply_second_stability_observation_after_restart_of_speech():
    controller = LocalWhisperStreaming(early_transcription=True)
    snapshot = early(controller)
    controller.accept(snapshot, [ASRWord(0, .4, "First.")])
    controller.feed(pcm(.4))
    regular = controller.snapshot()
    assert not regular.speculative
    result = controller.accept(regular, [ASRWord(0, .4, "First.")])
    assert result.segments == ()


def test_silence_only_remains_bounded_without_speculative_work():
    controller = LocalWhisperStreaming(early_transcription=True)
    controller.feed(pcm(60, 0))
    assert controller.snapshot() is None and controller.buffered_seconds == 0
    assert len(controller._prefix) == 20


def test_pcm_packets_and_new_speech_in_same_packet_invalidate_early_result():
    controller = LocalWhisperStreaming(early_transcription=True)
    snapshot = early(controller)
    resumed = pcm(.28, 0) + pcm(.12, 80)
    for index in range(0, len(resumed), 94):
        controller.feed(resumed[index:index + 94])
    controller.accept(snapshot, [ASRWord(0, .4, "Old.")])
    final = finish(controller, .5)
    assert final.cached_words is None
    assert final.pcm == snapshot.pcm + resumed + pcm(.5, 0)


def test_speculative_words_are_validated_before_caching():
    controller = LocalWhisperStreaming(early_transcription=True)
    snapshot = early(controller)
    with pytest.raises(EngineError) as error:
        controller.accept(snapshot, [ASRWord(0, 20, "Invalid.")])
    assert error.value.code == "asr_timestamps"
