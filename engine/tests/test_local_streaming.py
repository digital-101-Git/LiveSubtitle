"""Streaming policy tests with synthetic PCM and explicitly timed hypotheses."""
import math
import struct

import pytest

from engine.local_streaming import ASRWord, LocalWhisperStreaming
from engine.settings import EngineError


def pcm(seconds, amplitude=1000):
    return struct.pack("<h", amplitude) * round(seconds * 16000)


def decode(controller, words):
    snapshot = controller.snapshot()
    assert snapshot is not None
    return snapshot, controller.accept(snapshot, [ASRWord(*word) for word in words])


def feed_decode(controller, seconds, words, amplitude=1000):
    controller.feed(pcm(seconds, amplitude))
    return decode(controller, words)


def test_silent_input_is_bounded_and_produces_no_recognition_work():
    controller = LocalWhisperStreaming()
    controller.feed(pcm(60, 0))
    assert controller.snapshot() is None and controller.buffered_seconds == 0
    assert len(controller._prefix) == 20 and not controller._pending


def test_arbitrary_pcm_packet_boundaries_preserve_every_sample_and_prefix():
    controller = LocalWhisperStreaming()
    controller.feed(pcm(.4, 0))
    voice = pcm(1)
    for index in range(0, len(voice), 94):
        controller.feed(voice[index:index + 94])
    snapshot = controller.snapshot()
    assert snapshot.pcm == pcm(.4, 0) + voice
    assert snapshot.offset_seconds == 0 and not snapshot.is_final
    assert snapshot.voiced_frames == 50


def test_low_volume_threshold_retains_broadcast_speech_above_existing_gate():
    controller = LocalWhisperStreaming()
    controller.feed(pcm(1, 80))  # 80/32768 > .002; below the obsolete .006 gate.
    assert controller.snapshot() is not None


def test_two_observations_require_new_audio_not_repeated_snapshot_calls():
    controller = LocalWhisperStreaming()
    controller.feed(pcm(1))
    snapshot = controller.snapshot()
    assert controller.snapshot() is None  # Only one inference may be outstanding.
    first = controller.accept(snapshot, [ASRWord(.1, .7, "Hello.")])
    assert first.segments == () and first.committed_text == "" and first.pending_text == "Hello."
    for _ in range(20):
        assert controller.snapshot() is None
    controller.feed(pcm(.98))
    assert controller.snapshot() is None
    controller.feed(pcm(.02))
    second, result = decode(controller, [(.1, .7, "Hello."), (.9, 1.8, " More")])
    assert result.segments == ("Hello.",) and result.committed_text == "Hello."
    assert result.pending_text == "More" and second.sequence > snapshot.sequence


def test_changed_hypothesis_does_not_commit_until_two_outputs_agree():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [(.1, .7, "Wrong.")])
    _, changed = feed_decode(controller, 1, [(.1, .7, "Right.")])
    assert changed.segments == () and changed.committed_text == ""
    _, stable = feed_decode(controller, 1, [(.1, .7, "Right.")])
    assert stable.segments == ("Right.",)


@pytest.mark.parametrize("earlier,later,expected", [
    ([(.1, .4, "师娘"), (.4, .6, "们。")], [(.1, .2, "师"), (.2, .6, "娘们。")], "师娘们。"),
    ([(.1, .2, "师"), (.2, .6, "娘们。")], [(.1, .4, "师娘"), (.4, .6, "们。")], "师娘们。"),
    ([(.1, .4, "こんにちは"), (.4, .6, "世界。")], [(.1, .2, "こん"), (.2, .6, "にちは世界。")], "こんにちは世界。"),
])
def test_cjk_word_tokenization_changes_still_agree(earlier, later, expected):
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, earlier)
    _, result = feed_decode(controller, 1, later)
    assert result.segments == (expected,) and result.committed_text == expected


def test_character_prefix_never_commits_a_partial_current_asr_word():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [(.1, .6, "师娘")])
    _, result = feed_decode(controller, 1, [(.1, .7, "师娘们")])
    assert result.committed_text == "" and result.segments == ()
    assert result.pending_text == "师娘们"


def test_numeric_and_punctuation_revisions_are_not_normalized_away():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [(.1, .7, "1.5")])
    _, result = feed_decode(controller, 1, [(.1, .7, "15")])
    assert result.committed_text == ""


def test_near_audio_end_word_is_held_until_new_context_arrives():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [(.1, .9, "Hello.")])
    _, second = feed_decode(controller, 1, [(.1, 1.9, "Hello.")])
    assert second.committed_text == ""
    _, third = feed_decode(controller, 1, [(.1, 1.9, "Hello.")])
    assert third.segments == ("Hello.",)


def test_trim_uses_committed_timestamp_and_preserves_unconfirmed_pcm():
    controller = LocalWhisperStreaming()
    first_pcm, second_pcm = pcm(1, 1000), pcm(1, 2000)
    controller.feed(first_pcm)
    decode(controller, [(.1, .6, "Hello."), (.7, .9, " he")])
    controller.feed(second_pcm)
    snapshot, result = decode(controller, [(.1, .6, "Hello."), (.7, 1.7, " she")])
    assert result.segments == ("Hello.",) and result.pending_text == "she"
    retained = bytes(controller._windows[0].pcm)
    assert retained == snapshot.pcm[round(.6 * 16000) * 2:]
    assert controller._windows[0].offset_samples == round(.6 * 16000)
    controller.feed(pcm(1, 3000))
    next_snapshot = controller.snapshot()
    assert next_snapshot.offset_seconds == pytest.approx(.6)
    assert next_snapshot.pcm == retained + pcm(1, 3000)


def test_overlapping_unconfirmed_word_prevents_trimming_its_beginning():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [(.1, .5, "Hello."), (.45, .8, " he")])
    snapshot, result = feed_decode(controller, 1, [(.1, .5, "Hello."), (.45, .8, " she")])
    assert result.committed_text == "" and result.segments == ()
    assert bytes(controller._windows[0].pcm) == snapshot.pcm
    _, stable = feed_decode(controller, 1, [(.1, .5, "Hello."), (.45, .8, " she")])
    # Even stable words cannot be trimmed at an overlapping sentence boundary.
    assert stable.segments == () and stable.committed_text == ""
    assert controller._windows[0].offset_samples == 0
    _, final = feed_decode(controller, .5, [(.1, .5, "Hello."), (.45, .8, " she")], amplitude=0)
    assert final.segments == ("Hello.", "she") and final.committed_text == "Hello. she"


def test_overlap_chain_backs_up_to_last_safe_word_boundary():
    controller = LocalWhisperStreaming()
    previous = [(.0, .2, "First."), (.3, .6, " One"), (.55, .8, " two"), (.75, .9, " old")]
    current = [(.0, .2, "First."), (.3, .6, " One"), (.55, .8, " two"), (.75, 1.2, " new")]
    feed_decode(controller, 1, previous)
    snapshot, result = feed_decode(controller, 1, current)
    assert result.committed_text == "First." and result.pending_text == "One two new"
    assert bytes(controller._windows[0].pcm) == snapshot.pcm[round(.2 * 16000) * 2:]


