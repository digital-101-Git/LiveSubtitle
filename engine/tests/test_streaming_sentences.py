import pytest

from engine.settings import EngineError
from engine.streaming_sentences import SentenceUpdate, StreamingSentences


def stable(ledger, text, now=0.0):
    assert ledger.observe(text, now) == []
    return ledger.observe(text, now + .7)


def test_one_observation_never_publishes_even_after_many_timer_ticks():
    ledger = StreamingSentences()
    assert ledger.observe("Hello.", 0) == []
    for now in (.6, 1, 10, 100):
        assert ledger.flush_due(now) == []


def test_second_observation_and_timer_release_at_stability_boundary():
    ledger = StreamingSentences()
    assert ledger.observe("Hello.", 0) == []
    assert ledger.observe("Hello.", .1) == []
    assert ledger.flush_due(.599) == []
    assert ledger.flush_due(.6) == [SentenceUpdate(1, "Hello.", False)]
    assert ledger.flush_due(10) == []
    assert ledger.observe("Hello.", 11) == []


def test_growing_hypothesis_preserves_stability_of_existing_prefix():
    ledger = StreamingSentences()
    assert ledger.observe("Hello. Next", 0) == []
    assert ledger.observe("Hello. Next sentence", .1) == []
    assert ledger.observe("Hello. Next sentence.", .6) == [SentenceUpdate(1, "Hello.", False)]
    assert ledger.observe("Hello. Next sentence. More", .8) == []
    assert ledger.flush_due(1.3) == [SentenceUpdate(2, "Next sentence.", False)]


def test_incomplete_tail_is_not_provisionally_published():
    ledger = StreamingSentences()
    assert stable(ledger, "Hello. He wasn't") == [SentenceUpdate(1, "Hello.", False)]
    assert ledger.flush_due(20) == []
    assert ledger.finalize("Hello. He wasn't") == [
        SentenceUpdate(1, "Hello.", True), SentenceUpdate(2, "He wasn't", True)]


def test_changed_sentence_reuses_id_after_stability_is_reestablished():
    ledger = StreamingSentences()
    assert stable(ledger, "It costs fifteen dollars.") == [SentenceUpdate(1, "It costs fifteen dollars.", False)]
    assert ledger.observe("It costs fifty dollars.", 1) == []
    assert ledger.observe("It costs fifty dollars.", 1.1) == []
    assert ledger.flush_due(1.61) == [SentenceUpdate(1, "It costs fifty dollars.", False)]
    assert ledger.finalize("It costs fifty dollars.") == [SentenceUpdate(1, "It costs fifty dollars.", True)]


def test_changed_earlier_prefix_resets_stability_of_later_sentence():
    ledger = StreamingSentences()
    assert ledger.observe("Wrong. Next.", 0) == []
    assert ledger.observe("Right. Next.", .4) == []
    assert ledger.observe("Right. Next.", .5) == []
    assert ledger.flush_due(.7) == []
    assert ledger.flush_due(1.1) == [SentenceUpdate(1, "Right.", False), SentenceUpdate(2, "Next.", False)]


def test_disappearing_candidate_cannot_be_published_by_stale_timer():
    ledger = StreamingSentences()
    assert ledger.observe("First. Second.", 0) == []
    assert ledger.observe("First. Second.", .1) == []
    assert ledger.observe("First. unfinished", .2) == []
    assert ledger.flush_due(.6) == [SentenceUpdate(1, "First.", False)]


def test_final_merge_corrects_first_id_and_retracts_surplus_provisional():
    ledger = StreamingSentences()
    assert stable(ledger, "We won. Today.") == [
        SentenceUpdate(1, "We won.", False), SentenceUpdate(2, "Today.", False)]
    assert ledger.finalize("We won today.") == [
        SentenceUpdate(1, "We won today.", True), SentenceUpdate(2, "Today.", True, True)]
    assert ledger.flush_due(100) == []


def test_final_split_reuses_existing_position_and_allocates_additional_id():
    ledger = StreamingSentences()
    assert stable(ledger, "Hello and welcome.") == [SentenceUpdate(1, "Hello and welcome.", False)]
    assert ledger.finalize("Hello. Welcome.") == [
        SentenceUpdate(1, "Hello.", True), SentenceUpdate(2, "Welcome.", True)]


def test_final_without_matching_prefix_preserves_all_authoritative_text():
    ledger = StreamingSentences()
    assert stable(ledger, "Wrong words. Wrong ending.")
    assert ledger.finalize("Entirely different! Last fragment") == [
        SentenceUpdate(1, "Entirely different!", True), SentenceUpdate(2, "Last fragment", True)]


def test_same_final_marks_existing_id_final_without_allocating_another():
    ledger = StreamingSentences()
    assert stable(ledger, "Hello.") == [SentenceUpdate(1, "Hello.", False)]
    assert ledger.finalize("Hello.") == [SentenceUpdate(1, "Hello.", True)]
    assert ledger.nextid == 2


@pytest.mark.parametrize("text", ["Yes. Yes.", "はい。はい。", "你好。你好。"])
def test_repetitions_have_distinct_ids_within_and_across_turns(text):
    ledger = StreamingSentences()
    provisional = stable(ledger, text)
    assert [update.segment_id for update in provisional] == [1, 2]
    final = ledger.finalize(text)
    assert [update.segment_id for update in final] == [1, 2]
    again = ledger.finalize(text)
    assert [update.segment_id for update in again] == [3, 4]
    assert all(update.is_final and not update.removed for update in final + again)


