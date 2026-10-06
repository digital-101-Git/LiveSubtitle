import struct

import pytest

from engine.audio import PCMChunker
from engine.fast_qwen import FastQwenController


def pcm(seconds, value=1000):
    return struct.pack('<h', value) * round(seconds * 16000)


def feed(controller, audio):
    for offset in range(0, len(audio), 3200):
        controller.feed(audio[offset:offset+3200])


def drain(controller, text='你好。', language='zh'):
    results, snapshots = [], []
    while (snapshot := controller.next_snapshot()) is not None:
        snapshots.append(snapshot)
        results.append(controller.accept(snapshot, text, language))
    return snapshots, results


@pytest.mark.parametrize('detector', [None, lambda frame: False])
def test_continuous_audio_keeps_exact_legacy_four_seconds_and_overlap(detector):
    current = FastQwenController(voiced_detector=detector)
    old = PCMChunker()
    expected, actual = [], []
    audio = b''.join(pcm(.1, value) for value in range(500, 630))
    for offset in range(0, len(audio), 3200):
        packet = audio[offset:offset+3200]
        expected.extend(old.feed(packet))
        current.feed(packet)
        snapshots, results = drain(current)
        actual.extend(snapshots)
        assert all(result.is_final for result in results)
    assert [(s.pcm, s.overlaps_previous) for s in actual] == [(s.pcm, s.overlaps_previous) for s in expected]
    assert [s.start_sample / 16000 for s in actual] == [0, 3.8, 7.6]
    assert current.statistics['speculative_requests'] == 0


def test_early_result_is_hidden_until_final_and_preserves_detected_language():
    controller = FastQwenController()
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    assert early.speculative and not early.is_final
    assert not controller.accept(early, 'こんにちは。', 'ja').is_final
    assert controller.next_snapshot() is None
    feed(controller, pcm(.3, 0))
    final = controller.next_snapshot()
    assert final.cached_text == 'こんにちは。' and final.cached_language == 'ja'
    result = controller.accept(final, final.cached_text, final.cached_language)
    assert result.is_final and result.language == 'ja'
    assert controller.next_snapshot() is None


def test_inflight_early_result_promotes_after_end_and_new_window_does_not_invalidate():
    controller = FastQwenController()
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    feed(controller, pcm(.3, 0) + pcm(.4, 2000))
    result = controller.accept(early, 'Yes.', 'en')
    assert result.is_final and result.text == 'Yes.'
    feed(controller, pcm(.5, 0))
    _, results = drain(controller, 'Yes.', 'en')
    assert [r.text for r in results] == ['Yes.']


@pytest.mark.parametrize('inflight', [False, True])
def test_rms_resumption_invalidates_early_even_when_neural_misses_speech(inflight):
    controller = FastQwenController(voiced_detector=lambda frame: frame == pcm(.02, 1000))
    beginning = pcm(.4) + pcm(.2, 0)
    feed(controller, beginning)
    early = controller.next_snapshot()
    if not inflight:
        controller.accept(early, 'Earlier.', 'en')
    # Neural calls this music, but positive-RMS PCM still invalidates the cache.
    resumed = pcm(.2, 2000) + pcm(.1, 0)
    feed(controller, resumed)
    if inflight:
        assert not controller.accept(early, 'Earlier.', 'en').is_final
    final = controller.next_snapshot()
    assert final.is_final and final.cached_text is None
    assert final.pcm == beginning + resumed
    assert controller.accept(final, 'Earlier. Later.', 'en').text == 'Earlier. Later.'


def test_neural_endpoint_preserves_every_positive_rms_sample_and_later_missed_speech():
    controller = FastQwenController(voiced_detector=lambda frame: frame == pcm(.02, 1000))
    first = pcm(.4) + pcm(.5, 2000)
    feed(controller, first)
    snapshot = controller.next_snapshot()
    assert snapshot.reason == 'neural_silence' and snapshot.pcm == first
    controller.accept(snapshot, 'First.')
    following = pcm(.4, 3000) + pcm(.5, 0)
    feed(controller, following)
    snapshot = controller.next_snapshot()
    assert snapshot.pcm == following
    assert not snapshot.overlaps_previous
    assert controller.accept(snapshot, 'Second.').text == 'Second.'


def test_neural_positive_silence_never_opens_audio_window():
    controller = FastQwenController(voiced_detector=lambda frame: True)
    feed(controller, pcm(30, 0))
    assert controller.next_snapshot() is None
    assert controller.buffered_seconds == .4