def test_trim_checks_all_pending_starts_not_only_adjacent_word():
    controller = LocalWhisperStreaming()
    previous = [(.1, .5, "Hello."), (.8, .9, " old"), (.45, .95, " tail")]
    current = [(.1, .5, "Hello."), (.8, .9, " new"), (.45, 1.2, " tail")]
    feed_decode(controller, 1, previous)
    snapshot, result = feed_decode(controller, 1, current)
    assert result.committed_text == ""
    assert bytes(controller._windows[0].pcm) == snapshot.pcm


def test_zero_time_hypothesis_cannot_repeat_without_pcm_progress():
    controller = LocalWhisperStreaming()
    emitted = []
    for _ in range(4):
        _, result = feed_decode(controller, 1, [(0, 0, "Hello.")])
        emitted.extend(result.segments)
        assert result.committed_text == ""
    assert emitted == [] and controller._windows[0].offset_samples == 0
    _, result = feed_decode(controller, .5, [(0, 0, "Hello.")], amplitude=0)
    assert result.segments == ("Hello.",) and controller.buffered_seconds == 0


@pytest.mark.parametrize("text", ["Yes.", "はい。", "你好。"])
def test_identical_words_after_trim_are_retained_as_new_spoken_occurrences(text):
    controller = LocalWhisperStreaming()
    words = [(.1, .4, text), (.6, .9, " " + text)]
    feed_decode(controller, 1, words)
    _, second = feed_decode(controller, 1, words)
    assert second.segments == (text, text)
    # The next occurrence is later audio, not an overlapping text suffix.
    _, third = feed_decode(controller, 1, [(.2, .6, " " + text)])
    assert third.segments == ()
    _, fourth = feed_decode(controller, 1, [(.2, .6, " " + text)])
    assert fourth.segments == (text,)


def test_stable_unfinished_words_wait_for_sentence_or_clause_boundary():
    controller = LocalWhisperStreaming()
    words = [(.1, .3, "He"), (.4, .7, " wasn't")]
    feed_decode(controller, 1, words)
    snapshot, result = feed_decode(controller, 1, words)
    assert result.segments == () and result.committed_text == ""
    assert result.pending_text == "He wasn't"
    assert bytes(controller._windows[0].pcm) == snapshot.pcm
    complete_words = words + [(.8, 1.4, " that big.")]
    _, next_result = feed_decode(controller, 1, complete_words)
    assert next_result.segments == ()
    _, complete = feed_decode(controller, 1, complete_words)
    assert complete.segments == ("He wasn't that big.",)
    assert complete.committed_text == "He wasn't that big." and complete.pending_text == ""


def test_japanese_stable_middle_stays_revisable_with_its_audio_context():
    controller = LocalWhisperStreaming()
    mistaken = [(.1, .7, "五大名王と呼ばれる")]
    feed_decode(controller, 1, mistaken)
    snapshot, second = feed_decode(controller, 1, mistaken + [(.7, 1.7, "主要な命")])
    assert second.committed_text == "" and second.segments == ()
    assert bytes(controller._windows[0].pcm) == snapshot.pcm
    corrected = [(.1, .7, "五大明王と呼ばれる"), (.7, 1.7, "主要な明王の中央に配されることも多い。")]
    _, third = feed_decode(controller, 1, corrected)
    assert third.committed_text == "" and "名王" not in third.pending_text
    _, fourth = feed_decode(controller, 1, corrected)
    expected = "五大明王と呼ばれる主要な明王の中央に配されることも多い。"
    assert fourth.segments == (expected,) and fourth.committed_text == expected
    assert controller._windows[0].offset_samples == round(1.7 * 16000)


def test_complete_sentence_trims_only_published_words_not_stable_middle_after_it():
    controller = LocalWhisperStreaming()
    words = [(.1, .4, "Hello."), (.5, .8, " He wasn't")]
    feed_decode(controller, 1, words)
    snapshot, result = feed_decode(controller, 1, words)
    assert result.segments == ("Hello.",) and result.committed_text == "Hello."
    assert result.pending_text == "He wasn't"
    assert bytes(controller._windows[0].pcm) == snapshot.pcm[round(.4 * 16000) * 2:]
    corrected = [(.1, .4, " She wasn't"), (.5, 1.0, " that big.")]
    _, first = feed_decode(controller, 1, corrected)
    assert first.segments == () and first.pending_text == "She wasn't that big."
    _, second = feed_decode(controller, 1, corrected)
    assert second.segments == ("She wasn't that big.",)


def test_clause_trim_keeps_stable_unpublished_tail_in_pcm():
    controller = LocalWhisperStreaming()
    words = [(.1, .5, "This opening clause is stable,"), (.5, .8, " but that")]
    feed_decode(controller, 1, words)
    snapshot, result = feed_decode(controller, 1, words)
    assert result.segments == ("This opening clause is stable,",)
    assert result.committed_text == "This opening clause is stable," and result.pending_text == "but that"
    assert bytes(controller._windows[0].pcm) == snapshot.pcm[round(.5 * 16000) * 2:]


def test_stable_phrase_at_safe_pause_commits_but_following_middle_stays_pending():
    controller = LocalWhisperStreaming()
    words = [(.1, .5, "This is a stable phrase"), (.9, 1.0, " with tail")]
    feed_decode(controller, 1, words)
    snapshot, result = feed_decode(controller, 1, words)
    assert result.segments == ("This is a stable phrase",)
    assert result.committed_text == "This is a stable phrase" and result.pending_text == "with tail"
    assert bytes(controller._windows[0].pcm) == snapshot.pcm[round(.5 * 16000) * 2:]


def test_sentence_inside_a_timed_segment_cannot_trim_unpublished_partial_word():
    controller = LocalWhisperStreaming()
    words = [(.1, .7, "Hello. An unfinished second sentence")]
    feed_decode(controller, 1, words)
    snapshot, result = feed_decode(controller, 1, words)
    assert result.segments == () and result.committed_text == ""
    assert bytes(controller._windows[0].pcm) == snapshot.pcm
    _, final = feed_decode(controller, .5, words, amplitude=0)
    assert final.segments == ("Hello.", "An unfinished second sentence")


def test_real_repetitions_without_punctuation_are_retained_until_final():
    controller = LocalWhisperStreaming()
    words = [(.1, .4, "はい"), (.4, .7, "はい")]
    feed_decode(controller, 1, words)
    _, result = feed_decode(controller, 1, words)
    assert result.committed_text == "" and result.pending_text == "はいはい"
    _, final = feed_decode(controller, .5, words, amplitude=0)
    assert final.segments == ("はいはい",)


