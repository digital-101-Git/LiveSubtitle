"""Real PCM ownership and hypothesis regressions, with no model/network use."""
import struct

import pytest

from engine.qwen_streaming import QwenStreaming, _key
from engine.settings import EngineError


def pcm(seconds, amplitude=1000):
    return struct.pack('<h', amplitude) * round(seconds * 16000)


def feed(controller, data):
    for start in range(0, len(data), 3200):
        controller.feed(data[start:start+3200])


def observe(controller, seconds, text, amplitude=1000):
    feed(controller, pcm(seconds, amplitude))
    snapshot = controller.next_snapshot()
    assert snapshot is not None
    return snapshot, controller.accept(snapshot, text, 'zh')


def test_repeated_observations_keep_identical_audio_start_and_never_character_trim():
    controller = QwenStreaming()
    first, one = observe(controller, 1.2, '你好世界。后面继续')
    second, two = observe(controller, 1, '你好世界。后面继续说话')
    assert one.segments == ()
    assert two.segments == ('你好世界。',)
    assert (first.start_sample, second.start_sample) == (0, 0)
    assert second.pcm.startswith(first.pcm)
    assert controller.buffered_seconds == pytest.approx(2.2)
    third, three = observe(controller, 1, '你好世界。后面继续说话。')
    assert third.pcm.startswith(second.pcm)
    assert three.segments == ('后面继续说话。',)


def test_cjk_space_changes_do_not_block_stability_and_display_original_is_kept():
    controller = QwenStreaming()
    observe(controller, 1.2, '今天 是 一个 好日子。明天')
    _, result = observe(controller, 1, '今天是一个好日子。明天也好')
    assert result.segments == ('今天是一个好日子。',)
    assert result.pending_text == '明天也好'


def test_latin_word_spaces_and_decimal_points_are_significant():
    assert _key('now here') != _key('nowhere')
    assert _key('3.5') != _key('35')
    assert _key('Hello, world.') == _key('Hello world')


@pytest.mark.parametrize('text', ['她是我', '副', 'Still waiting', '这是一个没有标点的长句子'])
def test_incomplete_stable_text_does_not_become_a_premature_final(text):
    controller = QwenStreaming()
    observe(controller, 1.2, text)
    _, result = observe(controller, 1, text)
    assert result.segments == ()
    assert result.pending_text == text


def test_substantial_comma_clause_can_publish_but_tiny_clause_cannot():
    controller = QwenStreaming()
    observe(controller, 1.2, '如果天气很好，我们')
    _, result = observe(controller, 1, '如果天气很好，我们就出去。')
    assert result.segments == ('如果天气很好，',)
    assert result.pending_text == '我们就出去。'
    other = QwenStreaming()
    observe(other, 1.2, '所以，我们继续等')
    _, result = observe(other, 1, '所以，我们继续等待')
    assert result.segments == ()


def test_differing_prefix_is_not_forced_stable_by_time():
    controller = QwenStreaming()
    observe(controller, 1.2, '第一种说法。')
    _, result = observe(controller, 1, '第二种说法。')
    assert result.segments == ()
    _, result = observe(controller, 1, '第三种说法。')
    assert result.segments == ()


def test_empty_interim_breaks_agreement_without_erasing_pcm_or_pending_evidence():
    controller = QwenStreaming()
    observe(controller, 1.2, '一句完整的话。')
    observe(controller, 1, '')
    _, result = observe(controller, 1, '一句完整的话。')
    assert result.segments == ()
    assert controller.buffered_seconds == pytest.approx(3.2)
    _, result = observe(controller, 1, '一句完整的话。')
    assert result.segments == ('一句完整的话。',)


def test_natural_final_flushes_unpunctuated_tail_once():
    controller = QwenStreaming()
    observe(controller, 1.2, '今天下雨')
    snapshot, result = observe(controller, .5, '今天下雨了', 0)
    assert snapshot.is_final
    assert result.segments == ('今天下雨了',)
    assert result.window_closed
    assert controller.next_snapshot() is None
    with pytest.raises(RuntimeError):
        controller.accept(snapshot, '今天下雨了')


