import pytest

from engine.local_captions import LocalCaptions
from engine.local_streaming import ASRSnapshot, StreamingResult
from engine.streaming_sentences import SentenceUpdate


def snapshot(end, *, sequence=1, window=1, offset=0.0, final=False):
    return ASRSnapshot(window, sequence, b"\0" * round((end - offset) * 32000),
                       offset, final, 1)


def result(pending="", *segments):
    return StreamingResult(segments, " ".join(segments), pending)


def test_first_preview_waits_for_new_audio_then_allows_short_meaningful_speech():
    captions = LocalCaptions()
    assert captions.accept(snapshot(1), result("はい")) == []
    assert captions.accept(snapshot(1.2, sequence=2), result("はい")) == []
    assert captions.accept(snapshot(1.35, sequence=3), result("はい")) == [
        SentenceUpdate(1, "はい", False)]


@pytest.mark.parametrize("text", ["Yes", "I", "嗯", "5", "はい"])
def test_short_valid_previews_have_no_arbitrary_character_minimum(text):
    captions = LocalCaptions()
    assert captions.accept(snapshot(1.2), result(text)) == []
    assert captions.accept(snapshot(1.55, sequence=2), result(text)) == [SentenceUpdate(1, text, False)]


@pytest.mark.parametrize("text", ["", " \n\t", "...", "，。！？"])
def test_empty_or_punctuation_only_hypothesis_does_not_allocate_id(text):
    captions = LocalCaptions()
    assert captions.accept(snapshot(2), result(text)) == []
    assert captions.nextid == 1


def test_changed_preview_reuses_id_and_requires_point_eight_seconds_new_audio():
    captions = LocalCaptions()
    assert captions.accept(snapshot(1.2), result("fifteen dollars today"))
    assert captions.accept(snapshot(1.999, sequence=2), result("fifty dollars today")) == []
    assert captions.accept(snapshot(2, sequence=3), result("fifty dollars today")) == [
        SentenceUpdate(1, "fifty dollars today", False)]
    assert captions.nextid == 2


def test_same_preview_never_reissues_translation_for_timer_or_new_audio():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("Hello."))
    assert captions.accept(snapshot(5, sequence=2), result("Hello.")) == []
    assert captions.accept(snapshot(10, sequence=3), result("Hello.")) == []


def test_optional_wall_clock_also_throttles_backlogged_audio():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("First."), now=50)
    assert captions.accept(snapshot(4, sequence=2), result("Second."), now=50.1) == []
    assert captions.accept(snapshot(5, sequence=3), result("Latest."), now=50.8) == [
        SentenceUpdate(1, "Latest.", False)]


def test_deferred_same_source_retries_after_throttle_then_stops_after_acceptance():
    captions = LocalCaptions()
    offered = captions.accept(snapshot(2), result("Hello."), now=10)[0]
    captions.defer_preview(offered.segment_id, offered.text)
    assert captions.accept(snapshot(2.7, sequence=2), result("Hello."), now=10.7) == []
    assert captions.accept(snapshot(2.8, sequence=3), result("Hello."), now=10.8) == [offered]
    # No second rejection: the queue accepted the retry, so identical input
    # goes back to producing no new transcript or translation request.
    assert captions.accept(snapshot(4, sequence=4), result("Hello."), now=12) == []


def test_rejected_reoffer_can_retry_again_without_allocating_another_id():
    captions = LocalCaptions()
    offered = captions.accept(snapshot(2), result("Hello."))[0]
    for sequence in range(2, 5):
        captions.defer_preview(offered.segment_id, offered.text)
        assert captions.accept(snapshot(sequence + 1, sequence=sequence), result("Hello.")) == [offered]
    assert captions.nextid == 2


def test_deferred_retry_requires_new_audio_and_wall_time():
    captions = LocalCaptions()
    offered = captions.accept(snapshot(2), result("Hello."), now=10)[0]
    captions.defer_preview(offered.segment_id, offered.text)
    assert captions.accept(snapshot(2, sequence=2), result("Hello."), now=12) == []
    assert captions.accept(snapshot(3, sequence=3), result("Hello."), now=10.5) == []
    assert captions.accept(snapshot(4, sequence=4), result("Hello."), now=11) == [offered]


def test_new_hypothesis_supersedes_a_deferred_preview_without_replaying_old_text():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("Fifteen."))
    captions.defer_preview(1, "Fifteen.")
    assert captions.accept(snapshot(3, sequence=2), result("Fifty.")) == [
        SentenceUpdate(1, "Fifty.", False)]
    captions.defer_preview(1, "Fifteen.")
    assert captions.accept(snapshot(4, sequence=3), result("Fifty.")) == []


def test_rejection_for_wrong_id_does_not_reoffer_current_preview():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("Hello."))
    captions.defer_preview(9, "Hello.")
    assert captions.accept(snapshot(3, sequence=2), result("Hello.")) == []