def test_logged_long_chinese_final_is_split_at_phrase_spaces_even_with_one_timed_segment():
    controller = LocalWhisperStreaming()
    source = ("我不知道我能不能擁有一個好結局 但唯一知道的是 你不配擁有一個好結局 "
              "放錢給我 這場遊戲 我將奉陪到底 你分的什麼呀 小棠 我會")
    controller.feed(pcm(10) + pcm(.5, 0))
    _, result = decode(controller, [(0, 9.8, source)])
    assert result.segments == (
        "我不知道我能不能擁有一個好結局", "但唯一知道的是", "你不配擁有一個好結局",
        "放錢給我 這場遊戲", "我將奉陪到底", "你分的什麼呀", "小棠 我會",
    )
    assert "".join(result.segments).replace(" ", "") == source.replace(" ", "")
    assert result.committed_text == source and result.pending_text == ""


@pytest.mark.parametrize("gap", [" ", "  ", "\t", "\u3000"])
def test_cjk_phrase_space_is_an_early_boundary_only_after_two_observations(gap):
    controller = LocalWhisperStreaming()
    first = "我不知道我能不能擁有一個好結局"
    feed_decode(controller, 1, [(.1, .7, first)])
    snapshot, result = feed_decode(controller, 1, [(.1, .7, first), (.8, 1.7, gap + "但唯一知道的是")])
    assert result.segments == (first,) and result.committed_text == first
    assert result.pending_text == "但唯一知道的是"
    assert bytes(controller._windows[0].pcm) == snapshot.pcm[round(.7 * 16000) * 2:]


@pytest.mark.parametrize("text", [
    "This is an English sentence without punctuation",
    "我們使用 RTX 4080 顯示卡", "今年收入大約 1,000 元人民幣", "小棠 我會",
])
def test_cjk_space_rule_preserves_latin_numeric_and_short_phrases(text):
    controller = LocalWhisperStreaming()
    controller.feed(pcm(1) + pcm(.5, 0))
    _, result = decode(controller, [(.1, .7, text)])
    assert result.segments == (text,)


def test_cjk_phrase_spaces_do_not_remove_real_repetitions():
    controller = LocalWhisperStreaming()
    controller.feed(pcm(1) + pcm(.5, 0))
    _, result = decode(controller, [(.1, .7, "我會奉陪到底 我會奉陪到底")])
    assert result.segments == ("我會奉陪到底", "我會奉陪到底")


def test_no_space_continuous_speech_releases_three_second_stable_phrases_in_order():
    controller = LocalWhisperStreaming()
    chunks = ["今天我們", "一起研究", "這個遊戲", "所有細節", "都很重要", "請仔細看", "請仔細看", "不要錯過"]
    outputs = []
    first_output = None
    for received in range(1, 9):
        controller.feed(pcm(1))
        snapshot = controller.snapshot()
        words = [ASRWord(index - snapshot.offset_seconds, index + .7 - snapshot.offset_seconds, text)
                 for index, text in enumerate(chunks[:received]) if index >= snapshot.offset_seconds]
        result = controller.accept(snapshot, words)
        if result.segments and first_output is None:
            first_output = received
        outputs.extend(result.segments)
        assert result.warnings == ()
        assert controller.buffered_seconds <= 8
    controller.feed(pcm(.5, 0))
    snapshot = controller.snapshot()
    words = [ASRWord(index - snapshot.offset_seconds, index + .7 - snapshot.offset_seconds, text)
             for index, text in enumerate(chunks) if index >= snapshot.offset_seconds]
    outputs.extend(controller.accept(snapshot, words).segments)
    assert first_output == 4
    assert outputs == ["".join(chunks[:3]), "".join(chunks[3:6]), "".join(chunks[6:])]
    assert "".join(outputs) == "".join(chunks)


def test_time_fallback_does_not_force_an_unstable_prefix_after_four_seconds():
    controller = LocalWhisperStreaming()
    for second in range(1, 7):
        prefix = "甲" if second % 2 else "乙"
        words = [(0, .7, prefix), (1, 1.7, "第二部分"), (2, 2.7, "第三部分")]
        # The first short observation contains only the available timed words.
        _, result = feed_decode(controller, 1, [word for word in words if word[1] <= second])
        assert result.committed_text == "" and result.segments == ()
    assert controller._windows[0].offset_samples == 0 and controller.buffered_seconds == 6


def test_time_fallback_preserves_context_pcm_and_requires_new_audio_for_next_observation():
    controller = LocalWhisperStreaming()
    words = [(0, .7, "第一部分"), (1, 1.7, "第二部分"), (2, 2.7, "第三部分"), (3, 3.7, "最後部分")]
    feed_decode(controller, 4, words)
    snapshot, result = feed_decode(controller, 1, words)
    assert result.segments == ("第一部分第二部分第三部分",)
    assert result.pending_text == "最後部分"
    assert bytes(controller._windows[0].pcm) == snapshot.pcm
    assert controller._windows[0].context_frontier == pytest.approx(2.7)
    assert controller.snapshot() is None
    controller.feed(pcm(1))
    following = controller.snapshot()
    assert following.offset_seconds == 0 and following.context_in_audio


def test_time_fallback_holds_short_middle_when_longer_boundary_overlaps_tail():
    controller = LocalWhisperStreaming()
    earlier = [(0, .7, "第一部分"), (1, 1.7, "第二部分"), (2, 2.7, "第三部分"), (2.6, 3.8, "舊尾巴")]
    later = earlier[:3] + [(2.6, 3.8, "新尾巴")]
    feed_decode(controller, 4, earlier)
    snapshot, result = feed_decode(controller, 1, later)
    assert result.segments == () and result.committed_text == ""
    assert result.pending_text == "第一部分第二部分第三部分新尾巴"
    assert bytes(controller._windows[0].pcm) == snapshot.pcm


@pytest.mark.parametrize("parts,expected", [
    ([" RTX", " 4080", " Super"], "我們現在來看這款顯示卡 RTX 4080 Super"),
    (["1,", "000", "元"], "我們現在來看這款顯示卡1,000元"),
])
def test_time_fallback_avoids_product_number_and_unit_boundaries(parts, expected):
    controller = LocalWhisperStreaming()
    words = [(0, .7, "我們現在來看"), (1, 1.7, "這款顯示卡"),
             (2, 2.7, parts[0]), (2.7, 2.9, parts[1]), (2.9, 3.2, parts[2]), (3.2, 3.7, "的後續內容")]
    feed_decode(controller, 4, words)
    _, result = feed_decode(controller, 1, words)
    assert result.committed_text == expected and result.segments == (expected,)
    assert result.pending_text == "的後續內容"


def test_distant_natural_sentence_end_does_not_block_earlier_time_fallback():
    controller = LocalWhisperStreaming()
    words = [(index, index + .7, "這段內容" + ("。" if index == 14 else "")) for index in range(15)]
    feed_decode(controller, 3, words[:3])
    _, result = feed_decode(controller, 13, words)
    assert result.segments == ("這段內容" * 3,)
    assert result.pending_text == "這段內容" * 12 + "。"
    assert controller._windows[0].offset_samples == 0
    assert controller._windows[0].context_frontier == pytest.approx(2.7)