def test_speculative_does_not_publish_and_reuses_nonempty_text_and_language_at_final():
    controller = QwenStreaming()
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    assert early.speculative and not early.is_final
    result = controller.accept(early, 'Yes.', 'en')
    assert result.segments == () and not result.window_closed
    feed(controller, pcm(.3, 0))
    final = controller.next_snapshot()
    assert final.is_final and final.cached_text == 'Yes.'
    assert final.cached_language == 'en'
    result = controller.accept(final, final.cached_text, final.cached_language)
    assert result.segments == ('Yes.',) and result.language == 'en'
    assert result.window_closed


def test_speculative_reply_after_silence_endpoint_is_promoted_without_second_request():
    controller = QwenStreaming()
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    feed(controller, pcm(.3, 0))
    result = controller.accept(early, 'Yes.', 'en')
    assert result.segments == ('Yes.',) and result.window_closed
    assert controller.next_snapshot() is None


def test_speech_resumption_invalidates_outstanding_speculation_without_publication():
    controller = QwenStreaming()
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    feed(controller, pcm(.4))
    result = controller.accept(early, 'This was wrong.', 'en')
    assert result.segments == () and not result.window_closed
    feed(controller, pcm(.5, 0))
    final = controller.next_snapshot()
    assert final.cached_text is None
    assert final.pcm.startswith(early.pcm)
    assert controller.accept(final, 'This is correct.', 'en').segments == ('This is correct.',)


def test_new_window_speech_does_not_invalidate_previous_ended_window_speculation():
    controller = QwenStreaming()
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    feed(controller, pcm(.3, 0) + pcm(.3))
    result = controller.accept(early, 'Yes.', 'en')
    assert result.window_closed and result.segments == ('Yes.',)
    feed(controller, pcm(.5, 0))
    second = controller.next_snapshot()
    assert second.window_id != early.window_id
    assert controller.accept(second, 'Yes.', 'en').segments == ('Yes.',)


def test_empty_speculation_always_requests_full_final_recognition():
    controller = QwenStreaming()
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    feed(controller, pcm(.3, 0))
    result = controller.accept(early, '', 'auto')
    assert not result.window_closed
    final = controller.next_snapshot()
    assert final.is_final and final.cached_text is None
    assert controller.accept(final, 'はい', 'ja').segments == ('はい',)


def test_empty_early_cache_also_does_not_skip_later_final():
    controller = QwenStreaming()
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    controller.accept(early, '', 'auto')
    feed(controller, pcm(.3, 0))
    assert controller.next_snapshot().cached_text is None


def test_only_one_outstanding_recognition_even_while_feed_continues():
    controller = QwenStreaming()
    feed(controller, pcm(1.2))
    snapshot = controller.next_snapshot()
    feed(controller, pcm(2))
    assert controller.next_snapshot() is None
    assert len(snapshot.pcm) == 38400
    controller.accept(snapshot, 'First.', 'en')
    second = controller.next_snapshot()
    assert second.end_sample == 51200
    assert len(second.pcm) == 102400


def test_stop_drops_late_result_and_all_buffered_audio():
    controller = QwenStreaming()
    feed(controller, pcm(1.2))
    snapshot = controller.next_snapshot()
    controller.stop()
    result = controller.accept(snapshot, 'This must never appear.', 'en')
    controller.feed(pcm(.2))
    assert result.segments == () and result.replacement_segments is None
    assert controller.buffered_seconds == 0
    assert controller.next_snapshot() is None


def test_hard_windows_cover_each_pcm_sample_without_text_estimated_cuts():
    controller = QwenStreaming()
    controller.maximum_window_seconds = 3
    all_pcm = b''.join(pcm(.1, 1000 + index) for index in range(75))
    snapshots = []
    for offset in range(0, len(all_pcm), 3200):
        controller.feed(all_pcm[offset:offset+3200])
        if (snapshot := controller.next_snapshot()) is not None:
            result = controller.accept(snapshot, '继续说话', 'zh')
            if result.window_closed:
                snapshots.append(snapshot)
                assert 'qwen_streaming_deadline' in result.warnings
    controller.finish()
    final = controller.next_snapshot()
    snapshots.append(final)
    controller.accept(final, '继续说话', 'zh')
    assert [item.start_sample for item in snapshots] == [0, 48000, 96000]
    assert b''.join(item.pcm for item in snapshots) == all_pcm
    assert snapshots[-1].end_sample == len(all_pcm) // 2