def test_deferred_preview_finalizes_same_id_immediately_despite_throttle():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("Hello."), now=10)
    captions.defer_preview(1, "Hello.")
    assert captions.accept(snapshot(2.1, sequence=2, final=True),
                           result("", "Hello."), now=10.1) == [SentenceUpdate(1, "Hello.", True)]
    captions.defer_preview(1, "Hello.")
    assert captions.accept(snapshot(3, sequence=3), result("Hello."), now=11) == []


def test_late_rejection_does_not_target_new_preview_after_confirmation():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("Yes."))
    assert captions.accept(snapshot(3, sequence=2), result("Yes.", "Yes.")) == [
        SentenceUpdate(1, "Yes.", True), SentenceUpdate(2, "Yes.", False)]
    captions.defer_preview(1, "Yes.")
    assert captions.accept(snapshot(4, sequence=3), result("Yes.")) == []


def test_late_rejection_cannot_revive_a_removed_preview():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("Wrong."))
    assert captions.accept(snapshot(3, sequence=2), result()) == [
        SentenceUpdate(1, "Wrong.", True, removed=True)]
    captions.defer_preview(1, "Wrong.")
    assert captions.accept(snapshot(4, sequence=3), result()) == []


def test_final_revision_reuses_id_and_bypasses_preview_throttle():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("fifteen dollars today"), now=50)
    assert captions.accept(snapshot(2.1, sequence=2, final=True),
                           result("", "Fifty dollars."), now=50.1) == [
        SentenceUpdate(1, "Fifty dollars.", True)]


def test_identical_final_still_emits_same_id_finality_for_translation_reuse():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("Hello."))
    assert captions.accept(snapshot(3, sequence=2, final=True), result("", "Hello.")) == [
        SentenceUpdate(1, "Hello.", True)]


def test_multiple_commits_keep_order_and_pending_gets_new_preview_id():
    captions = LocalCaptions(start_id=7)
    captions.accept(snapshot(2), result("First maybe."))
    assert captions.accept(snapshot(3, sequence=2), result("Next.", "First.", "Second.")) == [
        SentenceUpdate(7, "First.", True), SentenceUpdate(8, "Second.", True),
        SentenceUpdate(9, "Next.", False)]
    assert captions.accept(snapshot(4, sequence=3, final=True), result("", "Next.")) == [
        SentenceUpdate(9, "Next.", True)]


def test_confirmation_can_retire_preview_before_next_preview_refresh_is_due():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("First."))
    assert captions.accept(snapshot(2.2, sequence=2), result("Next.", "First.")) == [
        SentenceUpdate(1, "First.", True)]
    assert captions.accept(snapshot(3, sequence=3), result("Next.")) == [
        SentenceUpdate(2, "Next.", False)]


def test_empty_final_removes_only_the_current_preview():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("Pending.", "Kept."))
    assert captions.accept(snapshot(3, sequence=2, final=True), result()) == [
        SentenceUpdate(2, "Pending.", True, removed=True)]
    assert captions.accept(snapshot(4, sequence=3, final=True), result()) == []


def test_disappearing_nonfinal_preview_is_retracted_and_never_reuses_retired_id():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("Wrong."))
    assert captions.accept(snapshot(3, sequence=2), result()) == [
        SentenceUpdate(1, "Wrong.", True, removed=True)]
    assert captions.accept(snapshot(4, sequence=3), result("Actual.")) == [
        SentenceUpdate(2, "Actual.", False)]


def test_new_window_retracts_unfinished_old_preview_before_allocating_new_ids():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("Old preview.", "Old commit."))
    assert captions.accept(snapshot(11.2, sequence=2, window=2, offset=10),
                           result("New preview.", "New commit.")) == [
        SentenceUpdate(2, "Old preview.", True, removed=True),
        SentenceUpdate(3, "New commit.", True), SentenceUpdate(4, "New preview.", False)]


def test_short_new_window_has_its_own_first_preview_audio_gate():
    captions = LocalCaptions()
    captions.accept(snapshot(2, final=True), result("", "Old."))
    assert captions.accept(snapshot(10.5, sequence=2, window=2, offset=10), result("New")) == []
    assert captions.accept(snapshot(11.2, sequence=3, window=2, offset=10), result("New")) == [
        SentenceUpdate(2, "New", False)]


def test_pcm_trimming_does_not_restart_preview_audio_clock():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("Before."))
    assert captions.accept(snapshot(3, sequence=2, offset=2.5), result("After.")) == [
        SentenceUpdate(1, "After.", False)]