def seed_retained_context(controller, tail=True):
    words = [(0, .7, "甲甲"), (1, 1.7, "乙乙"), (2, 2.7, "丙丙")]
    if tail:
        words.append((3, 3.7, "丁丁"))
    feed_decode(controller, 4, words)
    _, result = feed_decode(controller, 1, words)
    assert result.committed_text == "甲甲乙乙丙丙"
    assert controller._windows[0].offset_samples == 0
    return words


def test_retained_context_is_removed_across_cjk_repartition_and_merged_timed_word():
    controller = LocalWhisperStreaming()
    seed_retained_context(controller)
    words = [(0, 1.7, "甲甲乙乙"), (2, 3.7, "丙丙丁丁"), (4, 4.7, "戊戊")]
    snapshot, result = feed_decode(controller, 1, words)
    assert snapshot.context_in_audio and snapshot.offset_seconds == 0
    assert result.committed_text == "" and result.pending_text == "丁丁戊戊"
    remaining = controller._windows[0].previous_words
    assert remaining[0] == ASRWord(2.7, 3.7, "丁丁")
    assert bytes(controller._windows[0].pcm) == snapshot.pcm


def test_retained_context_prefix_revision_uses_published_audio_times():
    controller = LocalWhisperStreaming()
    seed_retained_context(controller)
    words = [(0, .72, "改甲"), (1, 1.72, "改乙"), (2, 2.69, "改丙"),
             (3, 3.7, "丁丁"), (4, 4.7, "戊戊")]
    _, result = feed_decode(controller, 1, words)
    assert result.pending_text == "丁丁戊戊" and result.committed_text == ""


def test_exact_old_context_text_allows_small_timestamp_jitter():
    controller = LocalWhisperStreaming()
    seed_retained_context(controller)
    words = [(0, .72, "甲甲"), (1, 1.72, "乙乙"), (2, 2.75, "丙丙"), (3, 3.7, "丁丁")]
    _, result = feed_decode(controller, 1, words)
    assert result.pending_text == "丁丁" and result.segments == ()


def test_revised_context_merged_word_ending_before_frontier_does_not_create_zero_time_suffix():
    controller = LocalWhisperStreaming()
    seed_retained_context(controller)
    words = [(0, 2.6, "甲甲乙乙丙丙改詞"), (3, 3.7, "丁丁")]
    _, result = feed_decode(controller, 1, words)
    assert result.pending_text == "丁丁"
    assert controller._windows[0].previous_words == (ASRWord(3, 3.7, "丁丁"),)


def test_new_short_word_straddling_frontier_is_not_deleted_as_timestamp_jitter():
    controller = LocalWhisperStreaming()
    seed_retained_context(controller)
    _, result = feed_decode(controller, 1, [(2.68, 2.8, "はい")])
    assert result.pending_text == "はい" and result.segments == ()
    assert controller._windows[0].previous_words == (ASRWord(2.7, 2.8, "はい"),)


def test_identical_later_phrase_is_preserved_if_asr_omits_old_context_occurrence():
    controller = LocalWhisperStreaming()
    seed_retained_context(controller)
    repeated = [(3, 3.7, "甲甲"), (4, 4.7, "乙乙"), (5, 5.7, "丙丙")]
    _, first = feed_decode(controller, 1, repeated)
    assert first.pending_text == "甲甲乙乙丙丙" and first.segments == ()
    _, second = feed_decode(controller, 1, repeated)
    assert second.segments == ("甲甲乙乙丙丙",)
    assert controller._windows[0].context_frontier == pytest.approx(5.7)


def test_natural_sentence_commit_releases_retained_context_and_resets_snapshot_flag():
    controller = LocalWhisperStreaming()
    context = seed_retained_context(controller)
    words = context[:3] + [(3, 3.7, "丁丁。")]
    _, first = feed_decode(controller, 1, words)
    assert first.segments == ()
    _, second = feed_decode(controller, 1, words)
    assert second.segments == ("丁丁。",)
    assert not controller._windows[0].context_words
    assert controller._windows[0].context_frontier is None
    assert controller._windows[0].offset_samples == round(3.7 * 16000)
    controller.feed(pcm(1))
    snapshot = controller.snapshot()
    assert not snapshot.context_in_audio and snapshot.offset_seconds == pytest.approx(3.7)


@pytest.mark.parametrize("hallucination", [False, True])
def test_silence_final_does_not_republish_audio_context_or_add_context_hallucination(hallucination):
    controller = LocalWhisperStreaming()
    words = seed_retained_context(controller, tail=False)
    if hallucination:
        words = words + [(5.1, 5.3, "新的幻覺。")]
    snapshot, result = feed_decode(controller, .5, words, amplitude=0)
    assert snapshot.is_final and snapshot.context_in_audio
    assert result.segments == () and result.committed_text == "" and result.pending_text == ""
    assert controller.buffered_seconds == 0


def test_new_short_repeated_speech_after_retained_context_survives_final():
    controller = LocalWhisperStreaming()
    words = seed_retained_context(controller, tail=False)
    controller.feed(pcm(.2) + pcm(.5, 0))
    _, result = decode(controller, words + [(5, 5.2, "甲甲")])
    assert result.segments == ("甲甲",) and result.committed_text == "甲甲"


def test_ambiguous_revised_crossing_word_does_not_erase_a_new_suffix():
    controller = LocalWhisperStreaming()
    seed_retained_context(controller)
    words = [(0, .7, "改甲"), (1, 1.7, "改乙"), (2, 3.7, "改丙丁丁"), (4, 4.7, "戊戊")]
    _, result = feed_decode(controller, 1, words)
    assert result.segments == () and result.pending_text == "改丙丁丁戊戊"
    assert controller._windows[0].previous_words[0].start == pytest.approx(2.7)


def test_long_no_punctuation_stream_retains_recent_context_but_bounds_audio_and_preserves_repetitions():
    controller = LocalWhisperStreaming()
    chunks = ["甲乙", "丙丁", "戊己"] * 14
    outputs = []
    saw_context_trim = False
    for received in range(1, len(chunks) + 1):
        controller.feed(pcm(1))
        snapshot = controller.snapshot()
        words = [ASRWord(index - snapshot.offset_seconds, index + .7 - snapshot.offset_seconds, text)
                 for index, text in enumerate(chunks[:received]) if index >= snapshot.offset_seconds]
        result = controller.accept(snapshot, words)
        outputs.extend(result.segments)
        window = controller._windows[0]
        if window.offset_samples:
            saw_context_trim = True
            assert window.context_frontier - window.offset_samples / 16000 >= 3
            assert window.context_words and window.context_words[0].start >= window.offset_samples / 16000
        assert controller.buffered_seconds <= 8.5
    controller.feed(pcm(.5, 0))
    snapshot = controller.snapshot()
    words = [ASRWord(index - snapshot.offset_seconds, index + .7 - snapshot.offset_seconds, text)
             for index, text in enumerate(chunks) if index >= snapshot.offset_seconds]
    outputs.extend(controller.accept(snapshot, words).segments)
    assert saw_context_trim and "".join(outputs) == "".join(chunks)
    assert controller.buffered_seconds == 0


