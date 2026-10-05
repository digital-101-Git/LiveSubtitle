import pytest

from engine.audio import forced_overlap_candidate, remove_forced_overlap, seam_repetition


@pytest.mark.parametrize('previous,current,expected', [
    ('他说这是一个测试。', '一个，测试已经完成。', '已经完成。'),
    ('結果をもう一度確認します。', '確認します、続けてください。', '続けてください。'),
    ('今天的天气很好。', '天气很好，我们出发吧。', '我们出发吧。'),
    ('今天的天气很好。', '天气很好。', '天气很好。'),
    ('你听我说。', '你听我说完再回答。', '你听我说完再回答。'),
    ('等等。你听我说。', '你听我说完再回答。', '你听我说完再回答。'),
    ('他一直哈哈哈哈', '哈哈哈哈然后走了', '哈哈哈哈然后走了'),
    ('hello beautiful world', 'beautiful world again', 'again'),
    ('a contest', 'test again', 'test again'),
    ('test', 'testing', 'testing'),
    ('hello', 'hello again', 'hello again'),
    ('他很喜欢这里。', '这里确实不错。', '这里确实不错。'),
    ('不要再欺负的？', '欺负的结果很严重。', '欺负的结果很严重。'),
])
def test_normalized_overlap_preserves_short_and_actual_repetition(previous, current, expected):
    assert remove_forced_overlap(previous, current, 'zh') == expected


def test_chinese_script_comparison_preserves_original_remaining_text(monkeypatch):
    from engine import text_normalization
    monkeypatch.setattr(text_normalization, 'chinese_comparison_text',
                        lambda text: text.translate(str.maketrans({'測':'测', '試':'试', '經':'经', '結':'结', '束':'束'})))
    assert remove_forced_overlap('這是一個測試結果。', '測试结果，已經結束。', 'zh') == '已經結束。'


@pytest.mark.parametrize('seam,decision', [
    ('提醒你听我说完再回答。', 'single'),
    ('提醒你听我说，听我说完再回答。', 'repeated'),
    ('听我说完再回答。', 'ambiguous'),
    ('提醒你说完再回答。', 'ambiguous'),
    ('提醒你听我说完再回答。提醒你听我说完再回答。', 'ambiguous'),
    ('提醒你听我说完再回答。提醒你听我说听我说完再回答。', 'ambiguous'),
    ('', 'ambiguous'),
])
def test_short_overlap_requires_unique_audio_observation_and_retains_actual_repeat(seam, decision):
    match = forced_overlap_candidate('提醒你听我说。', '听我说完再回答。', 'zh')
    assert match.key == '听我说'
    assert seam_repetition(match, seam, 'zh') == decision
    # Candidate detection alone must never remove these three characters.
    assert remove_forced_overlap('提醒你听我说。', '听我说完再回答。', 'zh') == '听我说完再回答。'


def test_no_candidate_when_no_novel_suffix_or_one_character_or_latin_product():
    assert forced_overlap_candidate('他说不。', '不要走。', 'zh') is None
    assert forced_overlap_candidate('听我说。', '听我说。', 'zh') is None
    assert forced_overlap_candidate('HDTV。', 'TV更新了。', 'zh') is None


def test_homophone_names_are_never_equalized():
    assert forced_overlap_candidate('他叫白小棠。', '白小糖已经来了。', 'zh') is None