@pytest.mark.parametrize("empty", ["", " \n\t"])
def test_empty_final_retracts_all_provisional_positions_and_resets_turn(empty):
    ledger = StreamingSentences()
    assert stable(ledger, "First. Second.")
    assert ledger.finalize(empty) == [
        SentenceUpdate(1, "First.", True, True), SentenceUpdate(2, "Second.", True, True)]
    assert ledger.flush_due(100) == []
    assert stable(ledger, "Next.", 10) == [SentenceUpdate(3, "Next.", False)]


def test_empty_final_before_publication_does_not_create_or_remove_ids():
    ledger = StreamingSentences()
    assert ledger.observe("Hello.", 0) == []
    assert ledger.finalize("") == []
    assert ledger.flush_due(10) == []
    assert ledger.finalize("Next") == [SentenceUpdate(1, "Next", True)]


def test_interim_shrink_then_final_cannot_leave_surplus_captions():
    ledger = StreamingSentences()
    assert stable(ledger, "First. Second. Third.")
    assert ledger.observe("Changed.", 1) == []
    assert ledger.finalize("Changed.") == [
        SentenceUpdate(1, "Changed.", True),
        SentenceUpdate(2, "Second.", True, True), SentenceUpdate(3, "Third.", True, True)]


def test_final_only_path_preserves_short_and_unpunctuated_text():
    ledger = StreamingSentences()
    assert ledger.finalize("really? はい。 unfinished") == [
        SentenceUpdate(1, "really?", True), SentenceUpdate(2, "はい。", True),
        SentenceUpdate(3, "unfinished", True)]


def test_length_errors_do_not_reset_valid_pending_state():
    ledger = StreamingSentences()
    assert stable(ledger, "Valid.") == [SentenceUpdate(1, "Valid.", False)]
    for operation in (lambda: ledger.observe("x" * 6001, 1), lambda: ledger.finalize("x" * 6001)):
        with pytest.raises(EngineError) as error:
            operation()
        assert error.value.code == "transcript_too_long"
    assert ledger.finalize("Valid.") == [SentenceUpdate(1, "Valid.", True)]


def test_exact_length_limit_and_independent_session_objects():
    first, second = StreamingSentences(), StreamingSentences()
    assert first.observe("x" * 6000, 0) == []
    assert first.flush_due(100) == []
    assert second.finalize("New session.") == [SentenceUpdate(1, "New session.", True)]
    assert first.finalize("x" * 6000) == [SentenceUpdate(1, "x" * 6000, True)]


def test_many_short_repeated_sentences_keep_position_order_within_length_limit():
    ledger = StreamingSentences()
    text = " ".join(["Yes."] * 1000)
    provisional = stable(ledger, text)
    assert [update.segment_id for update in provisional] == list(range(1, 1001))
    assert all(update.text == "Yes." for update in provisional)
    assert ledger.finalize(text) == [SentenceUpdate(index, "Yes.", True) for index in range(1, 1001)]


def test_real_final_leading_mhm_insertion_preserves_all_existing_sentence_ids():
    ledger = StreamingSentences()
    prior = ["Oh yeah, yeah.", "He wasn't that big.", "But his solo music did well."]
    assert [update.segment_id for update in stable(ledger, " ".join(prior))] == [1, 2, 3]
    final = ledger.finalize("Mhm. " + " ".join(prior))
    assert final == [SentenceUpdate(4, "Mhm.", True)] + [
        SentenceUpdate(index, sentence, True) for index, sentence in enumerate(prior, 1)]
    assert ledger.finalize("Next.") == [SentenceUpdate(5, "Next.", True)]


def test_leading_sentence_deletion_retracts_only_deleted_id():
    ledger = StreamingSentences()
    assert stable(ledger, "Mistake. Correct. Last.")
    assert ledger.finalize("Correct. Last.") == [
        SentenceUpdate(2, "Correct.", True), SentenceUpdate(3, "Last.", True),
        SentenceUpdate(1, "Mistake.", True, True)]


def test_punctuation_whitespace_and_case_normalization_only_affects_matching():
    ledger = StreamingSentences()
    assert stable(ledger, "HELLO, WORLD! Next.")
    assert ledger.finalize("Before. Hello world. Next!") == [
        SentenceUpdate(3, "Before.", True), SentenceUpdate(1, "Hello world.", True),
        SentenceUpdate(2, "Next!", True)]


def test_insertion_among_repeated_sentences_preserves_monotonic_matching():
    ledger = StreamingSentences()
    assert stable(ledger, "Yes. Yes. Last.")
    assert ledger.finalize("Before. Yes. Yes. Last.") == [
        SentenceUpdate(4, "Before.", True), SentenceUpdate(1, "Yes.", True),
        SentenceUpdate(2, "Yes.", True), SentenceUpdate(3, "Last.", True)]


def test_replacement_block_does_not_reuse_ids_from_later_equal_block():
    ledger = StreamingSentences()
    assert stable(ledger, "First. Wrong. Last.")
    assert ledger.finalize("First. Right. Added. Last.") == [
        SentenceUpdate(1, "First.", True), SentenceUpdate(2, "Right.", True),
        SentenceUpdate(4, "Added.", True), SentenceUpdate(3, "Last.", True)]


def test_merge_and_split_with_an_unchanged_later_sentence():
    ledger = StreamingSentences()
    assert stable(ledger, "We won. Today. Last.")
    assert ledger.finalize("We won today. Last.") == [
        SentenceUpdate(1, "We won today.", True), SentenceUpdate(3, "Last.", True),
        SentenceUpdate(2, "Today.", True, True)]


def test_many_repeated_sentences_with_leading_insertion_keep_all_occurrences():
    ledger = StreamingSentences()
    text = " ".join(["Yes."] * 1000)
    assert len(stable(ledger, text)) == 1000
    final = ledger.finalize("Before. " + text)
    assert final == [SentenceUpdate(1001, "Before.", True)] + [
        SentenceUpdate(index, "Yes.", True) for index in range(1, 1001)]