@pytest.mark.parametrize("punctuation", ["", "。"])
def test_long_no_space_final_splits_timed_words_without_losing_or_deduplicating_text(punctuation):
    controller = LocalWhisperStreaming()
    chunks = ["今天我們", "一起研究", "這個遊戲", "所有細節", "都很重要", "請仔細看", "請仔細看", "不要錯過", "不要錯過"]
    chunks[-1] += punctuation
    controller.feed(pcm(9) + pcm(.5, 0))
    _, result = decode(controller, [(index, index + .7, text) for index, text in enumerate(chunks)])
    assert result.segments == tuple("".join(chunks[index:index + 3]) for index in (0, 3, 6))
    assert "".join(result.segments) == "".join(chunks)
    assert result.committed_text == "".join(chunks) and result.pending_text == ""
    assert controller.buffered_seconds == 0


def test_final_time_split_preserves_product_name_and_unfinished_last_words():
    controller = LocalWhisperStreaming()
    words = [(0, .7, "我們現在來看"), (1, 1.7, "這款顯示卡"),
             (2, 2.7, " RTX"), (2.7, 2.9, " 4080"), (2.9, 3.2, " Super"),
             (3.2, 3.7, "的後續內容"), (4, 4.7, "尚未說完")]
    controller.feed(pcm(5) + pcm(.5, 0))
    _, result = decode(controller, words)
    assert result.segments == ("我們現在來看這款顯示卡 RTX 4080 Super", "的後續內容尚未說完")
    assert result.pending_text == "" and controller.buffered_seconds == 0


def test_final_time_split_does_not_invent_boundaries_inside_one_long_timed_word():
    controller = LocalWhisperStreaming()
    source = "沒有單字時間戳記的整段原文需要全部保留下來"
    controller.feed(pcm(5) + pcm(.5, 0))
    _, result = decode(controller, [(0, 4.7, source)])
    assert result.segments == (source,) and result.committed_text == source


@pytest.mark.parametrize("absolute_words,expected", [
    ([], ""),
    ([(.1, .6, "我會"), (5, 5.7, "繼續"), (6, 6.7, "說話。")], "我會繼續說話。"),
    ([(.1, 4.1, "這是一個完整穩定的句子。")], "這是一個完整穩定的句子。"),
    ([(0, .7, "甲甲"), (1, 1.7, "乙乙"), (2, 2.7, "丙丙"),
      (3, 3.4, "我會"), (8, 8.7, "繼續"), (9, 9.7, "說話。")], "甲甲乙乙丙丙我會繼續說話。"),
])
def test_former_24_second_stalls_continue_through_background_energy(absolute_words, expected):
    controller = LocalWhisperStreaming()
    output = []
    reset_count = 0
    for second in range(1, 46):
        controller.feed(pcm(1))
        snapshot = controller.snapshot()
        words = [ASRWord(start - snapshot.offset_seconds, end - snapshot.offset_seconds, text)
                 for start, end, text in absolute_words if start >= snapshot.offset_seconds and end <= second]
        result = controller.accept(snapshot, words)
        output.extend(result.segments)
        reset_count += int(result.reset_prompt)
        assert controller.buffered_seconds < 10
    assert "".join(output) == expected and reset_count > 0


def test_deadline_uses_latest_unstable_hypothesis_but_retains_last_half_second():
    controller = LocalWhisperStreaming()
    last = None
    for second in range(1, 9):
        controller.feed(pcm(1))
        snapshot = controller.snapshot()
        words = [ASRWord(index + .1, index + .9,
                         ("甲" if second % 2 else "乙") if index == 0 else "丙")
                 for index in range(second)]
        last = controller.accept(snapshot, words)
        if second < 8:
            assert not last.segments and not last.warnings
    assert "".join(last.segments) == "乙" + "丙" * 6
    assert last.pending_text == "丙" and last.warnings == ("local_streaming_deadline",)
    assert controller._windows[0].context_frontier == pytest.approx(6.9)
    assert controller._windows[0].pending_words[-1].end == pytest.approx(7.9)
    assert controller._windows[0].offset_samples / 16000 <= 3.9


def test_empty_observations_preserve_pending_until_warned_deadline_recovery():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [(.1, .6, "Earlier")])
    outputs, warnings = [], []
    for second in range(2, 12):
        _, result = feed_decode(controller, 1, [])
        outputs.extend(result.segments)
        warnings.extend(result.warnings)
        if second < 8:
            assert result.pending_text == "Earlier" and not result.segments
    assert outputs == ["Earlier"] and warnings == ["local_streaming_deadline"]


def test_moving_unstable_word_timestamps_cannot_reset_publication_deadline_forever():
    controller = LocalWhisperStreaming()
    published = []
    for second in range(1, 47):
        controller.feed(pcm(1))
        snapshot = controller.snapshot()
        duration = len(snapshot.pcm) / 32000
        latest = ("Alpha" if second % 2 else "Beta") + f" draft {second}"
        result = controller.accept(snapshot, [ASRWord(max(.1, duration - 2), max(.7, duration - 1), latest)])
        if result.segments:
            assert result.segments == (latest,)
            assert result.warnings == ("local_streaming_deadline",)
            published.append((second, latest))
        assert controller.buffered_seconds < 15
    assert published[0][0] == 8 and len(published) >= 5
    assert len({text for _, text in published}) == len(published)


def test_empty_gap_then_nonoverlapping_new_words_preserves_both_observed_utterances():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [(.1, .6, "Earlier")])
    feed_decode(controller, 1, [])
    _, later = feed_decode(controller, 1, [(2.1, 2.6, "Later")])
    assert later.pending_text == "Earlier Later"
    _, final = feed_decode(controller, .5, [], amplitude=0)
    assert final.segments == ("Earlier", "Later")  # The 1.5-second word pause separates two utterances.
    assert final.warnings == ("local_streaming_deadline",)


def test_empty_gap_then_overlapping_revised_words_replaces_old_hypothesis():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [(.1, .6, "Earlier")])
    feed_decode(controller, 1, [])
    _, revised = feed_decode(controller, 1, [(.1, .6, "Revised")])
    assert revised.pending_text == "Revised"
    _, final = feed_decode(controller, .5, [], amplitude=0)
    assert final.segments == ("Revised",)


def test_final_can_recognize_late_words_inside_previously_received_audio():
    controller = LocalWhisperStreaming()
    words = [(.1, .7, "Hello.")]
    feed_decode(controller, 1, words)
    _, first = feed_decode(controller, 1, words)
    assert first.segments == ("Hello.",)
    _, final = feed_decode(controller, .5, [(.7, 1.2, "Later.")], amplitude=0)
    assert final.segments == ("Later.",)


