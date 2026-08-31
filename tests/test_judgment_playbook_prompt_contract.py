"""判断 Playbook のプロンプト契約 — 退役した欄をプロンプトが指示していないこと。

判断点は構造化出力で動く: ``judgment_points`` が response_schema を組み立て、
``judgment_finalize`` がその中身だけを適用する。欄を退役させるときは
**スキーマ・finalize・プロンプトの三点**を同時に落とさなければならない。
スキーマと finalize だけ落とすと、プロンプトは存在しない欄を書けと言い続け、
ペルソナは毎回「書けと言われたのに書く場所が無い」指示を読まされる
(2026-08-21 Codex 指摘 7 — 束 6b でスキーマ側だけ落ちていた)。

人の目で追う代わりに機械で押さえる: 退役した欄名が判断 Playbook の JSON
(プロンプト文面・説明文を含む全文) に現れたら落とす。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

PLAYBOOK_DIR = Path(__file__).resolve().parents[1] / "builtin_data" / "playbooks" / "public"

#: 退役した判断欄と、その退役先の記録。
#: - promotions   : 欲求 → 関心 (Track) の昇格。Track と欲求プールが機構ごと退役
#:                  (track_retirement.md §7.2 ④群 / autonomous_behavior_v3.md §8)
#: - new_desires  : 欲求候補の採取。同上
#: - desire_reviews: 欲求のたな卸し。同上
#: - track_op     : Track の完了宣言。同上
RETIRED_JUDGMENT_FIELDS = (
    "promotions",
    "new_desires",
    "desire_reviews",
    "track_op",
)

#: 退役した参照 namespace。コマの ref enum は task:N + "none" だけになった
#: (``judgment_points.collect_slot_ref_enum``)。プロンプトが desire:N / track:N を
#: 指せと書いていると、スキーマに無い値を毎回誘導することになる。
RETIRED_REF_NAMESPACES = ("desire:", "track:")

JUDGMENT_PLAYBOOKS = sorted(PLAYBOOK_DIR.glob("judgment_*.json"))


def test_judgment_playbooks_exist():
    """glob が空振りしたまま緑になる (= 何も検査していない) のを防ぐ。"""
    assert JUDGMENT_PLAYBOOKS, f"no judgment playbooks found under {PLAYBOOK_DIR}"


@pytest.mark.parametrize(
    "path", JUDGMENT_PLAYBOOKS, ids=lambda p: p.stem,
)
def test_judgment_playbook_does_not_mention_retired_fields(path: Path):
    text = path.read_text(encoding="utf-8")
    # JSON として壊れていないことも同時に押さえる (プロンプト編集の事故防止)
    json.loads(text)

    found = [name for name in RETIRED_JUDGMENT_FIELDS if name in text]
    assert not found, (
        f"{path.name} still mentions retired judgment fields {found}. "
        "スキーマ (saiverse/judgment_points.py) と judgment_finalize からは "
        "落ちているので、プロンプトも同じ便で落とすこと。"
    )


@pytest.mark.parametrize(
    "path", JUDGMENT_PLAYBOOKS, ids=lambda p: p.stem,
)
def test_judgment_playbook_does_not_mention_retired_ref_namespaces(path: Path):
    text = path.read_text(encoding="utf-8")
    found = [ns for ns in RETIRED_REF_NAMESPACES if ns in text]
    assert not found, (
        f"{path.name} still points at retired ref namespaces {found}. "
        "コマの ref enum は task:N と 'none' だけ "
        "(saiverse/judgment_points.py:collect_slot_ref_enum)。"
    )
