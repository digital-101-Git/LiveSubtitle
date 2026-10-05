import pytest

from engine.sentences import completed_sentence_prefix, split_sentences


@pytest.mark.parametrize("text, expected", [
    ("", []),
    (" \t\n", []),
    ("Hello. Welcome! Are you ready?", ["Hello.", "Welcome!", "Are you ready?"]),
    ("really?", ["really?"]),
    ("No! Yes. Yes.", ["No!", "Yes.", "Yes."]),
    ("你好。现在开始！准备好了吗？", ["你好。", "现在开始！", "准备好了吗？"]),
    ("はい。はい。本当？行こう！", ["はい。", "はい。", "本当？", "行こう！"]),
    ("「本当？」次です。", ["「本当？」", "次です。"]),
    ("他说：“你好！”下一句。", ["他说：“你好！”", "下一句。"]),
    ('He asked ("Really?"). Then left.', ['He asked ("Really?").', "Then left."]),
    ("What?! Really！！？！ Yes.", ["What?!", "Really！！？！", "Yes."]),
    ("  Hello.\n\tNext   sentence!  unfinished tail  ", ["Hello.", "Next   sentence!", "unfinished tail"]),
    ("We paid 3.14 dollars. Next.", ["We paid 3.14 dollars.", "Next."]),
    ("Version 1.2.3 is ready. Good!", ["Version 1.2.3 is ready.", "Good!"]),
    ("Dr. Lee met Mr. Smith. They left.", ["Dr. Lee met Mr. Smith.", "They left."]),
    ("Prof. A. B. Jones is here. Welcome!", ["Prof. A. B. Jones is here.", "Welcome!"]),
    ("Use e.g. apples, i.e. fruit. Fine.", ["Use e.g. apples, i.e. fruit.", "Fine."]),
    ("The U.S. team won. Great!", ["The U.S. team won.", "Great!"]),
    ("Meet at 9 a.m. tomorrow. Okay?", ["Meet at 9 a.m. tomorrow.", "Okay?"]),
    ("Visit example.com today. Done.", ["Visit example.com today.", "Done."]),
    ("Visit example.com. Next.", ["Visit example.com.", "Next."]),
    ("See https://example.com/a?x=1.2&y=yes. Next.", ["See https://example.com/a?x=1.2&y=yes.", "Next."]),
    ("Use www.example.co.uk/path?q=yes!now today. Done.", ["Use www.example.co.uk/path?q=yes!now today.", "Done."]),
    ("Email a.b@example.com. Thanks!", ["Email a.b@example.com.", "Thanks!"]),
    ("访问example.com。下一句。", ["访问example.com。", "下一句。"]),
    ("访问example.com/search?q=1.2。下一句。", ["访问example.com/search?q=1.2。", "下一句。"]),
    ("参照https://example.com/search?q=why?です。次。", ["参照https://example.com/search?q=why?です。", "次。"]),
    ("Wait... still thinking", ["Wait... still thinking"]),
    ("Wait… still thinking", ["Wait… still thinking"]),
    ("Wait... really? Yes.", ["Wait... really?", "Yes."]),
    ("Wait...! Now.", ["Wait...!", "Now."]),
    ("First sentence. Last fragment", ["First sentence.", "Last fragment"]),
])
def test_final_sentence_splitting_preserves_text(text, expected):
    assert split_sentences(text) == expected
    # The only permitted loss is whitespace around each output piece.
    assert "".join("".join(expected).split()) == "".join(text.split())


@pytest.mark.parametrize("text, expected", [
    ("", ""),
    ("Incomplete phrase", ""),
    ("Hello. Welcome.", "Hello."),
    ("really? more words", "really?"),
    ("はい。はい。", "はい。"),
    ("你好！下一句", "你好！"),
    ('  \nHe asked ("Really?"). Next.', '  \nHe asked ("Really?").'),
    ("Dr.", ""),
    ("Mr. Smith", ""),
    ("A. B.", ""),
    ("e.g.", ""),
    ("i.e.", ""),
    ("U.S.", ""),
    ("Value 3.14", ""),
    ("Go to https://example.com/a?value=3.14", ""),
    ("Visit example.com", ""),
    ("访问example.com/search?q=1.2", ""),
    ("Visit https://example.com/search?q=why?", ""),
    ("Visit example.com?", ""),
    ("Wait...", ""),
    ("Wait..", ""),
    ("Wait…", ""),
    ("Wait... next sentence! trailing", "Wait... next sentence!"),
    ("What?!』 Next", "What?!』"),
])
def test_completed_prefix_is_conservative_and_exact(text, expected):
    assert completed_sentence_prefix(text) == expected
    assert text.startswith(expected)


def test_repeated_transcripts_are_never_deduplicated_between_calls():
    for _ in range(3):
        assert split_sentences("Thank you. Thank you.") == ["Thank you.", "Thank you."]
        assert completed_sentence_prefix("Thank you. Thank you.") == "Thank you."