def test_growing_segment_without_internal_timestamps_progresses_at_hard_deadline():
    controller = LocalWhisperStreaming()
    warnings = []
    outputs = []
    for second in range(1, 46):
        controller.feed(pcm(1))
        snapshot = controller.snapshot()
        duration = len(snapshot.pcm) / 32000
        text = "字" * second
        result = controller.accept(snapshot, [ASRWord(0, duration - .1, text)])
        if result.warnings:
            assert result.segments == (text,) and result.warnings == ("local_streaming_deadline",)
            assert controller._windows[0].offset_samples / 16000 == pytest.approx(second - .1)
            assert controller.buffered_seconds == pytest.approx(.1)
            assert not controller._windows[0].context_words
        warnings.extend(result.warnings)
        outputs.extend(result.segments)
        assert controller.buffered_seconds < 13
    assert len(warnings) == 3 and len(outputs) == 3


def test_hard_segment_deadline_preserves_audio_arriving_after_snapshot():
    controller = LocalWhisperStreaming()
    for second in range(1, 12):
        feed_decode(controller, 1, [(0, second - .1, "字" * second)])
    controller.feed(pcm(1))
    snapshot = controller.snapshot()
    controller.feed(pcm(1, 2000))
    result = controller.accept(snapshot, [ASRWord(0, 11.9, "字" * 12)])
    assert result.warnings == ("local_streaming_deadline",)
    assert bytes(controller._windows[0].pcm) == pcm(.1) + pcm(1, 2000)


def test_stable_clause_can_be_released_without_ending_audio():
    controller = LocalWhisperStreaming()
    words = [(.1, .7, "This is a complete opening clause,")]
    feed_decode(controller, 1, words)
    _, result = feed_decode(controller, 1, words)
    assert result.segments == ("This is a complete opening clause,",)
    assert controller._active is not None and not controller._active.ended


def test_thousands_separator_is_not_a_clause_boundary():
    controller = LocalWhisperStreaming()
    words = [(.1, .7, "The price is 1,000 dollars")]
    feed_decode(controller, 1, words)
    _, result = feed_decode(controller, 1, words)
    assert result.segments == () and result.pending_text == "The price is 1,000 dollars"


def test_half_second_silence_finalizes_every_tail_without_second_agreement():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [(.1, .7, "Still speaking")])
    controller.feed(pcm(.48, 0))
    assert controller.snapshot() is None
    controller.feed(pcm(.02, 0))
    snapshot, result = decode(controller, [(.1, .8, "Still speaking without a period")])
    assert snapshot.is_final and result.segments == ("Still speaking without a period",)
    assert result.pending_text == "" and controller.buffered_seconds == 0


def test_empty_silence_final_preserves_last_unconfirmed_hypothesis():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [(.1, .7, "Wrong candidate")])
    feed_decode(controller, 1, [(.1, 1.5, "Latest unfinished candidate")])
    _, result = feed_decode(controller, .5, [], amplitude=0)
    assert result.segments == ("Latest unfinished candidate",)
    assert result.committed_text == "Latest unfinished candidate" and result.pending_text == ""


def test_empty_final_after_additional_energy_preserves_pending_with_warning():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [(.1, .7, "Earlier hypothesis")])
    controller.feed(pcm(.2))
    controller.feed(pcm(.5, 0))
    snapshot = controller.snapshot()
    result = controller.accept(snapshot, [])
    assert result.segments == ("Earlier hypothesis",)
    assert result.warnings == ("local_streaming_deadline",)
    assert controller.buffered_seconds == 0


def test_empty_final_after_already_committed_words_reports_missing_recognition_without_stopping():
    controller = LocalWhisperStreaming()
    words = [(.1, .7, "Hello.")]
    feed_decode(controller, 1, words)
    _, result = feed_decode(controller, 1, words)
    assert result.segments == ("Hello.",) and not controller._windows[0].previous_words
    controller.feed(pcm(.2))
    controller.feed(pcm(.5, 0))
    snapshot = controller.snapshot()
    result = controller.accept(snapshot, [])
    assert result.segments == () and result.warnings == ("local_streaming_empty_final",)
    assert controller.buffered_seconds == 0


def test_silence_only_final_cannot_add_hallucinated_words_after_all_words_were_published():
    controller = LocalWhisperStreaming()
    words = [(.1, .7, "Hello.")]
    feed_decode(controller, 1, words)
    _, committed = feed_decode(controller, 1, words)
    assert committed.segments == ("Hello.",) and committed.pending_text == ""
    snapshot, final = feed_decode(controller, .5, [(1.4, 1.7, " Oh, yeah.")], amplitude=0)
    assert snapshot.is_final and final.segments == ()
    assert final.committed_text == "" and final.pending_text == ""
    assert controller.buffered_seconds == 0 and controller.snapshot() is None


def test_short_spoken_repetition_after_commit_is_preserved_when_new_voice_arrives():
    controller = LocalWhisperStreaming()
    words = [(.1, .7, "Yes.")]
    feed_decode(controller, 1, words)
    _, committed = feed_decode(controller, 1, words)
    assert committed.segments == ("Yes.",) and committed.pending_text == ""
    controller.feed(pcm(.2))
    controller.feed(pcm(.5, 0))
    snapshot, final = decode(controller, [(1.3, 1.5, " Yes.")])
    assert snapshot.is_final and final.segments == ("Yes.",)
    assert final.committed_text == "Yes." and controller.buffered_seconds == 0


def test_silence_final_still_revises_existing_unpublished_tail_after_a_commit():
    controller = LocalWhisperStreaming()
    words = [(.1, .5, "Hello."), (.6, .8, " he")]
    feed_decode(controller, 1, words)
    _, committed = feed_decode(controller, 1, words)
    assert committed.segments == ("Hello.",) and committed.pending_text == "he"
    _, final = feed_decode(controller, .5, [(.1, .3, " she")], amplitude=0)
    assert final.segments == ("she",) and final.committed_text == "she"


def test_all_empty_recognition_can_finish_without_inventing_speech():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [])
    _, result = feed_decode(controller, .5, [], amplitude=0)
    assert result.segments == () and result.committed_text == "" and result.pending_text == ""


def test_rms_positive_tone_with_all_empty_asr_does_not_report_lost_speech():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [])
    controller.feed(pcm(.2))  # RMS detects energy, but ASR may reject music or noise.
    controller.feed(pcm(.5, 0))
    snapshot, result = decode(controller, [])
    assert snapshot.is_final and result.segments == ()
    assert result.committed_text == "" and result.pending_text == ""
    assert controller.buffered_seconds == 0


def test_final_empty_after_stable_unpunctuated_text_preserves_pending_translation():
    controller = LocalWhisperStreaming()
    words = [(.1, .7, "Without punctuation")]
    feed_decode(controller, 1, words)
    feed_decode(controller, 1, words)
    _, result = feed_decode(controller, .5, [], amplitude=0)
    assert result.segments == ("Without punctuation",)


