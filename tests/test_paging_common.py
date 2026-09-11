"""ページ指定の共通部品のテスト (builtin_data/tools/_paging_common.py)。

正典: docs/intent/room_item_display_cap.md 設計 3。

この部品が守るのは三つ:

- ``page`` の解釈 — ``"2"`` の一枚と ``"1-5"`` の範囲。一度に開けるのは 5 ページ
  まで。数字でない・0 以下・逆順は分かる文言で断る。
- 既にページの列に分けたものからの切り出し (切れ目の規則は呼び手が持つ)。
- 全体の案内行 —「全 58 件・6 ページ。いま 2 ページ目 (11〜20 件目)」。
"""
from __future__ import annotations

import unittest

from builtin_data.tools._paging_common import (
    MAX_PAGE_SPAN,
    PageRange,
    chunk_by_count,
    format_missing_page,
    format_page_guide,
    parse_page_arg,
    select_pages,
)


class ParsePageArgTest(unittest.TestCase):
    def test_single_page(self):
        page_range, error = parse_page_arg("2")
        self.assertIsNone(error)
        self.assertEqual((page_range.start, page_range.end), (2, 2))
        self.assertTrue(page_range.is_single)

    def test_range(self):
        page_range, error = parse_page_arg("1-5")
        self.assertIsNone(error)
        self.assertEqual((page_range.start, page_range.end), (1, 5))
        self.assertEqual(page_range.span, 5)

    def test_omitted_and_blank_mean_the_first_page(self):
        for value in (None, "", "   "):
            page_range, error = parse_page_arg(value)
            self.assertIsNone(error, value)
            self.assertEqual((page_range.start, page_range.end), (1, 1), value)

    def test_surrounding_spaces_are_allowed(self):
        page_range, error = parse_page_arg("  3-4 ")
        self.assertIsNone(error)
        self.assertEqual((page_range.start, page_range.end), (3, 4))

    def test_plain_int_is_accepted(self):
        page_range, error = parse_page_arg(3)
        self.assertIsNone(error)
        self.assertEqual((page_range.start, page_range.end), (3, 3))

    def test_zero_is_refused(self):
        page_range, error = parse_page_arg("0")
        self.assertIsNone(page_range)
        self.assertIn("ページ番号は 1 以上で指定してください", error)

    def test_negative_is_refused(self):
        page_range, error = parse_page_arg("-2")
        self.assertIsNone(page_range)
        self.assertIn("page は '2' のような番号か '1-5' のような範囲", error)

    def test_reversed_range_is_refused(self):
        page_range, error = parse_page_arg("5-2")
        self.assertIsNone(page_range)
        self.assertIn("ページの範囲は小さい番号から指定してください", error)

    def test_non_number_is_refused(self):
        for value in ("二", "1.5", "1〜3", "abc", 1.5, True):
            page_range, error = parse_page_arg(value)
            self.assertIsNone(page_range, value)
            self.assertIn("page は '2' のような番号か '1-5' のような範囲", error)

    def test_fullwidth_digits_are_refused(self):
        """⭐ ASCII 数字だけを受ける (``\\d`` は全角も通すので使っていない)。"""
        page_range, error = parse_page_arg("２")
        self.assertIsNone(page_range)
        self.assertIn("page は '2' のような番号か '1-5' のような範囲", error)

    def test_span_over_the_cap_is_refused(self):
        page_range, error = parse_page_arg("1-6")
        self.assertIsNone(page_range)
        self.assertIn(f"一度に開けるのは {MAX_PAGE_SPAN} ページまでです", error)
        self.assertIn("6 ページ分です", error)

    def test_span_exactly_at_the_cap_is_allowed(self):
        page_range, error = parse_page_arg("3-7")
        self.assertIsNone(error)
        self.assertEqual(page_range.span, MAX_PAGE_SPAN)


class SelectPagesTest(unittest.TestCase):
    def setUp(self):
        self.pages = chunk_by_count(list(range(1, 26)), 10)

    def test_chunk_by_count(self):
        self.assertEqual([len(p) for p in self.pages], [10, 10, 5])

    def test_single_page(self):
        self.assertEqual(select_pages(self.pages, PageRange(2, 2)), [list(range(11, 21))])

    def test_range_returns_each_page_separately(self):
        selected = select_pages(self.pages, PageRange(1, 2))
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0][0], 1)
        self.assertEqual(selected[1][-1], 20)

    def test_range_past_the_end_returns_what_exists(self):
        selected = select_pages(self.pages, PageRange(2, 5))
        self.assertEqual([len(p) for p in selected], [10, 5])

    def test_start_past_the_end_is_empty(self):
        self.assertEqual(select_pages(self.pages, PageRange(4, 4)), [])

    def test_variable_length_pages_are_taken_as_given(self):
        """⭐ 可変長のページ (手帳の日付の境目) もそのまま切り出せる。"""
        pages = [["a", "b", "c"], ["d"], ["e", "f"]]
        self.assertEqual(select_pages(pages, PageRange(2, 3)), [["d"], ["e", "f"]])


class FormatPageGuideTest(unittest.TestCase):
    def setUp(self):
        self.pages = chunk_by_count(list(range(1, 59)), 10)  # 58 件・6 ページ

    def test_single_page_guide(self):
        self.assertEqual(
            format_page_guide(self.pages, PageRange(2, 2)),
            "全 58 件・6 ページ。いま 2 ページ目 (11〜20 件目)。"
            "page='N' で他のページを開けます。",
        )

    def test_range_guide(self):
        self.assertEqual(
            format_page_guide(self.pages, PageRange(1, 3)),
            "全 58 件・6 ページ。いま 1〜3 ページ目 (1〜30 件目)。"
            "page='N' で他のページを開けます。",
        )

    def test_range_past_the_end_counts_only_what_is_shown(self):
        self.assertEqual(
            format_page_guide(self.pages, PageRange(5, 9)),
            "全 58 件・6 ページ。いま 5〜6 ページ目 (41〜58 件目)。"
            "page='N' で他のページを開けます。",
        )

    def test_variable_length_pages_count_real_entries(self):
        """⭐ 件数は実際のページの長さから数える (等分の仮定を置かない)。"""
        pages = [["a", "b", "c"], ["d"], ["e", "f"]]
        self.assertEqual(
            format_page_guide(pages, PageRange(2, 2)),
            "全 6 件・3 ページ。いま 2 ページ目 (4 件目)。"
            "page='N' で他のページを開けます。",
        )

    def test_a_book_of_one_page_does_not_promise_other_pages(self):
        pages = [["a", "b"]]
        self.assertEqual(
            format_page_guide(pages, PageRange(1, 1)),
            "全 2 件・1 ページ。いま 1 ページ目 (1〜2 件目)。",
        )

    def test_empty_pages_produce_no_guide(self):
        self.assertEqual(format_page_guide([], PageRange(1, 1)), "")

    def test_unit_is_configurable(self):
        pages = [["a", "b"], ["c"]]
        self.assertIn("全 3 個・2 ページ", format_page_guide(pages, PageRange(1, 1), unit="個"))


class FormatMissingPageTest(unittest.TestCase):
    def test_message_names_the_total_and_the_missing_page(self):
        pages = chunk_by_count(list(range(1, 26)), 10)
        self.assertEqual(
            format_missing_page(pages, PageRange(5, 5)),
            "全 3 ページです。5 ページ目はありません。",
        )


if __name__ == "__main__":
    unittest.main()