def test_neural_music_pause_skips_speculation_but_finalizes_full_audio_at_500ms():
    controller = FastQwenController(voiced_detector=lambda frame: frame == pcm(.02, 1000))
    feed(controller, pcm(.4) + pcm(.2, 2000))
    assert controller.next_snapshot() is None
    feed(controller, pcm(.2, 2000))
    assert controller.next_snapshot() is None
    feed(controller, pcm(.1, 2000))
    final = controller.next_snapshot()
    assert final.is_final and final.reason == 'neural_silence'
    assert final.pcm == pcm(.4) + pcm(.5, 2000)
    assert controller.accept(final, 'First.').is_final
    assert controller.statistics['speculative_requests'] == 0


def test_neural_pause_precomputes_only_after_actual_200ms_rms_quiet_and_reuses_it():
    controller = FastQwenController(voiced_detector=lambda frame: frame == pcm(.02, 1000))
    feed(controller, pcm(.4) + pcm(.1, 2000) + pcm(.18, 0))
    assert controller.next_snapshot() is None  # neural quiet .28, RMS quiet .18
    feed(controller, pcm(.02, 0))
    early = controller.next_snapshot()
    assert early.speculative
    assert not controller.accept(early, 'Warm result.', 'en').is_final
    feed(controller, pcm(.18, 0))
    assert controller.next_snapshot() is None  # one speculation per pause
    feed(controller, pcm(.02, 0))
    final = controller.next_snapshot()
    assert final.is_final and final.reason == 'neural_silence'
    assert final.cached_text == 'Warm result.' and final.cached_language == 'en'
    result = controller.accept(final, final.cached_text, final.cached_language)
    assert result.is_final and result.text == 'Warm result.'
    assert controller.statistics['speculative_requests'] == 1
    assert controller.statistics['cache_reuses'] == 1


@pytest.mark.parametrize('finishes_before_accept', [False, True])
def test_empty_early_result_requires_final_recognition(finishes_before_accept):
    controller = FastQwenController()
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    if finishes_before_accept:
        feed(controller, pcm(.3, 0))
    assert not controller.accept(early, '').is_final
    if not finishes_before_accept:
        feed(controller, pcm(.3, 0))
    final = controller.next_snapshot()
    assert final.is_final and final.cached_text is None
    assert controller.accept(final, 'Yes.').text == 'Yes.'


def test_stale_guess_is_never_resurrected_after_empty_final():
    controller = FastQwenController()
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    feed(controller, pcm(.2) + pcm(.5, 0))
    assert not controller.accept(early, 'Wrong early guess.').is_final
    final = controller.next_snapshot()
    result = controller.accept(final, '')
    assert result.is_final and result.text == ''
    assert result.warnings == ('fast_qwen_empty_final',)


def test_snapshot_is_immutable_one_request_only_and_stop_ignores_late_output():
    controller = FastQwenController()
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    saved = early.pcm
    feed(controller, pcm(.2))
    assert early.pcm == saved
    assert controller.next_snapshot() is None
    controller.stop()
    assert not controller.accept(early, 'Late.').is_final
    assert controller.next_snapshot() is None and controller.buffered_seconds == 0


def test_waiting_audio_overrun_drops_oldest_and_ignores_late_result():
    controller = FastQwenController()
    feed(controller, pcm(4))
    snapshot = controller.next_snapshot()
    for _ in range(1200):
        controller.feed(pcm(.1, 2000))
        assert controller.buffered_seconds <= controller.maximum_buffer_seconds
    assert snapshot.pcm == pcm(4)
    assert controller.recognition_pending and controller.next_snapshot() is None
    assert controller.generation > 0
    events = controller.pop_recoveries()
    assert len(events) == 1 and events[0]['stage'] == 'fast_qwen_buffer'
    assert events[0]['dropped_audio_seconds'] > 110
    assert events[0]['dropped_items'] > 20
    assert controller.pop_recoveries() == []
    assert not controller.accept(snapshot, 'Obsolete audio.').is_final
    latest = controller.next_snapshot()
    assert latest.start_sample > 110 * 16000
    assert not latest.overlaps_previous
    assert controller.accept(latest, 'Latest speech.').text == 'Latest speech.'
    assert controller._sample == 124 * 16000


def test_recovery_discards_only_required_window_and_keeps_new_packet_exact():
    controller = FastQwenController()
    feed(controller, pcm(4))
    old = controller.next_snapshot()
    feed(controller, pcm(5.6))
    assert controller.pop_recoveries() == []
    assert controller.buffered_seconds == 10
    controller.feed(pcm(.1, 2000))
    event, = controller.pop_recoveries()
    assert event['dropped_items'] == 1
    assert event['dropped_audio_seconds'] == pytest.approx(3.8)
    assert [w.identifier for w in controller._windows] == [2, 3]
    assert controller._windows[0].pcm == pcm(4)
    assert controller._active.pcm.endswith(pcm(.1, 2000))
    assert not controller._windows[0].overlaps_previous
    assert controller._active.overlaps_previous
    assert controller._sample == round(9.7 * 16000)
    # Old model output is irrelevant even if it is no longer a valid transcript.
    assert not controller.accept(old, None).is_final
    assert controller.next_snapshot().window_id == 2