def test_short_utterance_is_not_discarded_by_minimum_update_interval():
    controller = LocalWhisperStreaming()
    controller.feed(pcm(.1))
    controller.feed(pcm(.5, 0))
    snapshot, result = decode(controller, [(0, .1, "Yes.")])
    assert snapshot.is_final and result.segments == ("Yes.",)


def test_multiple_utterances_in_one_packet_have_independent_windows_and_keep_repetition():
    controller = LocalWhisperStreaming()
    controller.feed(pcm(.2) + pcm(.5, 0) + pcm(.2) + pcm(.5, 0))
    first, result = decode(controller, [(0, .2, "Yes.")])
    assert result.segments == ("Yes.",)
    second, result = decode(controller, [(.4, .6, "Yes.")])
    assert result.segments == ("Yes.",)
    assert first.window_id != second.window_id and second.offset_seconds == pytest.approx(.3)
    assert controller.snapshot() is None and controller.buffered_seconds == 0


def test_pcm_arriving_during_inference_survives_accepting_older_snapshot():
    controller = LocalWhisperStreaming()
    controller.feed(pcm(1))
    snapshot = controller.snapshot()
    controller.feed(pcm(1, 2000))
    controller.accept(snapshot, [ASRWord(.1, .7, "Hello.")])
    second = controller.snapshot()
    assert second.pcm == pcm(1) + pcm(1, 2000)
    assert controller.accept(second, [ASRWord(.1, .7, "Hello.")]).segments == ("Hello.",)


def test_long_continuous_speech_remains_bounded_without_forced_audio_drops():
    controller = LocalWhisperStreaming()
    outputs = []
    for second in range(80):
        controller.feed(pcm(1))
        snapshot = controller.snapshot()
        assert snapshot is not None
        offset = snapshot.offset_seconds
        available_end = offset + len(snapshot.pcm) / 32000
        # Each spoken sentence ends well inside its one-second slot.
        words = [ASRWord(index + .1 - offset, index + .6 - offset, f" 第{index}句。")
                 for index in range(80) if index + .1 >= offset and index + .6 <= available_end]
        result = controller.accept(snapshot, words)
        outputs.extend(result.segments)
        assert controller.buffered_seconds < 3
    controller.feed(pcm(.5, 0))
    snapshot = controller.snapshot()
    outputs.extend(controller.accept(snapshot, [ASRWord(79.1 - snapshot.offset_seconds, 79.6 - snapshot.offset_seconds, " 第79句。")]).segments)
    assert outputs == [f"第{index}句。" for index in range(80)]


def test_unstable_cap_raises_without_dropping_or_trimming_unconfirmed_audio():
    controller = LocalWhisperStreaming(max_buffer_seconds=2)
    feed_decode(controller, 1, [(.1, .7, "First")])
    controller.feed(pcm(1))
    snapshot = controller.snapshot()
    with pytest.raises(EngineError) as error:
        controller.accept(snapshot, [ASRWord(.1, 1.5, "Different")])
    assert error.value.code == "local_streaming_overrun"
    assert bytes(controller._windows[0].pcm) == pcm(2)
    assert controller.buffered_seconds == 2


def test_feed_overrun_is_explicit_and_memory_does_not_exceed_cap():
    controller = LocalWhisperStreaming(max_buffer_seconds=2)
    controller.feed(pcm(2))
    with pytest.raises(EngineError) as error:
        controller.feed(pcm(.02))
    assert error.value.code == "local_streaming_overrun" and controller.buffered_seconds == 2


def test_progress_at_cap_trims_only_confirmed_words_and_continues():
    controller = LocalWhisperStreaming(max_buffer_seconds=2)
    feed_decode(controller, 1, [(.1, .6, "Stable.")])
    _, result = feed_decode(controller, 1, [(.1, .6, "Stable."), (.8, 1.6, " tail")])
    assert result.segments == ("Stable.",) and controller.buffered_seconds == pytest.approx(1.4)
    controller.feed(pcm(.2))
    assert controller.buffered_seconds == pytest.approx(1.6)


@pytest.mark.parametrize("word", [
    ASRWord(math.nan, .5, "bad"), ASRWord(.5, .2, "bad"), ASRWord(-1, .2, "bad"),
    ASRWord(.2, 20, "bad"), ASRWord(.1, .5, None),
])
def test_bad_timestamps_are_observable_without_mutating_pending_audio(word):
    controller = LocalWhisperStreaming()
    controller.feed(pcm(1))
    snapshot = controller.snapshot()
    with pytest.raises(EngineError) as error:
        controller.accept(snapshot, [word])
    assert error.value.code == "asr_timestamps" and controller.buffered_seconds == 1


def test_already_accepted_snapshot_cannot_commit_again():
    controller = LocalWhisperStreaming()
    snapshot, _ = feed_decode(controller, 1, [(.1, .7, "Hello.")])
    with pytest.raises(EngineError) as error:
        controller.accept(snapshot, [ASRWord(.1, .7, "Hello.")])
    assert error.value.code == "local_streaming_snapshot"


@pytest.mark.parametrize("data", [b"", b"\x00", "not PCM"])
def test_invalid_pcm_is_rejected(data):
    with pytest.raises(EngineError) as error:
        LocalWhisperStreaming().feed(data)
    assert error.value.code == "invalid_audio"


def test_chinese_script_normalization_preserves_recognizer_spelling_and_numbers():
    import os
    from engine.local_streaming import _agreement_key
    from engine.text_normalization import chinese_comparison_text
    if os.name != "nt":
        pytest.skip("Windows comparison conversion uses LCMapStringEx")
    assert _agreement_key("驚喜準備臺灣") == _agreement_key("惊喜准备台湾")
    assert _agreement_key("1.5") != _agreement_key("15")
    controller = LocalWhisperStreaming()
    feed_decode(controller, 1, [(.1, .7, "歡迎回來。")])
    _, result = feed_decode(controller, 1, [(.1, .7, "欢迎回来。")])
    assert result.segments == ("欢迎回来。",)
    assert chinese_comparison_text("😀臺灣") == "😀台湾"


def test_script_normalizer_unavailable_falls_back_without_changing_text(monkeypatch):
    from engine import text_normalization
    monkeypatch.setattr(text_normalization, "_mapper", None)
    text_normalization.chinese_comparison_text.cache_clear()
    assert text_normalization.chinese_comparison_text("測試😀123") == "測試😀123"
    text_normalization.chinese_comparison_text.cache_clear()