def test_music_with_empty_results_continues_past_multiple_hard_windows():
    controller = QwenStreaming()
    finals = 0
    for _ in range(450):
        controller.feed(pcm(.1))
        if (snapshot := controller.next_snapshot()) is not None:
            result = controller.accept(snapshot, '', 'auto')
            assert result.segments == () and result.replacement_segments is None
            finals += result.window_closed
    assert finals == 3
    assert controller.buffered_seconds <= 12


def test_real_repetition_within_cumulative_window_and_across_window_is_preserved():
    controller = QwenStreaming()
    observe(controller, 1.2, '不要。')
    _, result = observe(controller, 1, '不要。不要。')
    assert result.segments == ('不要。',)
    _, result = observe(controller, .5, '不要。不要。', 0)
    assert result.segments == ('不要。',)
    feed(controller, pcm(.3) + pcm(.5, 0))
    next_window = controller.next_snapshot()
    assert controller.accept(next_window, '不要。', 'zh').segments == ('不要。',)


def test_changed_published_prefix_is_replaced_at_final_instead_of_stopping_or_duplicating():
    controller = QwenStreaming()
    observe(controller, 1.2, '小王来了。然后')
    _, result = observe(controller, 1, '小王来了。然后坐下')
    assert result.segments == ('小王来了。',)
    _, result = observe(controller, 1, '小黄来了。然后坐下了。')
    assert not result.segments
    assert result.warnings == ('qwen_streaming_prefix_revised',)
    _, result = observe(controller, .5, '小黄来了。然后坐下了。', 0)
    assert result.window_closed
    assert result.segments == ()
    assert result.replacement_segments == ('小黄来了。', '然后坐下了。')


def test_final_replacement_can_remove_previous_wrong_sentence():
    controller = QwenStreaming()
    observe(controller, 1.2, 'Wrong sentence. More')
    observe(controller, 1, 'Wrong sentence. More words')
    _, result = observe(controller, .5, 'Correct sentence.', 0)
    assert result.segments == ()
    assert result.replacement_segments == ('Correct sentence.',)


def test_empty_final_preserves_last_nonempty_tail_with_explicit_warning():
    controller = QwenStreaming()
    observe(controller, 1.2, 'Earlier speech')
    _, result = observe(controller, .5, '', 0)
    assert result.segments == ('Earlier speech',)
    assert result.warnings == ('qwen_streaming_empty_final',)


def test_latest_nonempty_context_only_does_not_restore_older_unconfirmed_tail():
    controller = QwenStreaming()
    observe(controller, 1.2, 'Hello. Old tail')
    observe(controller, 1, 'Hello. Old tail')
    observe(controller, 1, 'Hello.')
    _, result = observe(controller, .5, '', 0)
    assert result.segments == ()
    assert result.replacement_segments is None


def test_adaptive_interval_skips_intermediate_snapshots_and_is_bounded():
    controller = QwenStreaming()
    feed(controller, pcm(1.2))
    controller.accept(controller.next_snapshot(), 'Text', inference_seconds=10)
    feed(controller, pcm(1))
    assert controller.next_snapshot() is None
    feed(controller, pcm(1))
    assert controller.next_snapshot() is not None


def test_bounded_memory_does_not_silently_discard_unrecognized_audio():
    controller = QwenStreaming()
    controller.maximum_buffer_seconds = 3
    feed(controller, pcm(3))
    before = controller.buffered_seconds
    with pytest.raises(EngineError) as error:
        controller.feed(pcm(.1))
    assert error.value.code == 'qwen_streaming_overrun'
    assert controller.buffered_seconds == before


def test_explicit_eos_retains_subframe_audio_tail():
    controller = QwenStreaming()
    original = pcm(.413)
    feed(controller, original)
    controller.finish()
    snapshot = controller.next_snapshot()
    assert snapshot.pcm == original
    assert snapshot.reason == 'end_of_stream' and snapshot.is_final
    assert controller.accept(snapshot, 'Yes.', 'en').segments == ('Yes.',)


@pytest.mark.parametrize('bad', [b'\x00', 'bad', pcm(1.1)], ids=['odd-bytes', 'not-bytes', 'oversized'])
def test_invalid_pcm_is_rejected(bad):
    with pytest.raises(ValueError):
        QwenStreaming().feed(bad)