def test_many_short_waiting_windows_have_separate_bound():
    controller = FastQwenController()
    for _ in range(20):
        feed(controller, pcm(.2) + pcm(.5, 0))
        assert len(controller._windows) <= controller.maximum_windows
        assert controller.buffered_seconds <= controller.maximum_buffer_seconds
    event, = controller.pop_recoveries()
    assert event['stage'] == 'fast_qwen_windows' and event['dropped_items'] == 12
    assert [w.identifier for w in controller._windows] == list(range(13, 21))
    assert controller.next_snapshot().window_id == 13


def test_input_gap_resets_state_and_advances_exact_subframe_clock():
    class Detector:
        def __init__(self):
            self.resets = 0

        def __call__(self, frame):
            return False

        def reset(self):
            self.resets += 1

    detector = Detector()
    controller = FastQwenController(voiced_detector=detector)
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    controller.feed(pcm(.01, 2000))
    controller.reset_for_gap(dropped_samples=123)
    assert controller._sample == 9600 + 160 + 123
    assert controller.generation == 1 and detector.resets == 1
    assert controller.buffered_seconds == 0
    assert not controller._pending_continuation and not controller._prefix
    assert controller.next_snapshot() is None  # Do not launch a second model call.
    new_pcm = pcm(.4, 3000) + pcm(.5, 0)
    feed(controller, new_pcm)
    assert not controller.accept(early, 'Old guess.').is_final
    new = controller.next_snapshot()
    assert new.start_sample == 9883 and new.pcm == new_pcm
    assert not new.overlaps_previous and new.cached_text is None
    assert controller.accept(new, 'New speech.').is_final


def test_recovery_at_hard_cut_accounts_for_new_overlap_without_exceeding_bound():
    controller = FastQwenController()
    # Incoming samples fit, but the hard-cut's new overlap needs extra space.
    controller.maximum_buffer_seconds = 4.1
    feed(controller, pcm(4))
    assert controller.buffered_seconds <= 4.1
    assert controller.generation == 1
    assert controller._active.pcm == pcm(.2)
    assert not controller._active.overlaps_previous
    event, = controller.pop_recoveries()
    assert event['dropped_audio_seconds'] == pytest.approx(3.8)
    feed(controller, pcm(.02, 2000) + pcm(.5, 0))
    final = controller.next_snapshot()
    assert final.pcm == pcm(.2) + pcm(.02, 2000) + pcm(.5, 0)


def test_end_of_stream_keeps_unframed_tail_and_invalidates_cache():
    controller = FastQwenController()
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    controller.accept(early, 'Early.')
    controller.feed(pcm(.01, 2000))
    controller.finish()
    final = controller.next_snapshot()
    assert final.cached_text is None
    assert final.pcm.endswith(pcm(.01, 2000))
    assert controller.accept(final, 'Full.').is_final
    assert controller.next_snapshot() is None


def test_detected_language_belongs_to_window_not_last_inference():
    controller = FastQwenController()
    feed(controller, pcm(.4) + pcm(.2, 0))
    controller.accept(controller.next_snapshot(), '日本語。', 'ja')
    feed(controller, pcm(.3, 0) + pcm(.4))
    snapshot = controller.next_snapshot()
    assert snapshot.cached_language == 'ja'
    controller.accept(snapshot, snapshot.cached_text, snapshot.cached_language)
    feed(controller, pcm(.5, 0))
    result = controller.accept(controller.next_snapshot(), '中文。', 'zh')
    assert result.language == 'zh'


def collect_final_pcm(controller, audio):
    snapshots = []
    for offset in range(0, len(audio), 640):
        controller.feed(audio[offset:offset+640])
        while (snapshot := controller.next_snapshot()) is not None:
            # Empty early results force a full final snapshot, so the check
            # compares complete PCM ownership rather than cache optimization.
            result = controller.accept(snapshot, '' if snapshot.speculative else 'source')
            if result.is_final:
                snapshots.append(snapshot)
    return snapshots


def test_neural_cut_preserves_short_contiguous_speech_tail_without_duplicate_pcm():
    controller = FastQwenController(voiced_detector=lambda frame: frame == pcm(.02, 1000))
    original = pcm(.4) + pcm(.5, 2000) + pcm(.18, 3000) + pcm(.5, 0)
    snapshots = collect_final_pcm(controller, original)
    assert len(snapshots) == 2
    assert b''.join(s.pcm for s in snapshots) == original
    assert snapshots[1].start_sample == snapshots[0].end_sample
    assert snapshots[1].pcm == pcm(.18, 3000) + pcm(.5, 0)
    assert not any(s.overlaps_previous for s in snapshots)