@pytest.mark.parametrize("traditional,simplified", [
    ("你為什麼", "你为什么"),
    ("這是怎麼回事", "这是怎么回事"),
    ("你要怎麽做", "你要怎么做"),
])
def test_windows_chinese_interrogative_variant_is_comparison_equivalent(traditional, simplified):
    import os
    from engine.local_streaming import _agreement_key
    if os.name != "nt":
        pytest.skip("Windows comparison conversion uses LCMapStringEx")
    assert _agreement_key(traditional) == _agreement_key(simplified)
    # Script equivalence must not silently correct distinct names/homophones.
    assert _agreement_key("顧小棠") != _agreement_key("顧小糖")
    assert _agreement_key("婚禮") != _agreement_key("婚紗")


def test_logged_script_switch_with_20ms_jitter_does_not_republish_last_glyph():
    import os
    if os.name != "nt":
        pytest.skip("Windows script conversion")
    controller = LocalWhisperStreaming()
    seed_retained_context(controller)
    window = controller._windows[0]
    window.context_words = (ASRWord(.46, 1.74, "我為你準備了一份"),
                            ASRWord(1.74, 1.96, "驚"), ASRWord(1.96, 2.16, "喜"))
    window.context_frontier = 2.16
    words = (ASRWord(.46, 1.74, "我为你准备了一份"), ASRWord(1.74, 1.96, "惊"),
             ASRWord(1.96, 2.18, "喜"), ASRWord(4.7, 5.88, "你是我最好的朋友"))
    assert controller._without_context(window, words) == words[-1:]


@pytest.mark.parametrize("old_last,new_last,old_start,old_end,new_start,new_end", [
    ("禮", "姓", 3.26, 3.42, 3.26, 3.52),
    ("了", "了", 5.2, 5.42, 5.25, 5.93),
])
def test_logged_last_word_revision_is_removed_only_with_same_audio_anchor(
        old_last, new_last, old_start, old_end, new_start, new_end):
    controller = LocalWhisperStreaming()
    seed_retained_context(controller)
    window = controller._windows[0]
    window.context_words = (ASRWord(0, old_start - .2, "之前"),
                            ASRWord(old_start - .2, old_start, "同字"),
                            ASRWord(old_start, old_end, old_last))
    window.context_frontier = old_end
    words = (ASRWord(0, old_start - .2, "改過"),
             ASRWord(old_start - .2, old_start, "同字"),
             ASRWord(new_start, new_end, new_last),
             ASRWord(new_end + 1, new_end + 2, "新的發話"))
    assert controller._without_context(window, words) == words[-1:]
    repeated = (ASRWord(old_end + .02, old_end + .3, new_last),)
    assert controller._without_context(window, repeated) == repeated


def test_tail_anchor_does_not_discard_merged_fresh_suffix_or_unanchored_short_response():
    controller = LocalWhisperStreaming()
    seed_retained_context(controller)
    window = controller._windows[0]
    words = (ASRWord(1, 1.7, "乙乙"), ASRWord(2, 2.8, "丙丙新話"))
    assert controller._without_context(window, words)[-1].text == "丙丙新話"
    assert controller._without_context(window, (ASRWord(2.68, 2.8, "はい"),))[0].text == "はい"


def test_short_stable_middle_is_not_finalized_merely_because_old_enough():
    controller = LocalWhisperStreaming()
    feed_decode(controller, 4, [(0, .2, "她"), (.2, .5, "是我"), (.5, 3.7, "新的假設")])
    _, result = feed_decode(controller, 1, [(0, .2, "她"), (.2, .5, "是我"), (.5, 4.7, "最好的朋友")])
    assert result.segments == ()
    assert result.pending_text == "她是我最好的朋友"
    assert controller._windows[0].offset_samples == 0


def test_short_whole_utterance_can_complete_over_background_energy():
    controller = LocalWhisperStreaming()
    words = [(.1, .7, "知道了")]
    feed_decode(controller, 1, words)
    feed_decode(controller, 1, words)
    feed_decode(controller, 1, words)
    _, result = feed_decode(controller, 1, words)
    assert result.segments == ("知道了",) and not result.warnings


def test_short_phrase_pause_is_a_boundary_but_tiny_word_gap_is_not():
    controller = LocalWhisperStreaming()
    words = [(.1, .7, "知道了"), (1.4, 1.7, "後面"), (1.7, 1.9, "還在說")]
    feed_decode(controller, 2, words)
    _, result = feed_decode(controller, 1, words)
    assert result.segments == ("知道了",)
    assert result.pending_text == "後面還在說"


def test_repeated_nonempty_context_only_hypotheses_retract_unsupported_pending():
    controller = LocalWhisperStreaming()
    context = seed_retained_context(controller)
    words = context[:3] + [(3, 4.8, "可能誤識別的尾句")]
    feed_decode(controller, 1, words)
    _, first_miss = feed_decode(controller, 1, context[:3])
    assert first_miss.pending_text == "可能誤識別的尾句"
    _, second_miss = feed_decode(controller, 1, context[:3])
    assert second_miss.pending_text == "" and second_miss.segments == ()
    assert second_miss.warnings == ("local_streaming_pending_revised",)
    _, final = feed_decode(controller, .5, [], amplitude=0)
    assert final.segments == ()


def test_single_context_only_miss_then_returned_tail_remains_revisable():
    controller = LocalWhisperStreaming()
    context = seed_retained_context(controller)
    tail = [(3, 4.8, "尚未確定的尾句")]
    feed_decode(controller, 1, context[:3] + tail)
    _, missing = feed_decode(controller, 1, context[:3])
    assert missing.pending_text == "尚未確定的尾句" and not missing.segments
    _, returned = feed_decode(controller, 1, context[:3] + tail)
    assert returned.pending_text == "尚未確定的尾句" and not returned.segments
    offset = controller._windows[0].offset_samples / 16000
    _, final = feed_decode(controller, .5, [(3 - offset, 4.8 - offset, "尚未確定的尾句")], amplitude=0)
    assert final.segments == ("尚未確定的尾句",)


def test_first_context_only_miss_cannot_resurrect_tail_even_when_deadline_is_due():
    controller = LocalWhisperStreaming()
    context = seed_retained_context(controller)
    feed_decode(controller, 1, context[:3] + [(3, 4.8, "未獲確認的尾句")])
    _, first = feed_decode(controller, 7, context[:3])
    assert first.pending_text == "未獲確認的尾句" and first.segments == ()
    _, second = feed_decode(controller, 1, context[:3])
    assert second.pending_text == "" and second.segments == ()
    assert second.warnings == ("local_streaming_pending_revised",)


def test_raw_empty_between_context_misses_keeps_temporary_missing_speech_recovery():
    controller = LocalWhisperStreaming()
    context = seed_retained_context(controller)
    feed_decode(controller, 1, context[:3] + [(3, 4.8, "短暫漏掉的真話")])
    feed_decode(controller, 1, context[:3])
    feed_decode(controller, 1, [])
    _, context_again = feed_decode(controller, 1, context[:3])
    assert context_again.pending_text == "短暫漏掉的真話"
    assert "local_streaming_pending_revised" not in context_again.warnings
    _, final = feed_decode(controller, .5, [], amplitude=0)
    assert final.segments == ("短暫漏掉的真話",)
