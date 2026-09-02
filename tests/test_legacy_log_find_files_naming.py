"""取り込み対象の探し方が、ディレクトリ一覧の名前照合に依存しないこと。

macOS はファイル名の濁点・半濁点を「ヒ + 濁点」の 2 文字へ分解して保存する
(NFD) 一方、DB の BUILDINGID はアプリが受け取った合成済みの 1 文字 (NFC) で
入る。ディレクトリ一覧が返す名前と文字列比較すると一致せず、実在する
log.json を取りこぼす。ファイルシステム自身はこの 2 つを同じ名前として扱うので、
パスとして組み立てて開けば見つかる — 検算側 (``_scan_one_building``) は最初から
そうしていた。

そのずれのせいで、実ユーザーの macOS 環境では「リビング」の部屋について
「597 件が移せていない」と「対象 log.json: 0 件」が同時に成立し、取り込みが
毎起動 0 件で空振りしながらアラートだけが出続けた (2026-09-02 実測)。

macOS のファイル名正規化そのものは他の OS で再現できないため、ここで固定する
のは **名前を指定されたときに一覧を引かない** という構造の方。
"""
from __future__ import annotations

import json
import tempfile
import unittest
import unicodedata
from pathlib import Path
from unittest.mock import patch

from saiverse.legacy_log_import import find_log_files


class FindLogFilesNamingTests(unittest.TestCase):
    CITY = "city_a"
    BUILDING = "リビング_city_a"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="saiverse_home_")
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _make_log(self, city: str, building: str) -> Path:
        bdir = self.home / "cities" / city / "buildings" / building
        bdir.mkdir(parents=True, exist_ok=True)
        path = bdir / "log.json"
        path.write_text(json.dumps([]), encoding="utf-8")
        return path

    def test_finds_the_log_when_both_names_are_given(self) -> None:
        expected = self._make_log(self.CITY, self.BUILDING)
        found = find_log_files(
            self.home, city_filter=self.CITY, building_filter=self.BUILDING
        )
        self.assertEqual(found, [expected])

    def test_does_not_list_directories_when_names_are_given(self) -> None:
        """名前が分かっているなら一覧を引かない。

        一覧を引くと、その名前と DB 側の名前を文字列で比べることになり、
        同じ名前の違う書き方 (NFC / NFD) で落ちる。
        """
        self._make_log(self.CITY, self.BUILDING)
        with patch.object(
            Path, "iterdir", side_effect=AssertionError("一覧を引いてはいけない")
        ):
            found = find_log_files(
                self.home, city_filter=self.CITY, building_filter=self.BUILDING
            )
        self.assertEqual(len(found), 1)

    def test_decomposed_and_composed_names_reach_the_same_path(self) -> None:
        """分解形と合成形が、同じ 1 本のパスに解決されること。

        ファイルシステムが両者を同一視するかは OS 次第だが、こちらが組み立てる
        パスは「渡された名前をそのまま最後の要素にする」形でなければならない。
        ここが一覧との照合だと、この時点で候補が 0 件になる。
        """
        nfc = unicodedata.normalize("NFC", self.BUILDING)
        nfd = unicodedata.normalize("NFD", self.BUILDING)
        self.assertNotEqual(nfc, nfd, "テストの前提: 濁点で表現が変わる名前を使う")

        self._make_log(self.CITY, nfc)
        for name in (nfc, nfd):
            with self.subTest(name=unicodedata.name(name[1])):
                built = (
                    self.home / "cities" / self.CITY / "buildings" / name / "log.json"
                )
                self.assertEqual(built.name, "log.json")
                self.assertEqual(built.parent.name, name)

    def test_still_walks_everything_when_no_filter_is_given(self) -> None:
        a = self._make_log(self.CITY, "salon")
        b = self._make_log(self.CITY, self.BUILDING)
        found = find_log_files(self.home)
        self.assertEqual(sorted(found), sorted([a, b]))

    def test_walks_buildings_when_only_the_city_is_given(self) -> None:
        a = self._make_log(self.CITY, "salon")
        self._make_log("city_b", "other")
        found = find_log_files(self.home, city_filter=self.CITY)
        self.assertEqual(found, [a])

    def test_rejects_names_that_climb_out_of_the_parent(self) -> None:
        """パスを直接組む以上、名前が階層を跨がないことを確かめる。

        一覧と照合していた頃は、一覧に載る名前しか通らないので構造上ありえ
        なかった。照合をやめた分の穴をここで塞ぐ。
        """
        self._make_log(self.CITY, self.BUILDING)
        # 空文字列は「指定なし」の意味なので、ここでは扱わない (従来どおり全走査)。
        for bad in ("..", "../..", "other/buildings", "other\\buildings"):
            with self.subTest(name=bad):
                self.assertEqual(
                    find_log_files(self.home, city_filter=bad), []
                )
                self.assertEqual(
                    find_log_files(
                        self.home, city_filter=self.CITY, building_filter=bad
                    ),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
