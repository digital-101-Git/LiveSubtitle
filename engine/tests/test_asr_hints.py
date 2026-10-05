"""User vocabulary validation and prompt construction; no models or files."""
import pytest

from engine.asr_hints import build_asr_hint_prompt, validate_asr_hints


def test_empty_hints_keep_prompt_completely_absent():
    assert validate_asr_hints([]) == []
    assert validate_asr_hints(["", "  "]) == []
    assert build_asr_hint_prompt([]) == ""
    assert build_asr_hint_prompt(["  "]) == ""


def test_normalization_deduplicates_only_identical_source_terms_without_mutation():
    original = ["  Cafe\u0301 ", "Café", "", "RTX 4080", "棠", "糖", "Word", "word"]
    before = original.copy()
    assert validate_asr_hints(original) == ["Café", "RTX 4080", "棠", "糖", "Word", "word"]
    assert original == before


def test_prompt_contains_only_supplied_terms_and_a_fixed_vocabulary_label():
    assert build_asr_hint_prompt([" 東京 ", "Hunyuan", "東京"]) == "Vocabulary: 東京, Hunyuan"


@pytest.mark.parametrize("value", [None, "Tokyo", ("Tokyo",), {}, [None], [True], [1],
    ["A" * 49], [str(i) for i in range(33)],
    [str(i).ljust(48, "x") for i in range(11)],
    ["one\ntwo"], ["one\rtwo"], ["one\ttwo"], ["a\0b"], ["a\u200bb"], ["a\ud800b"],
    ["a\u2028b"], ["a\u2029b"],
    ["<|im_start|>"], ["x|>y"], ["<asr_text>"], ["[INST]"], ["<end_of_turn>"],
])
def test_invalid_hints_are_rejected_without_echoing_user_input(value):
    with pytest.raises(ValueError) as error:
        validate_asr_hints(value)
    assert "<|im_start|>" not in str(error.value)


def test_exact_limits_are_accepted_and_prompt_does_not_truncate_terms():
    terms = [f"{i:02}" + "字" * 14 for i in range(32)]
    assert len(terms) == 32 and sum(map(len, terms)) == 512
    assert validate_asr_hints(terms) == terms
    assert all(term in build_asr_hint_prompt(terms) for term in terms)
    assert validate_asr_hints(["字" * 48]) == ["字" * 48]
