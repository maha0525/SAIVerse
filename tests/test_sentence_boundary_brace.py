"""_find_next_sentence_boundary: ``{`` から ``}`` の間では区切らない。

仮想身体アドオンの全身エモート記法 ``{"body_emote": "wave"}`` は弱い区切り
(``:`` ``,``) を含む。途中で割れて voice-tts へ届くと、記法と気づけずに読み上げる。
"""
from sea.runtime_llm import _find_next_sentence_boundary


def _split(text: str) -> list[str]:
    parts, pos = [], 0
    while True:
        end = _find_next_sentence_boundary(text, pos)
        if end < 0:
            break
        parts.append(text[pos:end])
        pos = end
    if pos < len(text):
        parts.append(text[pos:])
    return parts


def test_emote_marker_stays_in_one_piece():
    text = 'おはよう。{"body_emote": "wave"}今日もいい天気だね。'
    assert _split(text) == ['おはよう。', '{"body_emote": "wave"}今日もいい天気だね。']


def test_boundary_after_closing_brace_still_splits():
    text = '{"body_emote": "nod"}うん、そうだね。'
    assert _split(text) == ['{"body_emote": "nod"}うん、', 'そうだね。']


def test_unclosed_brace_waits_for_more_text():
    assert _find_next_sentence_boundary('やあ{"body_emote":', 2) == -1


def test_brace_that_never_closes_nearby_does_not_stall_later_sentences():
    code = '{' + 'x' * 80
    text = f'コードは{code}です。次の文。'
    assert _split(text) == [f'コードは{code}です。', '次の文。']


def test_plain_sentences_unchanged():
    assert _split('こんにちは、元気？うん。') == ['こんにちは、', '元気？', 'うん。']