def test_unique_tail_anchor_can_continue_after_name_revision_before_final_correction():
    controller = QwenStreaming()
    observe(controller, 1.2, '小王今天早上来到这里。下一句')
    observe(controller, 1, '小王今天早上来到这里。下一句话还没说')
    _, result = observe(controller, 1, '小黄今天早上来到这里。下一句话还没说。')
    assert result.segments == ('下一句话还没说。',)
    assert result.warnings == ('qwen_streaming_prefix_revised',)
    _, result = observe(controller, .5, '小黄今天早上来到这里。下一句话还没说。', 0)
    assert result.replacement_segments == ('小黄今天早上来到这里。', '下一句话还没说。')


def test_ambiguous_repeated_anchor_is_not_silently_used_to_drop_text():
    controller = QwenStreaming()
    observe(controller, 1.2, '小王今天早上来到这里。')
    observe(controller, 1, '小王今天早上来到这里。')
    _, result = observe(controller, 1, '小黄今天早上来到这里。小黄今天早上来到这里。后面。')
    assert result.segments == ()
    _, result = observe(controller, .5, '小黄今天早上来到这里。小黄今天早上来到这里。后面。', 0)
    assert result.replacement_segments == ('小黄今天早上来到这里。', '小黄今天早上来到这里。', '后面。')


def test_eos_partial_frame_speech_invalidates_early_silence_cache():
    controller = QwenStreaming()
    feed(controller, pcm(.4) + pcm(.2, 0))
    early = controller.next_snapshot()
    controller.accept(early, 'Old.', 'en')
    controller.feed(pcm(.01))
    controller.finish()
    final = controller.next_snapshot()
    assert final.cached_text is None
    assert final.speech_revision > early.speech_revision


def test_empty_final_fallback_retains_the_language_of_its_text():
    controller = QwenStreaming()
    feed(controller, pcm(1.2))
    controller.accept(controller.next_snapshot(), 'English speech', 'en')
    feed(controller, pcm(.5, 0))
    result = controller.accept(controller.next_snapshot(), '', 'auto')
    assert result.segments == ('English speech',)
    assert result.language == 'en'


def test_same_phrase_after_hard_boundary_is_not_cross_window_deduplicated():
    controller = QwenStreaming()
    controller.maximum_window_seconds = 2
    outputs = []
    for _ in range(40):
        controller.feed(pcm(.1))
        if (snapshot := controller.next_snapshot()) is not None:
            outputs.extend(controller.accept(snapshot, 'Again.', 'en').segments)
    assert outputs == ['Again.', 'Again.']


def test_all_closed_windows_wait_in_order_while_first_inference_runs():
    controller = QwenStreaming()
    feed(controller, pcm(.4) + pcm(.2, 0))
    first = controller.next_snapshot()
    feed(controller, pcm(.3, 0) + pcm(.4) + pcm(.5, 0) + pcm(.4) + pcm(.5, 0))
    results = [controller.accept(first, 'One.', 'en')]
    identifiers = [first.window_id]
    for text in ('Two.', 'Three.'):
        snapshot = controller.next_snapshot()
        identifiers.append(snapshot.window_id)
        results.append(controller.accept(snapshot, text, 'en'))
    assert identifiers == [1, 2, 3]
    assert [result.segments for result in results] == [('One.',), ('Two.',), ('Three.',)]
    assert controller.next_snapshot() is None


def test_simplified_traditional_equivalence_does_not_change_display_or_duplicate_tail():
    if _key('驚喜') != _key('惊喜'):
        pytest.skip('Windows Chinese comparison mapper unavailable')
    controller = QwenStreaming()
    observe(controller, 1.2, '我為你準備了一份驚喜。後面')
    _, result = observe(controller, 1, '我为你准备了一份惊喜。后面继续')
    assert result.segments == ('我为你准备了一份惊喜。',)
    _, result = observe(controller, .5, '我為你準備了一份驚喜。後面繼續。', 0)
    assert result.segments == ('後面繼續。',)
    assert result.replacement_segments is None


def test_invalidated_speculation_does_not_delay_a_due_regular_observation():
    controller = QwenStreaming()
    observe(controller, 1.2, 'First words')
    feed(controller, pcm(.8) + pcm(.2, 0))
    early = controller.next_snapshot()
    assert early.speculative
    feed(controller, pcm(.1))
    controller.accept(early, 'Discarded guess', 'en')
    resumed = controller.next_snapshot()
    assert resumed is not None and not resumed.speculative
    assert resumed.end_sample == 36800