def test_neural_cut_continuation_survives_chain_until_actual_silence():
    controller = FastQwenController(voiced_detector=lambda frame: frame == pcm(.02, 1000))
    original = (pcm(.4) + pcm(.5, 2000)) * 3 + pcm(.02, 3000) + pcm(.5, 0)
    snapshots = collect_final_pcm(controller, original)
    assert len(snapshots) == 4
    assert b''.join(s.pcm for s in snapshots) == original
    assert all(a.end_sample == b.start_sample for a, b in zip(snapshots, snapshots[1:]))
    # The exception is not sticky after an actual RMS pause: an independent
    # sub-200-ms input still follows the existing minimum-utterance policy.
    assert collect_final_pcm(controller, pcm(.18, 3000) + pcm(.5, 0)) == []


def test_one_quiet_frame_breaks_pending_continuation_before_new_short_noise():
    controller = FastQwenController(voiced_detector=lambda frame: frame == pcm(.02, 1000))
    first = collect_final_pcm(controller, pcm(.4) + pcm(.5, 2000))
    assert len(first) == 1
    assert collect_final_pcm(controller, pcm(.02, 0) + pcm(.18, 3000) + pcm(.5, 0)) == []


def test_continuation_carries_across_maximum_window_but_never_forwards_empty_overlap():
    controller = FastQwenController(voiced_detector=lambda frame: frame == pcm(.02, 1000))
    audio = pcm(.4) + pcm(.5, 2000) + pcm(4.18, 3000) + pcm(.5, 0)
    snapshots = collect_final_pcm(controller, audio)
    assert len(snapshots) == 3
    assert snapshots[1].reason == 'maximum_window'
    assert snapshots[2].overlaps_previous
    assert snapshots[2].pcm == pcm(.38, 3000) + pcm(.5, 0)
    assert snapshots[2].start_sample == snapshots[1].end_sample - 3200
    # A forced carry contains old PCM, but with no new voiced frame it must
    # never become another caption just because it inherited continuation.
    controller = FastQwenController(voiced_detector=lambda frame: frame == pcm(.02, 1000))
    snapshots = collect_final_pcm(controller, pcm(.4) + pcm(.5, 2000) + pcm(4, 3000) + pcm(.5, 0))
    assert len(snapshots) == 2


def test_neural_end_followed_only_by_silence_does_not_create_empty_caption():
    controller = FastQwenController(voiced_detector=lambda frame: frame == pcm(.02, 1000))
    snapshots = collect_final_pcm(controller, pcm(.4) + pcm(.5, 2000) + pcm(3, 0))
    assert len(snapshots) == 1 and snapshots[0].reason == 'neural_silence'


@pytest.mark.parametrize('tail_seconds', [.02, .1, .18, .2])
def test_forced_cut_keeps_short_immediate_tail(tail_seconds):
    snapshots = collect_final_pcm(
        FastQwenController(), pcm(4) + pcm(tail_seconds, 2000) + pcm(.5, 0))
    assert len(snapshots) == 2
    assert snapshots[0].reason == 'maximum_window'
    assert snapshots[1].start_sample == snapshots[0].end_sample - 3200
    assert snapshots[1].overlaps_previous
    assert snapshots[1].pcm == pcm(.2) + pcm(tail_seconds, 2000) + pcm(.5, 0)


@pytest.mark.parametrize('burst_seconds', [.02, .1, .18, .2])
def test_forced_cut_silence_restores_minimum_for_separate_burst(burst_seconds):
    snapshots = collect_final_pcm(
        FastQwenController(), pcm(4) + pcm(.02, 0) + pcm(burst_seconds, 2000) + pcm(.5, 0))
    assert len(snapshots) == (2 if burst_seconds >= .2 else 1)


def test_forced_cut_silence_does_not_emit_overlap_only_caption():
    snapshots = collect_final_pcm(FastQwenController(), pcm(4) + pcm(.5, 0))
    assert len(snapshots) == 1 and snapshots[0].pcm == pcm(4)


def test_subminimum_parent_does_not_relax_next_forced_window():
    # Nine voiced frames, separated by gaps shorter than the endpoint delay,
    # reach the four-second cut without qualifying as a parent utterance.
    parent = pcm(.02) + pcm(.46, 0) + pcm(.02)
    parent += (pcm(.48, 0) + pcm(.02)) * 7
    assert len(parent) == 4 * 32000
    assert collect_final_pcm(FastQwenController(), parent + pcm(.1, 2000) + pcm(.5, 0)) == []
