import pytest

from engine.streaming_sentences import SentenceUpdate, StreamingSentences, extends_draft


def test_only_real_growth_can_release_a_whole_prefix_before_punctuation():
    ledger = StreamingSentences()
    assert ledger.observe("We are", 0) == []
    assert ledger.observe("We are", 1.0) == []
    assert ledger.flush_due(4.0) == []
    assert ledger.observe("We are still", 4.1) == [SentenceUpdate(1, "We are", False)]
    assert ledger.observe("We are still here", 4.3) == []
    assert ledger.flush_due(6) == []
    assert ledger.observe("We are still here today", 6.1) == [SentenceUpdate(1, "We are still", False)]
    assert ledger.finalize("We are still here tonight.") == [SentenceUpdate(1, "We are still here tonight.", True)]


def test_corrected_tail_context_and_new_turn_restart_the_clock():
    ledger = StreamingSentences()
    assert ledger.observe("Wrong. 我们", 0) == []
    assert ledger.observe("Right. 我们来", .7) == []
    assert ledger.observe("Right. 我们来到", 1.0) == []
    assert ledger.observe("Right. 我们来到了", 1.31) == [
        SentenceUpdate(1, "Right.", False), SentenceUpdate(2, "我们来", False)]
    assert list(ledger.draft_observations("Right.")) == [(1, "Right."), (2, "")]
    assert ledger.finalize("Right.") == [SentenceUpdate(1, "Right.", True), SentenceUpdate(2, "我们来", True, True)]
    assert ledger.observe("我们来了", 4.1) == []
    assert ledger.flush_due(100) == []


def test_withdrawn_translation_is_reoffered_if_source_stabilizes_again():
    ledger = StreamingSentences()
    assert not ledger.observe("It is five.", 0)
    assert ledger.observe("It is five.", .7) == [SentenceUpdate(1, "It is five.", False)]
    assert not ledger.observe("It is six.", .8)
    ledger.retry_draft(1)
    assert not ledger.observe("It is five.", 1.0)
    assert not ledger.observe("It is five.", 1.1)
    assert ledger.flush_due(1.7) == [SentenceUpdate(1, "It is five.", False)]
    assert not ledger.flush_due(3.0)


@pytest.mark.parametrize(("cut", "rest"), [
    ("He isn", "'t ready"), ("1", ".75 kg"), ("cafe\u0301", "s"),
    ("can'", "t see"), ("work", "shop"),
])
def test_incomplete_words_and_numbers_are_never_released_on_growth(cut, rest):
    assert not extends_draft(cut, cut + rest)
    ledger = StreamingSentences()
    assert ledger.observe(cut, 0) == []
    assert ledger.observe(cut + rest, 1) == []
    assert ledger.flush_due(100) == []


@pytest.mark.asyncio
async def test_provider_interim_without_punctuation_reaches_caption_before_final(tmp_path, monkeypatch):
    from engine.tests.test_gemini import make_gemini_session, send_interim, provider_final
    monkeypatch.setattr(StreamingSentences, "stability_seconds", 0)
    session, provider, client, runtime = await make_gemini_session(tmp_path, monkeypatch)
    try:
        await send_interim(provider, client, "我们")
        await send_interim(provider, client, "我们到家")
        draft = await client.wait_matching(lambda e: e["type"] == "caption" and not e["is_final"])
        provider_final(provider, "我们还没到家。")
        final = await client.wait_matching(lambda e: e["type"] == "caption" and e["is_final"])
        assert final["segment_id"] == draft["segment_id"]
        assert (draft["source_text"], final["source_text"]) == ("我们", "我们还没到家。")
        assert [call[0] for call in runtime.translations] == ["我们", "我们还没到家。"]
    finally:
        await session.stop(notify=False)