def test_injected_speech_detector_ends_voice_over_music_without_altering_pcm():
    seen = []
    def detector(frame):
        seen.append(frame)
        return struct.unpack_from('<h', frame)[0] == 2100
    controller = QwenStreaming(voiced_detector=detector)
    original = pcm(.4, 2100) + pcm(.5, 900)
    feed(controller, original)
    snapshot = controller.next_snapshot()
    assert snapshot.is_final and snapshot.reason == 'silence'
    assert snapshot.pcm == original
    assert b''.join(seen) == original
    assert controller.accept(snapshot, 'Speech.', 'en').segments == ('Speech.',)


def test_detector_receives_subframe_tail_at_finish():
    seen = []
    def detector(frame):
        seen.append(frame)
        return True
    controller = QwenStreaming(voiced_detector=detector)
    original = pcm(.413)
    feed(controller, original)
    controller.finish()
    snapshot = controller.next_snapshot()
    assert b''.join(seen) == original
    assert len(seen[-1]) == len(pcm(.013))
    assert snapshot.pcm == original


def test_neural_false_negative_cannot_discard_rms_speech_or_prevent_its_window():
    controller = QwenStreaming(voiced_detector=lambda _: False)
    original = pcm(1.4, 1200) + pcm(.5, 0)
    feed(controller, original)
    final = controller.next_snapshot()
    assert final.is_final and final.reason == 'silence'
    assert final.start_sample == 0 and final.pcm == original
    assert controller.accept(final, 'Missed by VAD, retained by ASR.', 'en').segments == (
        'Missed by VAD, retained by ASR.',)


def test_neural_false_negative_continuous_audio_retains_existing_hard_windows():
    controller = QwenStreaming(voiced_detector=lambda _: False)
    controller.maximum_window_seconds = 3
    original = b''.join(pcm(.1, 1000 + index) for index in range(75))
    finals = []
    for offset in range(0, len(original), 3200):
        controller.feed(original[offset:offset+3200])
        if (snapshot := controller.next_snapshot()) is not None:
            result = controller.accept(snapshot, 'Again.', 'en')
            if result.window_closed:
                finals.append(snapshot)
                assert result.warnings == ('qwen_streaming_deadline',)
    controller.finish()
    final = controller.next_snapshot()
    finals.append(final)
    controller.accept(final, 'Again.', 'en')
    assert b''.join(item.pcm for item in finals) == original
    assert [item.start_sample for item in finals] == [0, 48000, 96000]


def test_neural_endpoint_does_not_replay_rms_positive_tail_in_the_next_window():
    detector = lambda frame: struct.unpack_from('<h', frame)[0] == 2100
    controller = QwenStreaming(voiced_detector=detector)
    first_pcm = pcm(.4, 2100) + pcm(.5, 900)
    feed(controller, first_pcm)
    first = controller.next_snapshot()
    assert controller.accept(first, 'Again.', 'en').segments == ('Again.',)
    # This utterance is missed entirely by the neural detector. It must still
    # start and close through RMS, without duplicating the earlier music tail.
    second_pcm = pcm(.4, 1300) + pcm(.5, 0)
    feed(controller, second_pcm)
    second = controller.next_snapshot()
    assert second.window_id != first.window_id
    assert second.start_sample == first.end_sample
    assert second.pcm == second_pcm
    assert controller.accept(second, 'Again.', 'en').segments == ('Again.',)


def test_rms_positive_neural_false_negative_invalidates_speculative_cache():
    controller = QwenStreaming(
        voiced_detector=lambda frame: struct.unpack_from('<h', frame)[0] == 2100)
    feed(controller, pcm(.4, 2100) + pcm(.2, 900))
    early = controller.next_snapshot()
    assert early.speculative
    # A neural silence hint is uncertain: later positive PCM may be resumed
    # speech, so the old speculative result cannot finalize it from cache.
    feed(controller, pcm(.3, 1300))
    stale = controller.accept(early, 'Earlier guess.', 'en')
    assert stale.segments == () and not stale.window_closed
    final = controller.next_snapshot()
    assert final.is_final and final.cached_text is None
    assert final.speech_revision > early.speech_revision
    assert controller.accept(final, 'Complete new speech.', 'en').segments == ('Complete new speech.',)


def test_neural_positive_on_low_rms_cannot_create_a_speech_window():
    controller = QwenStreaming(voiced_detector=lambda _: True)
    feed(controller, pcm(2, 0))
    assert controller.next_snapshot() is None
    assert controller.buffered_seconds == pytest.approx(.4)