def test_real_repetitions_are_preserved_across_commits_and_windows():
    captions = LocalCaptions()
    assert captions.accept(snapshot(.5, final=True), result("", "Yes.", "Yes.")) == [
        SentenceUpdate(1, "Yes.", True), SentenceUpdate(2, "Yes.", True)]
    assert captions.accept(snapshot(2, sequence=2, window=2, offset=1, final=True),
                           result("", "Yes.")) == [SentenceUpdate(3, "Yes.", True)]


def test_duplicate_and_out_of_order_snapshots_cannot_duplicate_confirmed_text():
    captions = LocalCaptions()
    committed = result("", "Hello.")
    assert captions.accept(snapshot(2, sequence=2), committed)
    assert captions.accept(snapshot(2, sequence=2), committed) == []
    assert captions.accept(snapshot(1, sequence=1), committed) == []
    assert captions.nextid == 2


def test_closed_window_cannot_publish_again_with_a_later_sequence():
    captions = LocalCaptions()
    captions.accept(snapshot(2, final=True), result("", "Done."))
    assert captions.accept(snapshot(3, sequence=2), result("Ghost", "Ghost.")) == []


@pytest.mark.parametrize(("pending", "expected"), [
    ("First sentence. Second sentence and more", "First sentence."),
    ("这是第一个小句，后面还有很多未确认的文字", "这是第一个小句，"),
    ("我不知道能不能有好結局 但唯一知道的是 后面还会变化", "我不知道能不能有好結局"),
    ("Take 1,234 dollars, then continue", "Take 1,234 dollars,"),
    ("It costs 3.5 dollars today", "It costs 3.5 dollars today"),
    ("Dr. Smith can help", "Dr. Smith can help"),
])
def test_first_short_clause_preview_respects_decimals_and_abbreviations(pending, expected):
    assert LocalCaptions().accept(snapshot(2), result(pending)) == [
        SentenceUpdate(1, expected, False)]


def test_growing_tail_behind_same_preview_does_not_trigger_extra_translation():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("First clause, second"))
    assert captions.accept(snapshot(3, sequence=2), result("First clause, second grows")) == []


@pytest.mark.parametrize("pending", ["中" * 6000, "word " * 1200, "X" * 6000],
                         ids=["long-cjk", "long-spaced-english", "long-single-token"])
def test_preview_length_is_bounded_but_final_preserves_all_authoritative_text(pending):
    captions = LocalCaptions()
    updates = captions.accept(snapshot(2), result(pending))
    assert len(updates) == 1
    assert 0 < len(updates[0].text) <= captions.maximum_preview_characters
    assert pending.startswith(updates[0].text)
    assert captions.accept(snapshot(3, sequence=2, final=True), result("", pending)) == [
        SentenceUpdate(1, pending.strip(), True)]


def test_ascii_word_crossing_preview_limit_is_not_split_when_space_exists():
    captions = LocalCaptions()
    pending = "short " * 15 + "extraordinary later"
    updates = captions.accept(snapshot(2), result(pending))
    assert updates[0].text == ("short " * 15).strip()


def test_final_pending_is_not_promoted_without_controller_confirmation():
    captions = LocalCaptions()
    captions.accept(snapshot(2), result("Unconfirmed."))
    assert captions.accept(snapshot(3, sequence=2, final=True), result("Still unconfirmed")) == [
        SentenceUpdate(1, "Unconfirmed.", True, removed=True)]


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "1"])
def test_invalid_start_id_is_rejected(invalid):
    with pytest.raises(ValueError):
        LocalCaptions(invalid)


@pytest.mark.parametrize(("prefix", "complete"), [
    ("她是我", "她是我最好的朋友。"),
    ("I am", "I am waiting for the train."),
    ("彼女は私", "彼女は私の友達です。"),
    ("그 사람은", "그 사람은 제 친구예요."),
])
def test_single_short_noun_phrase_observation_waits_for_fuller_next_hypothesis(prefix, complete):
    captions = LocalCaptions()
    assert captions.accept(snapshot(2), result(prefix)) == []
    assert captions.nextid == 1
    assert captions.accept(snapshot(3, sequence=2), result(complete)) == [
        SentenceUpdate(1, complete, False)]


@pytest.mark.parametrize("text", ["Yes!", "はい。", "啊，", "No;"])
def test_explicit_short_sentence_or_clause_boundary_does_not_wait_for_second_observation(text):
    assert LocalCaptions().accept(snapshot(1.2), result(text)) == [SentenceUpdate(1, text, False)]


@pytest.mark.parametrize("text", ["Yes", "え", "嗯", "5"])
def test_authoritative_short_final_never_waits_for_preview_stability(text):
    captions = LocalCaptions()
    assert captions.accept(snapshot(.5, final=True), result("", text)) == [SentenceUpdate(1, text, True)]
    assert captions.nextid == 2


