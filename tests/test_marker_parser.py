"""層1マーカーパーサ saiverse/marker_parser.py のテスト (life_concept_map.md §9.1 / §13-2)。

検証項目:
- 基本の抽出・剥離 / 複数マーカー / 日本語語句
- 参照接尾 (統一文法で解釈できる ref / できない ref)
- コードブロック・インラインコードの内側は対象外
- 非対称 (閉じない ==)・空マーク (====)・前後空白つきの無視
- マーカー無しテキストの高速パス (入力オブジェクトそのまま返す)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from saiverse.marker_parser import MarkSpan, extract_marks, strip_marks


class ExtractMarksBasicTests(unittest.TestCase):
    def test_single_mark(self):
        clean, spans = extract_marks("今日は==言葉の標本==を見つけた")
        self.assertEqual(clean, "今日は言葉の標本を見つけた")
        self.assertEqual(spans, [MarkSpan(quote="言葉の標本", purpose_ref=None)])

    def test_multiple_marks(self):
        clean, spans = extract_marks("==朝の光==と==夕方の音==が良かった")
        self.assertEqual(clean, "朝の光と夕方の音が良かった")
        self.assertEqual([s.quote for s in spans], ["朝の光", "夕方の音"])
        self.assertTrue(all(s.purpose_ref is None for s in spans))

    def test_quote_is_verbatim_in_clean_text(self):
        # 剥離後の本文に quote が逐語で含まれる = (message_id, quote) の
        # 引用アンカーが成立する (§9.1)
        clean, spans = extract_marks("これは==とても大事な発見==だと思う")
        self.assertIn(spans[0].quote, clean)

    def test_ascii_phrase(self):
        clean, spans = extract_marks("I found ==a specimen of words== today")
        self.assertEqual(clean, "I found a specimen of words today")
        self.assertEqual(spans[0].quote, "a specimen of words")


class ExtractMarksRefSuffixTests(unittest.TestCase):
    def test_parseable_short_ref(self):
        clean, spans = extract_marks("==言葉の標本==(task:3)を集めよう")
        self.assertEqual(clean, "言葉の標本を集めよう")
        self.assertEqual(spans[0].purpose_ref, "task:3")

    def test_parseable_uri_ref(self):
        clean, spans = extract_marks("==あの約束==(saiverse://self/track/2)")
        self.assertEqual(clean, "あの約束")
        self.assertEqual(spans[0].purpose_ref, "saiverse://self/track/2")

    def test_unparseable_ref_keeps_paren_as_prose(self):
        # 統一文法で解釈できない "(...)" はマーカー記法の一部とみなさず
        # 地の文として残す。語句マークだけ (purpose_ref=None) が打たれる
        clean, spans = extract_marks("==強調したい==(ただの補足)なのだ")
        self.assertEqual(clean, "強調したい(ただの補足)なのだ")
        self.assertEqual(spans[0].purpose_ref, None)
        self.assertEqual(spans[0].quote, "強調したい")

    def test_single_letter_prefix_is_not_canon_grammar(self):
        # intent doc 初期案の (t:3) 形は references.py の正典 (単語 kind) 外
        clean, spans = extract_marks("==言葉の標本==(t:3)")
        self.assertEqual(clean, "言葉の標本(t:3)")
        self.assertIsNone(spans[0].purpose_ref)

    def test_detached_paren_is_not_a_ref(self):
        # "(...)" は閉じ "==" に密着している時だけ参照接尾
        clean, spans = extract_marks("==語句== (task:3)")
        self.assertEqual(clean, "語句 (task:3)")
        self.assertIsNone(spans[0].purpose_ref)


class ExtractMarksCodeExclusionTests(unittest.TestCase):
    def test_fenced_code_block_excluded(self):
        text = "外の==印==\n```python\nassert a ==b== c\n```\n後ろ"
        clean, spans = extract_marks(text)
        self.assertEqual(clean, "外の印\n```python\nassert a ==b== c\n```\n後ろ")
        self.assertEqual([s.quote for s in spans], ["印"])

    def test_unclosed_fence_protects_to_end(self):
        text = "前==印==\n```\ncode ==x== code"
        clean, spans = extract_marks(text)
        self.assertEqual(clean, "前印\n```\ncode ==x== code")
        self.assertEqual([s.quote for s in spans], ["印"])

    def test_inline_code_excluded(self):
        clean, spans = extract_marks("この `a ==b== c` は対象外で==これ==は対象")
        self.assertEqual(clean, "この `a ==b== c` は対象外でこれは対象")
        self.assertEqual([s.quote for s in spans], ["これ"])

    def test_code_only_text_unchanged(self):
        text = "```\n==全部コード==\n```"
        clean, spans = extract_marks(text)
        self.assertEqual(clean, text)
        self.assertEqual(spans, [])


class ExtractMarksIgnoreTests(unittest.TestCase):
    def test_unclosed_marker_left_as_is(self):
        text = "これは==閉じない"
        clean, spans = extract_marks(text)
        self.assertEqual(clean, text)
        self.assertEqual(spans, [])

    def test_empty_marker_left_as_is(self):
        text = "空の====は無視"
        clean, spans = extract_marks(text)
        self.assertEqual(clean, text)
        self.assertEqual(spans, [])

    def test_whitespace_padded_marker_left_as_is(self):
        # "x == y == z" のような地の文の等号連鎖を食わない
        text = "if x == y == z なら成立"
        clean, spans = extract_marks(text)
        self.assertEqual(clean, text)
        self.assertEqual(spans, [])

    def test_marker_spanning_newline_not_matched(self):
        text = "==行を\n跨ぐ=="
        clean, spans = extract_marks(text)
        self.assertEqual(clean, text)
        self.assertEqual(spans, [])


class ExtractMarksFastPathTests(unittest.TestCase):
    def test_no_marker_returns_same_object(self):
        text = "マーカーの無い普通の文章。"
        clean, spans = extract_marks(text)
        self.assertIs(clean, text)
        self.assertEqual(spans, [])

    def test_empty_and_none_like_inputs(self):
        self.assertEqual(extract_marks(""), ("", []))

    def test_strip_marks_convenience(self):
        self.assertEqual(strip_marks("見て、==これ==(task:1)だよ"), "見て、これだよ")
        plain = "剥がすものが無い"
        self.assertIs(strip_marks(plain), plain)


if __name__ == "__main__":
    unittest.main()