def test_changed_short_candidate_cannot_borrow_previous_candidate_observation():
    captions = LocalCaptions()
    assert captions.accept(snapshot(2), result("She is")) == []
    assert captions.accept(snapshot(3, sequence=2), result("He is")) == []
    assert captions.accept(snapshot(3.34, sequence=3), result("He is")) == []
    assert captions.accept(snapshot(3.35, sequence=4), result("He is")) == [SentenceUpdate(1, "He is", False)]


def test_short_stability_needs_new_audio_and_wall_time_not_repeated_decoding_only():
    captions = LocalCaptions()
    assert captions.accept(snapshot(2), result("Hello"), now=10) == []
    assert captions.accept(snapshot(2, sequence=2), result("Hello"), now=11) == []
    assert captions.accept(snapshot(3, sequence=3), result("Hello"), now=10.1) == []
    assert captions.accept(snapshot(3.4, sequence=4), result("Hello"), now=10.35) == [
        SentenceUpdate(1, "Hello", False)]
    assert captions.accept(snapshot(4.4, sequence=5), result("Hello"), now=11.4) == []


def test_committed_phrase_does_not_make_a_new_short_residual_immediately_eligible():
    captions = LocalCaptions()
    assert captions.accept(snapshot(2), result("已经完整的前一句。")) == [SentenceUpdate(1, "已经完整的前一句。", False)]
    assert captions.accept(snapshot(3, sequence=2), result("她是我", "已经完整的前一句。")) == [
        SentenceUpdate(1, "已经完整的前一句。", True)]
    assert captions.accept(snapshot(4, sequence=3), result("她是我最好的朋友。")) == [
        SentenceUpdate(2, "她是我最好的朋友。", False)]


def test_pending_candidate_does_not_survive_an_empty_observation_or_new_window():
    captions = LocalCaptions()
    assert captions.accept(snapshot(2), result("嗯")) == []
    assert captions.accept(snapshot(2.4, sequence=2), result()) == []
    assert captions.accept(snapshot(3, sequence=3), result("嗯")) == []
    assert captions.accept(snapshot(12, sequence=4, window=2, offset=10), result("嗯")) == []
    assert captions.accept(snapshot(12.35, sequence=5, window=2, offset=10), result("嗯")) == [
        SentenceUpdate(1, "嗯", False)]


def test_growing_phrase_leaves_short_guard_without_waiting_for_exact_whole_sentence_repetition():
    captions = LocalCaptions()
    assert captions.accept(snapshot(2), result("我想")) == []
    assert captions.accept(snapshot(3, sequence=2), result("我想要看看")) == []
    assert captions.accept(snapshot(4, sequence=3), result("我想要看看更多的东西")) == [
        SentenceUpdate(1, "我想要看看更多的东西", False)]


def test_final_arrives_before_short_candidate_is_ready_without_losing_or_delaying_text():
    captions = LocalCaptions()
    assert captions.accept(snapshot(2), result("She is")) == []
    assert captions.accept(snapshot(2.1, sequence=2, final=True), result("", "She is my friend.")) == [
        SentenceUpdate(1, "She is my friend.", True)]


def test_stabilized_short_preview_keeps_ack_retry_and_same_id_final_reuse():
    captions = LocalCaptions()
    assert captions.accept(snapshot(1.2), result("Yes")) == []
    preview = captions.accept(snapshot(1.55, sequence=2), result("Yes"))[0]
    captions.defer_preview(preview.segment_id, preview.text)
    assert captions.accept(snapshot(2.35, sequence=3), result("Yes")) == [preview]
    assert captions.accept(snapshot(2.45, sequence=4, final=True), result("", "Yes")) == [
        SentenceUpdate(preview.segment_id, "Yes", True)]


def test_deferred_first_preview_cannot_bypass_stability_for_a_different_short_hypothesis():
    captions = LocalCaptions()
    assert captions.accept(snapshot(2), result("She is")) == []
    offered = captions.accept(snapshot(3, sequence=2), result("She is"))[0]
    captions.defer_preview(offered.segment_id, offered.text)
    assert captions.accept(snapshot(4, sequence=3), result("He is")) == []
    assert captions.accept(snapshot(4.35, sequence=4), result("He is")) == [
        SentenceUpdate(offered.segment_id, "He is", False)]
    assert captions.nextid == 2


def test_already_accepted_preview_corrections_remain_immediate_after_later_deferral():
    captions = LocalCaptions()
    assert captions.accept(snapshot(2), result("She is")) == []
    accepted = captions.accept(snapshot(3, sequence=2), result("She is"))[0]
    # No rejection acknowledges the initial offer. A later correction can be
    # deferred, but should not turn this previously accepted ID into a new one.
    changed = captions.accept(snapshot(4, sequence=3), result("He is"))[0]
    captions.defer_preview(changed.segment_id, changed.text)
    assert captions.accept(snapshot(5, sequence=4), result("They are")) == [
        SentenceUpdate(accepted.segment_id, "They are", False)]
