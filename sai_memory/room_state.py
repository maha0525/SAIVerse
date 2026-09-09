"""部屋の様子 (room state) — パッケージの束を構造のまま運び、差分はキー照合で組む。

設計の正典: docs/intent/room_state_packages.md (2026-09-06)。発端は
docs/issues/archive/room_state_diff_built_on_string_parsing.md — 差分を描画済み文字列の
解析 (空行 = アイテムの境目、という推測) で組んでいたため、開いたドキュメントの
本文段落が「見当たらなくなったもの」に化けて v0.3.9 の出荷を止めた。

芯は一つ:

- **部屋はパッケージの束のまま運ぶ。** パッケージ = ``{key, family, label,
  lines, media, state}``。描画側 (``builtin_data/tools/get_visual_context.py``)
  がアイテムを一個ずつ組み立てた構造をそのまま受け取り、一枚の文字列に畳むのは
  **消費の組成の一回だけ** (:func:`render_pending_room_states` — 積む側は束を
  記帳するのみ。2026-09-06 まはー裁定、intent §11-2 規則 2)。逆方向
  (文字列 → 構造) は二度とやらない。
- **差分はキーの照合** (:func:`render_room_diff`)。新登場は全文 + その
  パッケージのメディア、Close→Open は「(開かれた)」の出来事行 + 開いて初めて
  見える行 + メディア (説明・作成日時は再掲しない — 2026-09-06 実機裁定)、
  消えたものは label の一行、Open→Close は「(閉じられた)」の一行、open の
  ままの本文変化は行単位の diff (全文より大きければ全文)、部屋全体で変化が
  無ければ一行。
- **メディアはパッケージの持ち物。** パッケージを新しく見せる瞬間 (新登場・
  Close→Open・全文への開き直し・移管での復元) にそのメディアも一緒に運ぶ。
  変わっていないパッケージのメディアは再添付しない — その絵は土台のバッチに
  まだ付いている。

台帳の器 (``perception_buffer``) は変えない。バッチの記帳 ``room_state_json`` の
``snapshot`` が「文字列一枚」から「パッケージの束 (JSON オブジェクト)」になり、
土台の指紋 ``base_digest`` は束の正準 JSON (:func:`canonical_bundle_json`) の
sha256 になっただけ。

不変条件 (v0.3.9 から引き継ぎ + 改訂):

1. **今いる部屋の全体像が、提示のどこかに常に見えている。** 担い手は二段構え —
   生き残りが居る回は既存の移管・回復 (:func:`restore_room_state_bases` /
   :func:`reopen_lost_bases`)、最後の運搬役が
   下りる回は :func:`reseat_current_room` (付記・境界前進と同一トランザクション
   で、最新の全文を提示の最古端へ置き直す)、それでも漏れた形は検知の瞬間の
   自己回復 (sea/head_pipeline/integration.py の部屋の照合) が拾う。
2. 提示に見えるどの差分も、土台がその直前に見える (連なり)。指紋の中身が正準
   JSON の sha256 に変わっただけで、照合の形は同じ。
3. 提示の書き換えは、編纂の付記・境界前進と同一トランザクションだけ。
   自己回復の置き直しは例外 (部屋が見えていない異常の一回きりの修復)。
4. 台帳の行 (``perception_buffer``) は書き換えない — 変わるのは提示の正準
   (``perception_batches.rendered_text`` / ``media`` / ``room_state_json``)
   だけ。

**旧形式 (文字列 snapshot) との互換**: 構造照合できないので**土台なし扱い** —
連なりに参加しない (土台にもならず、開き直しもされない)。次の入室が一度だけ
全文を積み、以後は構造つきで運ぶ。旧データの読者は書かない (2026-09-06 裁定)。

**head 照合機構は退役** (2026-09-06): head の VisualContextSection が部屋の描画
ごと退役したので、``base_source="head"`` の差分・提示時の head 部屋判定・
実行 model の照合はすべて消えた。部屋の様子の置き場は知覚 (tail) 一つ。
"""
from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import logging
import sqlite3
import time
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

LOGGER = logging.getLogger(__name__)

#: 部屋の様子を積む知覚の型 (``perception_buffer.kind``)。見出しは付かない
#: (``_KIND_HEADERS`` で "" — 本文が ``# 「X」の様子`` から始まり自己完結する)。
ROOM_STATE_KIND = "surroundings"

#: 知覚台帳の ``metadata`` に載せるキー。
ROOM_STATE_META_KEY = "room_state"

#: 通知ラベルの型付け (docs/intent/room_state_packages.md §11-3-2)。書き手は
#: sea/head_pipeline/sections/building.py (NotificationLabel.metadata)、運び手は
#: sea/head_pipeline/integration.py (label.metadata を知覚エントリの metadata へ
#: 写す)、読み手は :func:`reclaim_pending_perceptions` (§11-2 の移動群の畳み)。
LABEL_KIND_META_KEY = "label_kind"
LABEL_KIND_BUILDING_CHANGED = "building_changed"
#: 役割・指示エントリの型。書き手は 2026-09-07 に退役 (§11-3 改訂 — 指示は束の
#: building:prompt パッケージが運ぶ) — 回収 (§11-2 規則 3) が旧コードの積んだ
#: 遺物を識別するためだけに残る。
LABEL_KIND_BUILDING_INSTRUCTION = "building_instruction"

#: 再会の想起を積む知覚の型 (``perception_buffer.kind``)。
RECALL_KIND = "persona_recall"

#: 同席想起の印 (metadata の JSON に載るキー)。書き手は
#: sea/head_pipeline/integration.py の ``inject_copresence_recall`` — Pulse の頭で
#: 「いま同席している相手」を見る新方式だけがこの印を付け、相手の ID を
#: :data:`RECALL_OCCUPANT_META_KEY` に併記する (診断とプレビュー用)。印の無い
#: ``persona_recall`` は、移動の瞬間に積んでいた旧方式 (2026-09-07 退役) の遺物
#: なので、回収 (:func:`reclaim_pending_perceptions` の §11-2 規則 3(c)) が捨てる。
RECALL_COPRESENCE_META_KEY = "copresence"
RECALL_OCCUPANT_META_KEY = "occupant_id"

#: 経路一行 (§11-2 の移動群の畳みの合成文) の書き出し。
_MOVE_TRAIL_PREFIX = "この間に現在地が移動しました: "

_NO_CHANGE_LINE = "前回見たときから変わっていません。"
_DIFF_TITLE_SUFFIX = " (前回見たときからの変化)"
_ADDED_HEADING = "## 増えた・変わったもの"
_GONE_HEADING = "## 見当たらなくなったもの"
_TAIL_LINE = "これ以外は前回と同じです。"
_CLOSED_SUFFIX = " (閉じられた)"
_OPENED_LINE = "(開かれた)"
_LINE_DIFF_SUFFIX = " (変わった行だけ)"

#: アイテム描画の open/closed の状態マーカー行。書き手は
#: builtin_data/tools/get_visual_context.py (_render_item) で、ここから import
#: して使う (文字列の知識は一枚)。差分側で読むのは Close→Open の描画
#: (:func:`_render_opened`) だけ — 状態は出来事行「(開かれた)」が語るので、
#: 開いて初めて見える行からマーカーを除くのに使う。
STATE_MARKER_OPEN = "(Open)"
STATE_MARKER_CLOSED = "(Closed)"


def room_key(building_id: str) -> str:
    """同じ部屋かどうかの判定キー。

    Building が部屋の同一性の単位 (部屋の様子は Building 単位で作られる)。
    """
    return f"building:{building_id}"


# ---------------------------------------------------------------------------
# パッケージの束 (bundle)
# ---------------------------------------------------------------------------
#
# bundle = {"building_id": str, "building_name": str, "packages": [package...]}
# package = {"key": str, "family": str, "label": str, "lines": [str...],
#            "media": [{"path","mime_type","type"}...], "state": "open"|"closed"|None}
#
# family は描画の節を決めるためだけの印 ("persona" / "user" / "interior" /
# "prompt" / "item" / "fixture")。差分の同一性は key が持つ
# (docs/intent/room_state_packages.md §3 の表)。


#: パッケージの ``state`` が取りうる値 (intent §3 — open / closed / 概念なし)。
_PACKAGE_STATES = (None, "open", "closed")


def bundle_is_valid(bundle: Any) -> bool:
    """パッケージの束として扱える形か (旧形式 = 文字列 snapshot は False)。

    利用側が実際に読むフィールドの型まで検める共通 validator — ここを通った
    束は描画 (:func:`render_room_full` / :func:`render_room_diff`)・指紋
    (:func:`snapshot_digest`)・メディア (:func:`bundle_media`) がそのまま
    読める。検査は読まれる実フィールドだけ: top-level の building_id /
    building_name (文字列) と packages (list — 空は正当な空室)、各パッケージの
    key (空でない文字列・**束の中で一意**) / family / label (文字列) /
    lines (文字列の list) / media (dict の list — path は非空文字列、
    mime_type は存在するなら文字列) / state (open / closed / None)。読まれ
    ないフィールドの有無では落とさない (過剰に厳格にしない)。

    キーの一意性も読み手の前提: 差分の組成 (:func:`render_room_diff`) は
    パッケージをキーで辞書化するので、重複キーの束を有効と数えると片方が
    静かに上書きされ、全文 (走査順) と差分 (辞書) の整合が崩れる。組成側
    (build_room_bundle) は先勝ち + WARN で弾いているが、それは組成時の弾き —
    保存済みの束 (記帳破損を含む) の検証はこちらの仕事 (2026-09-06 九巡目
    修正 2)。

    浅い検査 (dict + packages が list) だけだと、型の壊れた記帳が
    :func:`first_room_bundle` の停止規則 (旧形式・不正束 = 材料なし・土台なし)
    を素通りして、壊れた土台への差分や壊れた束の置き直しが静かに確定する
    (2026-09-06 七巡目修正 1)。
    """
    if not isinstance(bundle, dict):
        return False
    if not isinstance(bundle.get("building_id"), str):
        return False
    if not isinstance(bundle.get("building_name"), str):
        return False
    packages = bundle.get("packages")
    if not isinstance(packages, list):
        return False
    seen_keys: set = set()
    for package in packages:
        if not isinstance(package, dict):
            return False
        key = package.get("key")
        if not isinstance(key, str) or not key:
            return False
        if key in seen_keys:
            return False  # 重複キー = 記帳破損 (差分の辞書化で片方が消える)
        seen_keys.add(key)
        if not isinstance(package.get("family"), str):
            return False
        if not isinstance(package.get("label"), str):
            return False
        lines = package.get("lines")
        if not isinstance(lines, list) or not all(
            isinstance(line, str) for line in lines
        ):
            return False
        media = package.get("media")
        if not isinstance(media, list):
            return False
        for m in media:
            # 消費契約どおりの型まで検める (2026-09-06 八巡目修正 2):
            # path は set への in 照合 (:func:`bundle_media`) とファイルパス
            # として、mime_type は LLM クライアントがそのまま API へ渡す値
            # として読まれる。truthiness だけだと {"path": ["x"]} が有効束を
            # 名乗り、検証済みの束が後段の bundle_media で TypeError になる。
            if not isinstance(m, dict):
                return False
            path = m.get("path")
            if not isinstance(path, str) or not path:
                return False
            if "mime_type" in m and not isinstance(m["mime_type"], str):
                return False
        if package.get("state") not in _PACKAGE_STATES:
            return False
    return True


def canonical_bundle_json(bundle: Mapping[str, Any]) -> str:
    """束の正準 JSON。指紋 (:func:`snapshot_digest`) はこの文字列から取る。

    dict キーの順だけを固定する (``sort_keys``)。パッケージの並びは list なので
    組成側の決定論 (get_visual_context の family 内ソート) がそのまま残る。
    """
    return json.dumps(
        bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def snapshot_digest(snapshot: Any) -> str:
    """土台の指紋。束なら正準 JSON の sha256。

    照合にしか使わないので中身は要らない — 全文をもう一枚記帳すると 1 万字級の
    重複になる。文字列を渡された場合 (旧形式・テストの素材) はその文字列自体の
    sha256 (旧形式は連なりに参加しないので、実運用でこの枝は照合に出ない)。
    """
    if isinstance(snapshot, Mapping):
        text = canonical_bundle_json(snapshot)
    else:
        text = str(snapshot)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _packages(bundle: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [p for p in bundle.get("packages", []) if isinstance(p, dict) and p.get("key")]


def _package_lines(package: Mapping[str, Any]) -> List[str]:
    lines = package.get("lines")
    if not isinstance(lines, list):
        return []
    return [str(line) for line in lines]


def _package_media(package: Mapping[str, Any]) -> List[Dict[str, Any]]:
    media = package.get("media")
    if not isinstance(media, list):
        return []
    return [m for m in media if isinstance(m, dict) and m.get("path")]


def _package_text(package: Mapping[str, Any]) -> str:
    return "\n".join(_package_lines(package))


def bundle_media(bundle: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """束の全パッケージのメディア (path で重複排除、束の並び順)。"""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for package in _packages(bundle):
        for m in _package_media(package):
            path = m.get("path")
            if path in seen:
                continue
            seen.add(path)
            out.append(m)
    return out


def _families(bundle: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    by_family: Dict[str, List[Dict[str, Any]]] = {}
    for package in _packages(bundle):
        by_family.setdefault(str(package.get("family") or ""), []).append(package)
    return by_family


def render_room_full(bundle: Mapping[str, Any]) -> str:
    """束から知覚向けの全文を導出する (決定論 — 構造が正、文字列は導出物)。

    節立ては旧 ``get_visual_context(for_perception=True)`` の書式をそのまま
    引き継ぐ (ペルソナの目に映る形を変えないため)。節の有無・人数の一行は
    すべてパッケージの束から導出する。
    """
    name = str(bundle.get("building_name") or bundle.get("building_id") or "?")
    fam = _families(bundle)
    parts: List[str] = [f"# 「{name}」の様子", ""]

    parts.append("## 一緒にいるペルソナ")
    personas = fam.get("persona", [])
    if not personas:
        parts.append("他のペルソナはいません。")
    parts.append("")
    for package in personas:
        parts.extend(_package_lines(package))
        parts.append("")

    users = fam.get("user", [])
    if users:
        parts.append("## ユーザー")
        parts.append(f"現在、このBuildingには{len(users)}人のユーザーがいます。")
        for package in users:
            parts.extend(_package_lines(package))
        parts.append("")

    parts.extend(["---", "", "## Building"])
    for package in fam.get("interior", []):
        parts.extend(_package_lines(package))
        parts.append("")
    for package in fam.get("prompt", []):
        parts.extend(_package_lines(package))
        parts.append("")

    parts.extend(["---", "", "## Item", ""])
    items = fam.get("item", [])
    for package in items:
        parts.extend(_package_lines(package))
        parts.append("")
    if not items:
        parts.append("アイテムはありません。")
        parts.append("")

    fixtures = fam.get("fixture", [])
    if fixtures:
        parts.extend(["---", "", "## 設置物 (Fixture)", ""])
        for package in fixtures:
            parts.extend(_package_lines(package))
            parts.append("")

    # 未知の family (将来の族) は末尾にそのまま並べる — 黙って落とさない。
    known = {"persona", "user", "interior", "prompt", "item", "fixture"}
    for family, packages in fam.items():
        if family in known:
            continue
        for package in packages:
            parts.extend(_package_lines(package))
            parts.append("")

    return "\n".join(parts)


def _line_opcodes(
    old_lines: Sequence[str], new_lines: Sequence[str],
) -> List[Tuple[str, int, int, int, int]]:
    """行単位 diff の機構の一枚。

    §4 の「本文の変化」(:func:`_render_line_diff`) と Close→Open の「開いて
    初めて見える行」(:func:`_render_opened`) が同じこの一枚を通る — 同じ
    パッケージの lines 同士の比較なので、v0.3.9 を止めた「どこからどこまでが
    誰の文章か」の推測が無い。
    """
    return difflib.SequenceMatcher(
        a=old_lines, b=new_lines, autojunk=False,
    ).get_opcodes()


def _render_line_diff(old_pkg: Mapping[str, Any], new_pkg: Mapping[str, Any]) -> str:
    """open のままの本文変化を行単位の diff で描く。

    diff が全文より大きければ全文を返す (intent §4)。
    """
    old_lines = _package_lines(old_pkg)
    new_lines = _package_lines(new_pkg)
    diff_lines: List[str] = [str(new_pkg.get("label") or "") + _LINE_DIFF_SUFFIX]
    for tag, i1, i2, j1, j2 in _line_opcodes(old_lines, new_lines):
        if tag == "equal":
            continue
        if tag in ("delete", "replace"):
            diff_lines.extend(f"- {line}" for line in old_lines[i1:i2])
        if tag in ("insert", "replace"):
            diff_lines.extend(f"+ {line}" for line in new_lines[j1:j2])
    diff_text = "\n".join(diff_lines)
    full_text = _package_text(new_pkg)
    if len(diff_text) >= len(full_text):
        return full_text
    return diff_text


def _render_opened(old_pkg: Mapping[str, Any], new_pkg: Mapping[str, Any]) -> str:
    """Close→Open を出来事として描く (intent §4、2026-09-06 実機裁定)。

    形は「label + (開かれた) + 開いて初めて見える行」。「開いて初めて見える行」
    は、閉じた状態の描画 (old の lines) と開いた状態の描画 (new の lines) の
    行差分の新側 (:func:`_line_opcodes` — §4 の「本文の変化」と同じ機構) で
    求める。見出し・作成日時・説明は閉じている間も提示に出ていた (§3 の表 =
    equal 側に落ちる) ので再掲されない — 保証 2「同じ内容が二枚並ぶことは
    構造的に無い」。除くのは二種だけ: 状態マーカー行
    (:data:`STATE_MARKER_OPEN` — 状態は出来事行が語る) と、label と同じ行
    (先頭行が既に名乗っている — 閉じている間の改名で見出し行が差分の新側に
    入る形の重複防止)。
    """
    old_lines = _package_lines(old_pkg)
    new_lines = _package_lines(new_pkg)
    label = str(new_pkg.get("label") or new_pkg.get("key") or "")
    parts: List[str] = [label, _OPENED_LINE]
    for tag, _i1, _i2, j1, j2 in _line_opcodes(old_lines, new_lines):
        if tag not in ("insert", "replace"):
            continue
        parts.extend(
            line for line in new_lines[j1:j2]
            if line != STATE_MARKER_OPEN and line != label
        )
    return "\n".join(parts)


def render_room_diff(
    old_bundle: Mapping[str, Any], new_bundle: Mapping[str, Any],
) -> Dict[str, Any]:
    """二つの束のキー照合から、積む差分の本文とメディアを作る (決定論)。

    規則は intent §4 の表の写し:

    - 新登場 — 全文 + そのパッケージのメディア。
    - Close→Open — 「(開かれた)」の出来事行 + 開いて初めて見える行 (本文・
      中身・メディアリンク) + そのパッケージのメディア (:func:`_render_opened`)。
      説明・作成日時は閉じている間も提示に出ているので再掲しない
      (2026-09-06 実機裁定)。
    - 消えた — label の一行だけ (理由は描き分けない)。
    - Open→Close — 「(閉じられた)」の一行 (本文・説明を再掲しない)。
    - open のままの本文変化 — 行単位 diff。diff が全文より大きければ全文。
      メディアだけが変わった (絵の実体が差し替わった) パッケージは全文 +
      新しいメディア — 「絵がある」と読めるのに絵が無い状態を作らないため。
    - 部屋全体で変化なし — 一行。

    Returns: ``{"content": str, "media": [ ... ]}``。
    """
    name = str(new_bundle.get("building_name") or new_bundle.get("building_id") or "?")
    old_pkgs = {str(p["key"]): p for p in _packages(old_bundle)}
    new_pkgs = _packages(new_bundle)
    new_keys = {str(p["key"]) for p in new_pkgs}

    changed_blocks: List[str] = []
    media: List[Dict[str, Any]] = []
    seen_media: set = set()

    def _add_media(package: Mapping[str, Any]) -> None:
        for m in _package_media(package):
            path = m.get("path")
            if path in seen_media:
                continue
            seen_media.add(path)
            media.append(m)

    for package in new_pkgs:
        key = str(package["key"])
        old = old_pkgs.get(key)
        new_state = package.get("state")
        old_state = old.get("state") if old else None
        if old is None:
            # 新登場 = 全文 + メディア。
            changed_blocks.append(_package_text(package))
            _add_media(package)
            continue
        if old_state == "closed" and new_state == "open":
            # Close→Open = 「(開かれた)」+ 開いて初めて見える行 + メディア。
            changed_blocks.append(_render_opened(old, package))
            _add_media(package)
            continue
        if old_state == "open" and new_state == "closed":
            # Close はユーザーの「コンテキストから外す」意思 — 一行だけ。
            changed_blocks.append(str(package.get("label") or key) + _CLOSED_SUFFIX)
            continue
        lines_changed = _package_lines(old) != _package_lines(package)
        media_changed = _package_media(old) != _package_media(package)
        if lines_changed:
            changed_blocks.append(_render_line_diff(old, package))
            if media_changed:
                _add_media(package)
        elif media_changed or old_state != new_state:
            # 本文は同じでも絵の実体や状態が変わった — 全文で開き直して見せる。
            changed_blocks.append(_package_text(package))
            _add_media(package)

    gone_labels = [
        str(p.get("label") or key)
        for key, p in old_pkgs.items()
        if key not in new_keys
    ]

    if not changed_blocks and not gone_labels:
        return {"content": f"# 「{name}」の様子\n{_NO_CHANGE_LINE}", "media": []}

    parts: List[str] = [f"# 「{name}」の様子" + _DIFF_TITLE_SUFFIX, ""]
    if changed_blocks:
        parts.append(_ADDED_HEADING)
        parts.append("")
        for block in changed_blocks:
            parts.append(block)
            parts.append("")
    if gone_labels:
        parts.append(_GONE_HEADING)
        for label in gone_labels:
            parts.append(f"- {label}")
        parts.append("")
    parts.append(_TAIL_LINE)
    return {"content": "\n".join(parts), "media": media}


# ---------------------------------------------------------------------------
# 台帳の読み口
# ---------------------------------------------------------------------------

def _parse_item_state(metadata: Optional[str]) -> Optional[Dict[str, Any]]:
    """知覚台帳 1 行の ``metadata`` から部屋の様子の記帳を取り出す。"""
    if not metadata:
        return None
    try:
        meta = json.loads(metadata)
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    state = meta.get(ROOM_STATE_META_KEY)
    return state if isinstance(state, dict) else None


def is_legacy_entry(entry: Mapping[str, Any]) -> bool:
    """束として読めない記帳か (旧形式 = snapshot が文字列、および縮めた記帳)。

    True のエントリは連なりに参加しない — 土台にもならず、開き直しもされない
    (旧データの読者を書かない — intent §9)。提示には積んだときの文面のまま出る。
    次の入室は土台なし扱いで全文を積み、以後は構造つきで運ぶ。

    **提示の節約で縮めた部屋の記帳も同じ道に合流する**
    (:func:`sai_memory.presented_reduction.mark_presentation_reductions` は
    縮めるときに束を落とす)。だから「離れている間に縮めた部屋へ戻ったら全文と
    画像を見せ直す」(まはー裁定 2026-09-09) は、連なりの読み手を一枚も書き換え
    ずに成立する — 縮めた記帳は土台にならないので、戻った回の消費は土台なし =
    全文を積む。
    """
    return not bundle_is_valid(entry.get("snapshot"))


def _parse_label_meta(metadata: Optional[str]) -> Optional[Dict[str, Any]]:
    """知覚台帳 1 行の ``metadata`` から型付きラベルの記帳を取り出す。

    :data:`LABEL_KIND_META_KEY` を持つ dict-JSON だけが型付き。台帳配送の冪等
    キー (ledger_outbox_id 等) が同じ dict にマージされていても、そのまま読める。
    """
    if not metadata:
        return None
    try:
        meta = json.loads(metadata)
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    kind = meta.get(LABEL_KIND_META_KEY)
    if not isinstance(kind, str) or not kind:
        return None
    return meta


def _is_copresence_recall(metadata: Optional[str]) -> bool:
    """再会の想起が「Pulse の頭の同席チェック」(新方式) の印を持つか。

    印は :data:`RECALL_COPRESENCE_META_KEY` が真の dict-JSON。metadata が無い /
    読めない / 印が無いものは、移動の瞬間に積んでいた旧方式の遺物と見なす。
    """
    if not metadata:
        return False
    try:
        meta = json.loads(metadata)
    except (TypeError, ValueError):
        return False
    if not isinstance(meta, dict):
        return False
    return bool(meta.get(RECALL_COPRESENCE_META_KEY))


def reclaim_pending_perceptions(items: Sequence[Any]) -> List[Any]:
    """未消費の組成から、読まれる前に不要になった知覚を回収する (intent §11-2)。

    **純関数** — DB を読み書きせず、``items`` (reduce 済みの
    :class:`~sai_memory.perception_buffer.PerceptionItem` の列) から新しい list
    を返す。実 flush (saiverse_memory/adapter.flush_perception_buffer_payload)
    とプレビュー (sea/runtime_context._compose_pending_preview) が **reduce の
    後・消費時描画 (:func:`render_pending_room_states`) の前**に同じこの一枚を
    通る。

    未消費の知覚は定義上どの Pulse もまだ読んでいない (§11-1) — ここで畳んでも
    提示済みの列には 1 バイトも触れず、前方一致が割れる場所は存在しない。
    動作は三つ (intent §11-2 の規則の番号):

    1. **移動群の畳み**: 型付きの移動通知 (:data:`LABEL_KIND_BUILDING_CHANGED`
       — 書き手は sea/head_pipeline/sections/building.py) が 2 件以上 pending
       なら、経路一行「この間に現在地が移動しました: 「A」 → 「B」 → 「A」」
       (最初の通知の from の名前 + 各通知の to の名前を発生順に連結) を合成して
       **最後の移動通知の位置**に置き、それ以外の型付き移動通知を外す。1 件
       以下なら通知は触らない。metadata の無い旧ラベルの通知は畳まない
       (§11-3-2 — 小さいので実害なし)。
    2. **様子の畳み + 末尾寄せ**: 有効な様子エントリが複数 pending なら**最後の
       一つだけ**残す (部屋が違っても最後の一つ — 2026-09-07 まはー裁定「最後に
       いる部屋の様子だけ残せば良い」)。残した一枚は**組成の末尾へ動かす**
       (2026-09-07 まはー裁定 — 様子は「読む時点の部屋の状態」であって出来事
       ではない。出来事より前に状態の差分が出ると因果が逆に読める)。残した束の
       描画 (差分か全文か + 文字列への畳み) は後段の消費時描画
       (:func:`render_pending_room_states`) が「提示に見えている同部屋の末尾の
       束」を土台に行う — pending は土台にならないので、途中の pending を
       捨てても連なりは切れない (§11-2 規則 2)。入室配送の再試行による様子の
       二重積みもこれが自己修復する。
    3. **遺物の破棄**: (a) kind='surroundings' で :func:`is_legacy_entry` (束と
       して読めない旧文字列形式) の行、(b) 型付きの役割・指示エントリ
       (:data:`LABEL_KIND_BUILDING_INSTRUCTION` — 書き手は 2026-09-07 に退役。
       指示は束の building:prompt パッケージが運ぶので、独立エントリは様子との
       重複 — §11-3 改訂)、(c) kind='persona_recall' (:data:`RECALL_KIND`) で
       同席の印 (:data:`RECALL_COPRESENCE_META_KEY`) が**無い**行 (移動の瞬間に
       入室ラベルを目印として想起を積んでいた旧方式 — 2026-09-07 に退役。積んだ
       時点と読む時点で同席が変わるので「もう居ない相手との再会」になり、往復の
       たびに積み重なる) は、移動の件数に関係なく無条件で組成から外す。
       **印つきの想起は触らない** — Pulse の頭が積んだ新方式の想起は、その Pulse
       が読むはずのもので、クラッシュ等で読まれずに残った回も次の Pulse で正しく
       読まれるべき本物 (印は「いま同席している相手を見て積んだ」ことの証)。
       消費済みの印は呼び出し側の消費 (全 item id を渡す既存挙動) がそのまま
       付ける。

    移動・指示・様子以外のエントリ (スペル・フィード等) は位置ごと一切触らない
    (削除と差し替えのほかは、様子一枚だけを末尾へ動かし、他は並び替えない)。
    """
    keep = [True] * len(items)
    replacements: Dict[int, Any] = {}

    # 規則 2 + 3(a): 様子 — 旧形式は破棄し、有効なものは最後の一つだけ残す。
    valid_rooms: List[int] = []
    for index, item in enumerate(items):
        if getattr(item, "kind", None) != ROOM_STATE_KIND:
            continue
        state = _parse_item_state(getattr(item, "metadata", None)) or {}
        if is_legacy_entry(state):
            keep[index] = False
            LOGGER.info(
                "[room_state] reclaimed a legacy-format surroundings entry "
                "from the unconsumed buffer (it is consumed but not presented)",
            )
            continue
        valid_rooms.append(index)
    for index in valid_rooms[:-1]:
        keep[index] = False

    # 規則 1: 移動群 — 2 件以上なら経路一行に畳む。
    # 規則 3(b): 型付き指示エントリは無条件で破棄 (書き手は退役済みの遺物)。
    # 規則 3(c): 同席の印が無い再会の想起も同じく破棄 (旧方式の遺物)。
    moves: List[Tuple[int, Dict[str, Any]]] = []
    for index, item in enumerate(items):
        if getattr(item, "kind", None) == RECALL_KIND:
            if not _is_copresence_recall(getattr(item, "metadata", None)):
                keep[index] = False
                LOGGER.info(
                    "[room_state] reclaimed a legacy persona-recall entry from "
                    "the unconsumed buffer (pushed at move time by the retired "
                    "path; the pulse head recalls copresent partners now)",
                )
            continue
        meta = _parse_label_meta(getattr(item, "metadata", None))
        if meta is None:
            continue
        kind = meta.get(LABEL_KIND_META_KEY)
        if kind == LABEL_KIND_BUILDING_CHANGED:
            moves.append((index, meta))
        elif kind == LABEL_KIND_BUILDING_INSTRUCTION:
            keep[index] = False
            LOGGER.info(
                "[room_state] reclaimed a retired building-instruction entry "
                "from the unconsumed buffer (the bundle's building:prompt "
                "package carries the instruction now)",
            )
    if len(moves) >= 2:
        first_meta = moves[0][1]
        names = [
            str(first_meta.get("from_name") or first_meta.get("from_id") or "?")
        ]
        names.extend(
            str(meta.get("to_name") or meta.get("to_id") or "?")
            for _index, meta in moves
        )
        trail = _MOVE_TRAIL_PREFIX + " → ".join(f"「{name}」" for name in names)
        last_index = moves[-1][0]
        replacements[last_index] = dataclasses.replace(
            items[last_index], content=trail,
        )
        for index, _meta in moves[:-1]:
            keep[index] = False
        LOGGER.info(
            "[room_state] reclaimed %d pending move notification(s) into one "
            "trail line", len(moves),
        )

    # 規則 2 の末尾寄せ: 残した様子一枚を組成の末尾へ (既に末尾なら何もしない)。
    out: List[Any] = []
    room_position: Optional[int] = None
    for index, item in enumerate(items):
        if not keep[index]:
            continue
        if valid_rooms and index == valid_rooms[-1]:
            room_position = len(out)
        out.append(replacements.get(index, item))
    if room_position is not None and room_position != len(out) - 1:
        out.append(out.pop(room_position))
    return out


def chain_is_intact(
    entry: Mapping[str, Any], previous: Optional[Mapping[str, Any]],
) -> bool:
    """差分 ``entry`` の土台が ``previous`` として今も見えているか。

    差分は「同部屋の**直前**のエントリの束」から作る。連なりの検査は、積むとき
    に記帳した土台の指紋 (``base_digest``) と、直前に見えているエントリの
    ``snapshot`` (束) の指紋の一致で行う — 中間の一枚だけが提示から下りた形も
    これで捕まる。

    ``previous`` が None (この提示でこの部屋の最初のエントリ) なら常に False。
    指紋か束が読めない壊れた記帳も False (全文へ開き直す側に倒す — 余分な全文
    一枚は無害、土台の無い差分は復元不能)。
    """
    if previous is None:
        return False
    base = entry.get("base_digest")
    if not base:
        return False
    snapshot = previous.get("snapshot")
    if not bundle_is_valid(snapshot):
        return False
    return snapshot_digest(snapshot) == str(base)


def raw_room_entries(room_state_json: Optional[str]) -> Optional[List[Any]]:
    """バッチの ``room_state_json`` を**篩わずに**生の list として読む。

    壊れている (JSON でない / list でない) なら None。

    :func:`batch_room_states` は読む側の便宜で「dict で ``key`` を持つ要素」だけに
    絞るが、**記帳への書き戻しにその結果を使うと、篩で落ちた未知の要素が黙って
    消える**。記帳を UPDATE する箇所 (:func:`restore_room_state_bases` /
    :func:`sai_memory.presented_reduction.mark_presentation_reductions`) はこの生の
    並びを保ち、書き換える要素だけを差し替える (「記録は追加だけ」— ローカル
    レビュー指摘 2026-09-10)。
    """
    if not room_state_json:
        return None
    try:
        data = json.loads(room_state_json)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, list) else None


def batch_room_states(room_state_json: Optional[str]) -> List[Dict[str, Any]]:
    """バッチの ``room_state_json`` を list に復元する。壊れていれば空 list。

    読む側の篩 — 「dict で ``key`` を持つ要素」だけを返す。書き戻しにこの結果を
    使ってはいけない (:func:`raw_room_entries` の docstring)。
    """
    raw = raw_room_entries(room_state_json)
    if raw is None:
        return []
    return [e for e in raw if isinstance(e, dict) and e.get("key")]


def batch_is_room_reseat(room_state_json: Optional[str]) -> bool:
    """このバッチが機構の置き直し (:func:`reseat_current_room`) で作られたものか。

    置き直しのバッチは「提示の最古端」に置くため ``consumed_at`` が id の順序と
    食い違う。id 一本で持つ下ろし境界がこの id を境界に取ると、より新しい
    consumed_at のバッチまで巻き添えで下ろしてしまうので、下ろす機構は候補から
    外す必要がある (2026-09-09 に知覚の合計上限を廃止して以降、境界を進める
    呼び出しは無い — 将来また現れたときのための規則)。編纂の付記では普通に
    引き取られる (材料には載せない — 機構の置き直しは出来事ではないため。
    sai_memory/arasuji/executor.collect_annex_items)。
    """
    for entry in batch_room_states(room_state_json):
        if entry.get("reseated"):
            return True
    return False


def first_room_bundle(
    records: Iterable[Mapping[str, Any]], key: str,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """同部屋の**最初の一致**で束を確定する — 止まり方の規則の一枚。

    ``records`` は新しい順の記帳 (pending の記帳・バッチ内エントリの逆順など、
    並べ方は呼び出し側が揃える)。最初に ``key`` が一致した記録だけで判定を
    確定する: 束が有効ならそれを返し、旧形式 (文字列 snapshot)・不正な束なら
    **束なし** — さらに古い構造化束へは遡らない (2026-09-06 五巡目修正 1。
    旧形式の受け皿は次の入室 push の全文と検知の自己回復 — intent §9)。

    置き直しの材料探し (:func:`_latest_room_bundle`)・その見積もり
    (sea/runtime_context._room_reseat_projection)・差分の土台探し
    (:func:`latest_visible_snapshot`)・置き直しの運搬役判定
    (:func:`reseat_current_room`) がこの一枚を通る。止まり方を二枚書くと
    必ずずれる — 実物は材料なしで置き直しを発火しないのに、見積もりだけが
    旧形式を飛ばして古い束の全文一枚を加算し、境界が必要以上に進んで、まだ
    提示できた履歴を不可逆に下ろす (2026-09-06 六巡目修正)。

    Returns:
        ``(matched, bundle)``。``matched`` が False なら一致なし — 呼び出し側は
        より古い記録源 (次のバッチなど) へ走査を続けてよい。True で ``bundle``
        が None なら「一致したが束なし」— 走査はそこで終わり。
    """
    for record in records:
        if str(record.get("key") or "") != key:
            continue
        snapshot = record.get("snapshot")
        return True, (snapshot if bundle_is_valid(snapshot) else None)
    return False, None


def latest_visible_snapshot(
    conn: sqlite3.Connection, key: str, *,
    in_window: Optional[Callable[[Any], bool]] = None,
) -> Optional[Dict[str, Any]]:
    """いま提示に見えている (or 次の消費で見える) 同部屋エントリの最新の束。

    見つからなければ None = 「土台が無いので全文を積む」。順序は
    「未消費 (pending) → 提示に出るバッチの新しい順」— pending は必ず最後の消費
    より後に積まれたので、あればそれが最新。バッチ側で見るのは**提示に出る**
    ものだけ (未付記かつ知覚の合計上限で下ろした境界より新しい)。

    ``in_window`` は提示窓の篩 (バッチを受けて bool、None = 窓なし = 全部
    見える) — 検知の読み (saiverse_memory/adapter.latest_room_snapshot) が
    Chronicle 無効ペルソナの anchor 由来の窓を渡す。窓の外のバッチはこの提示に
    出ないので、そこの束を「前回」に拾うと、窓の中に残る古い提示との差を
    「変化なし」と誤読して覆い隠す (2026-09-06 八巡目修正 1 — 検知の読みは
    提示と同じ窓を通る)。篩は :func:`reseat_current_room` の運搬役判定と同じ
    一枚 (:func:`sai_memory.perception_buffer.batch_in_window`) を呼び出し側が
    渡す。pending には篩をかけない — 次の消費は提示の末尾 (境界キー = 最新の
    message) に立つので、どの窓からも見える。読み手は検知
    (saiverse_memory/adapter.latest_room_snapshot) だけ — 積む側は描画も土台
    探しもしない (束の記帳のみ、:func:`build_room_state_push`)。消費時描画の
    土台は :func:`_visible_chain_tail` (pending を土台にしない別の読み)。

    直近のエントリが旧形式 (:func:`is_legacy_entry`) なら None — 旧形式は連なり
    に参加しないので、次は全文を積み直す (旧データの読者を書かない)。

    読み取りに失敗したら None (= 全文を積む) に倒す。**pending の読み取りが
    落ちたらバッチ側へ進まない** — pending の方が新しいので、そこを空と見なして
    バッチ側の古い束を土台にすると、実際とは違う土台に対する差分を積むことに
    なる。全文を積み直す冗長は無害だが、間違った土台の差分は復元不能。
    """
    from sai_memory.perception_buffer import list_presented_batches

    try:
        matched, bundle = pending_room_bundle(conn, key)
    except sqlite3.OperationalError:
        LOGGER.warning(
            "[room_state] could not read pending perceptions while looking for "
            "a diff base (key=%s); pushing the full room text instead", key,
            exc_info=True,
        )
        return None
    if matched:
        return bundle

    try:
        batches = list_presented_batches(conn)
    except sqlite3.OperationalError:
        LOGGER.warning(
            "[room_state] could not read the presented batches while looking "
            "for a diff base (key=%s); pushing the full room text instead", key,
            exc_info=True,
        )
        return None
    for batch in reversed(batches):
        if in_window is not None and not in_window(batch):
            continue  # 窓の外 = この提示に出ないバッチ (束は「前回」にならない)
        matched, bundle = first_room_bundle(
            reversed(batch_room_states(batch.room_state_json)), key,
        )
        if matched:
            return bundle
    return None


# ---------------------------------------------------------------------------
# 積む側 (入室・滞在中の変化)
# ---------------------------------------------------------------------------

def build_room_state_push(
    building_id: str,
    bundle: Mapping[str, Any],
    *,
    allow_diff: bool = True,
) -> Dict[str, Any]:
    """積む「部屋の様子」= 束の記帳だけ。描画の判定はしない。

    **描画 (差分か全文かの判定 + 文字列への畳み) は消費の組成の一回だけ**
    (:func:`render_pending_room_states` — 2026-09-06 まはー裁定、intent §11-2
    規則 2)。回収 (§11-2) は途中の pending を必ず捨てるので、積む時に土台を
    pending から選んで描画しても、その差分は構造的に提示へ届き得ない — 実機
    では、提示済みの全文が生きているのに、土台を失った差分の開き直しがもう
    一枚の全文を立てて保証 2 (同じ内容が二枚並ばない) を破った。

    記帳 (``metadata`` の :data:`ROOM_STATE_META_KEY`) は ``key`` / ``snapshot``
    (束) / ``allow_diff`` (Chronicle 無効 = 毎回全文、の旗の凍結) のみ —
    ``is_diff`` / ``base_digest`` は積む段階では確定しない。``content`` には
    劣化時・生の点検用に全文の描画を入れておく (消費の組成はこれを使わず、
    束から組み直す)。メディアも同じ扱いで束の全メディアを添える。

    Returns:
        ``{"content", "media", "metadata"}`` — そのまま
        :func:`sai_memory.perception_buffer.push_perception` に渡せる形。
    """
    state: Dict[str, Any] = {
        "key": room_key(building_id),
        "snapshot": dict(bundle),
        "allow_diff": bool(allow_diff),
    }
    metadata = json.dumps({ROOM_STATE_META_KEY: state}, ensure_ascii=False)
    return {
        "content": render_room_full(bundle),
        "media": bundle_media(bundle) or None,
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# 消費側 (バッチ確定時の記帳)
# ---------------------------------------------------------------------------

def _visible_chain_tail(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """部屋ごとの「提示に出るバッチの同部屋の末尾のエントリ」。

    消費時描画 (:func:`render_pending_room_states`) の土台はこの末尾のエントリ
    でなければならない (次の消費の連なり :func:`chain_is_intact` がこの指紋で
    照合するため)。**末尾が旧形式・不正束でもそのまま返す** — 読み手が
    「末尾が束として読めない = 土台なし = 全文」と判定する
    (:func:`first_room_bundle` と同じ止まり方: 最新の一致で確定し、さらに古い
    構造化束へは遡らない — intent §9)。

    読み取りに失敗したら「土台なし」(空 dict) に倒す — この消費は全文で描画
    されるので、全文が二枚並ぶ冗長は出るが失われるものは無い (積む側の読み
    失敗と同じ向き: 余分な全文は無害、間違った土台の差分は復元不能)。
    """
    from sai_memory.perception_buffer import list_presented_batches

    try:
        batches = list_presented_batches(conn)
    except sqlite3.OperationalError:
        LOGGER.warning(
            "[room_state] could not read the presented batches while looking "
            "for the visible tail; rendering this consumption's room states "
            "as full text", exc_info=True,
        )
        return {}
    tail: Dict[str, Dict[str, Any]] = {}
    for batch in batches:
        for entry in batch_room_states(batch.room_state_json):
            tail[str(entry["key"])] = entry
    return tail


def render_pending_room_states(
    conn: sqlite3.Connection, items: Sequence[Any],
) -> List[Any]:
    """消費の組成の一回だけの描画 — 束から差分か全文かを決めて文字列に畳む。

    **描画はここが唯一の場所** (2026-09-06 まはー裁定 — intent §11-2 規則 2)。
    積む側 (:func:`build_room_state_push`) は束を記帳するだけで、差分か全文かの
    判定・文字列への畳み・メディアの選定は、回収
    (:func:`reclaim_pending_perceptions`) が最後の様子を残した後のこの一枚で
    行う。実 flush (saiverse_memory/adapter.flush_perception_buffer_payload) と
    プレビュー (sea/runtime_context._compose_pending_preview) が同じここを通る。

    土台は常に「提示に見えている同部屋の末尾の束」(:func:`_visible_chain_tail`)
    — pending は土台にならない (回収が途中の pending を必ず捨てるので、pending
    土台の差分は構造的に提示へ届き得ない)。全文になるのは: 末尾なし /
    ``allow_diff`` が False (Chronicle 無効 = 毎回全文) / 末尾が旧形式・不正束
    (連なりの外 — intent §9) / 記帳に ``allow_diff`` が無い旧世代の pending 行
    (積む時に描画していた世代の移行 — 全文に倒す: 余分な全文は無害、間違った
    土台の差分は復元不能)。それ以外は末尾との差分 (:func:`render_room_diff` —
    変わっていなければ一行、は §4 の既存規則)。

    書き換えるのは**この消費でレンダリングに使う写しだけ** — 台帳の行
    (``perception_buffer``) は束の記帳のまま残る。写しの metadata には描画で
    確定した ``is_diff`` / ``base_digest`` (末尾の指紋) を刻み、消費の記帳
    (:func:`collect_batch_room_states`) がバッチへ写す — 以後の連なり・移管・
    回復の読み手が正しい土台を見る。返るのは ``items`` と同じ並び・同じ長さの
    list。
    """
    room_positions = [
        (index, state)
        for index, item in enumerate(items)
        if (state := _parse_item_state(getattr(item, "metadata", None)))
        and state.get("key")
    ]
    if not room_positions:
        return list(items)

    tail_by_key = _visible_chain_tail(conn)
    out = list(items)
    for index, state in room_positions:
        key = str(state["key"])
        if is_legacy_entry(state):
            continue  # 旧形式は描けない (回収が落とす — 万一残っても触らない)
        snapshot = state["snapshot"]
        previous = tail_by_key.get(key)
        base: Optional[Dict[str, Any]] = None
        if state.get("allow_diff") is True and previous is not None:
            prev_snapshot = previous.get("snapshot")
            if bundle_is_valid(prev_snapshot):
                base = prev_snapshot
        composed = dict(state)
        if base is None:
            composed["is_diff"] = False
            composed.pop("base_digest", None)
            content = render_room_full(snapshot)
            media = bundle_media(snapshot)
        else:
            composed["is_diff"] = True
            composed["base_digest"] = snapshot_digest(base)
            diff = render_room_diff(base, snapshot)
            content = diff["content"]
            media = diff["media"]
        tail_by_key[key] = composed  # 万一の複数枚は前の写しに連なる
        # 台帳配送の冪等キー等、room_state 以外の metadata は写しでも保つ。
        try:
            meta = json.loads(getattr(out[index], "metadata", None) or "{}")
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta[ROOM_STATE_META_KEY] = composed
        out[index] = dataclasses.replace(
            out[index],
            content=content,
            media=json.dumps(media, ensure_ascii=False) if media else None,
            metadata=json.dumps(meta, ensure_ascii=False),
        )
    return out


def collect_batch_room_states(
    items: Sequence[Any], rendered_text: str,
) -> Optional[str]:
    """消費する項目から、バッチに記帳する部屋の様子エントリを組む。

    ``items`` は reduce 済みの :class:`~sai_memory.perception_buffer.PerceptionItem`
    (= 実際に ``rendered_text`` に出た項目)。``block`` には確定文面の中の位置を
    後から特定できるよう、その項目の本文をそのまま持たせる — 回復の差し替えは
    この文字列の一致で行う。本文が確定文面に見つからない項目は記帳しない
    (差し替えられないものを記帳すると、回復が黙って空振りする)。
    """
    entries: List[Dict[str, Any]] = []
    for item in items:
        state = _parse_item_state(getattr(item, "metadata", None))
        if not state or not state.get("key"):
            continue
        block = getattr(item, "content", "") or ""
        if not block or block not in rendered_text:
            LOGGER.warning(
                "[room_state] item content not found in the rendered batch text; "
                "skipping the room-state record (key=%s)", state.get("key"),
            )
            continue
        is_diff = bool(state.get("is_diff"))
        entry: Dict[str, Any] = {
            "key": str(state["key"]),
            "is_diff": is_diff,
            "block": block,
            "snapshot": state.get("snapshot"),
        }
        if is_diff and state.get("base_digest"):
            entry["base_digest"] = str(state["base_digest"])
        entries.append(entry)
    if not entries:
        return None
    return json.dumps(entries, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 土台の回復 (付記・境界前進と同一トランザクション / 提示時の開き直し)
# ---------------------------------------------------------------------------

def _reopen_lost_bases(batches: Sequence[Any]) -> Dict[int, tuple]:
    """土台を失った差分を全文へ開き直した結果をバッチ id ごとに返す。

    **純関数** — DB を読み書きせず、``batches`` の並びだけから答えが決まる。
    同じ並びを二度渡せば必ず同じ結果になる (提示時の開き直しがこの決定論に
    寄りかかっている)。

    走査は部屋ごとの連なり: 古い順に辿り、差分エントリの土台
    (:func:`chain_is_intact`) が直前に見えていなければ、その位置の文面を
    束から導出した全文へ差し替え、束のメディアを添える。旧形式のエントリは
    連なりの外 (検査もしないし、次のエントリの土台にもしない)。

    返るのは**変わったバッチだけ**の
    ``{batch.id: (rendered_text, entries, extra_media)}``。``entries`` は
    差し替え済みの記帳の**生の並び** (:func:`raw_room_entries` — 読む側の篩を
    通していない)、``extra_media`` は開き直しで戻すメディア (束由来、path
    重複なし)。生で返すのは、書き戻す側 (:func:`restore_room_state_bases`) が
    篩の落とした未知の要素を消さずに済むようにするため — 差し替えはエントリの
    dict をその場で書き換えるので、生の並びの中の同じ dict がそのまま更新される。
    """
    previous_by_key: Dict[str, Dict[str, Any]] = {}
    out: Dict[int, tuple] = {}
    for batch in batches:
        raw = raw_room_entries(batch.room_state_json)
        entries = [
            e for e in (raw or []) if isinstance(e, dict) and e.get("key")
        ]
        if not entries:
            continue
        rendered = batch.rendered_text or ""
        extra_media: List[Dict[str, Any]] = []
        changed = False
        for entry in entries:
            key = str(entry["key"])
            if is_legacy_entry(entry):
                continue  # 旧形式は連なりの外 (previous_by_key も更新しない)
            previous = previous_by_key.get(key)
            # 差し替えても snapshot は変わらないので、次のエントリの土台判定は
            # この entry (同じ dict) をそのまま指してよい。
            previous_by_key[key] = entry
            if not entry.get("is_diff"):
                continue  # 全文はそれ自体が土台 = 連なりはここから始め直す
            if chain_is_intact(entry, previous):
                continue
            block = entry.get("block") or ""
            snapshot = entry.get("snapshot")
            if not block or not bundle_is_valid(snapshot) or block not in rendered:
                LOGGER.warning(
                    "[room_state] cannot reopen the full room text in "
                    "batch %s (key=%s): the recorded block is not in the "
                    "rendered text", batch.id, key,
                )
                continue
            full_text = render_room_full(snapshot)
            rendered = rendered.replace(block, full_text, 1)
            entry["block"] = full_text
            entry["is_diff"] = False
            entry["transferred"] = True
            extra_media.extend(bundle_media(snapshot))
            changed = True
        if changed:
            out[int(batch.id)] = (rendered, raw, extra_media)
    return out


def reopen_lost_bases(batches: Sequence[Any]) -> Dict[int, Tuple[str, List[Dict[str, Any]]]]:
    """この並びを**そのまま提示する**ときの、開き直し後の文面とメディア (変わった分だけ)。

    台帳も確定文面も書き換えない — 返るのは
    ``{batch.id: (rendered_text, extra_media)}`` で、呼び出し側
    (:func:`sea.runtime_context.list_presented_perception_blocks`) がブロックを
    組むときに文面を差し替え、メディアをブロックの metadata に足す。

    要るのは、**可視性が DB に書けない形で狭まる**ときのため: Chronicle 無効の
    ペルソナは提示窓 (anchor) より古いバッチを付記なしで忘れるので、台帳側の
    回復 (:func:`restore_room_state_bases`) は「土台はまだ見えている」と読んで
    走らない。同じ理由で、**下ろし境界を進めずに測るだけの呼び出し** も、
    進めた**つもり**の並びをここへ通して勘定を実送信と一致させる。

    Chronicle 有効で境界の前進も済んだ並びは台帳の
    :func:`sai_memory.perception_buffer.list_presented_batches` と一致するので、
    ここは空 dict を返す (台帳側の回復が既に不変条件を保っている)。
    """
    return {
        batch_id: (rendered, extra_media)
        for batch_id, (rendered, _entries, extra_media)
        in _reopen_lost_bases(batches).items()
    }


def _merge_media_json(
    existing_json: Optional[str], extra: Sequence[Mapping[str, Any]],
) -> Optional[str]:
    """バッチの media (JSON) に開き直しのメディアを合流させる (path 重複なし)。"""
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    if existing_json:
        try:
            data = json.loads(existing_json)
        except (TypeError, ValueError):
            data = []
        if isinstance(data, list):
            for m in data:
                if isinstance(m, dict):
                    path = m.get("path")
                    if path:
                        seen.add(path)
                    merged.append(m)
    for m in extra:
        path = m.get("path")
        if path and path in seen:
            continue
        if path:
            seen.add(path)
        merged.append(dict(m))
    if not merged:
        return None
    return json.dumps(merged, ensure_ascii=False)


def restore_room_state_bases(conn: sqlite3.Connection) -> int:
    """不変条件「どの差分も自分の土台が直前に見えている」を回復する。**commit しない**。

    提示に出るバッチを古い順に走査し、部屋ごとに連なりを辿る。差分エントリの
    土台 (:func:`chain_is_intact`) が直前に見えていなければ、その位置の文面を
    束から導出した全文へ差し替え、**束のメディアをバッチの media にも合流させる**
    (全文が絵を名乗るのに実体が無い状態を作らない — intent §5 の復元)。

    呼ぶのは**可視性が変わる二つの書き込み点**だけで、単独では走らせない
    (提示の書き換え時点を増やさないため):

    - :func:`sai_memory.perception_buffer.mark_batches_annexed` — 付記が 1 行
      でも立った tx の中 (編纂で全文バッチが下りる瞬間)。
    - :func:`sai_memory.perception_buffer.advance_presentation_cutoff` — 知覚の
      合計上限で古い側をまとめて下ろした tx の中。

    **読み取りに失敗したら例外をそのまま送出する** (「回復対象なし」に化かさない)。
    ここを 0 件で返すと、呼び出し側は回復が済んだ場合と区別できないまま付記や
    境界の前進を commit してしまい、「土台の全文バッチだけが提示から下り、
    残った差分が宙に浮く」壊れ方が確定する。呼び出し側は例外を受けたら tx ごと
    rollback して、付記も境界前進も見送る (次の機会に全体をやり直す)。

    記帳の書き戻しは**読んだ生の並びの上**で行う (:func:`raw_room_entries`) —
    読む側の篩 (:func:`batch_room_states`) の結果を書き戻すと、篩が落とした未知の
    要素が黙って消える。もう一つの書き戻し
    (:func:`sai_memory.presented_reduction.mark_presentation_reductions`) と同じ
    規則 (ローカルレビュー指摘 2026-09-10)。

    Returns:
        文面を差し替えたバッチの件数。
    """
    from sai_memory.perception_buffer import list_presented_batches

    batches = list_presented_batches(conn)
    media_by_id = {int(b.id): b.media for b in batches}
    repaired = _reopen_lost_bases(batches)
    for batch_id, (rendered, raw_entries, extra_media) in repaired.items():
        conn.execute(
            "UPDATE perception_batches SET rendered_text = ?, "
            "room_state_json = ?, media = ? WHERE id = ?",
            (
                rendered,
                json.dumps(raw_entries, ensure_ascii=False),
                _merge_media_json(media_by_id.get(batch_id), extra_media),
                batch_id,
            ),
        )
        LOGGER.info(
            "[room_state] reopened a room diff to its full text in batch %s "
            "(its base is no longer presented right before it)", batch_id,
        )
    return len(repaired)


# ---------------------------------------------------------------------------
# 最後の運搬役が下りる回の置き直し (intent §6-4) と自己回復の器
# ---------------------------------------------------------------------------

def find_current_room_key(conn: sqlite3.Connection) -> Optional[str]:
    """台帳が知る「今いる部屋」のキー (最新の surroundings 記録から)。

    入室のたびに部屋の様子が積まれるので、最新の記録のキーが現在地に一致する
    (積み損ねた回だけずれうる — その回は次の入室・検知が上書きする)。pending
    (未消費) が最新、無ければバッチを id の新しい順 (= 記録順) に辿る。

    **読み取りに失敗したら例外をそのまま送出する** (「部屋の記録なし」の None
    に化かさない — 2026-09-06 四巡目修正 1)。None を返すと、置き直し
    (:func:`reseat_current_room`) が「対象なし」と読み、hook は付記・境界前進を
    そのまま commit してしまう — 読み取りが失敗しただけなのに、最後の運搬役が
    置き直しなしで下りる。呼び出し側の受けは hook が tx ごと rollback
    (perception_buffer の両 hook)、下ろし計画は組成ごと fail-open
    (sea/runtime_context の外側の except)。
    """
    rows = conn.execute(
        "SELECT metadata FROM perception_buffer "
        "WHERE consumed_at IS NULL AND kind = ? "
        "ORDER BY created_at DESC, id DESC",
        (ROOM_STATE_KIND,),
    ).fetchall()
    for (metadata,) in rows:
        state = _parse_item_state(metadata)
        if state and state.get("key"):
            return str(state["key"])
    cursor = conn.execute(
        "SELECT room_state_json FROM perception_batches "
        "WHERE room_state_json IS NOT NULL ORDER BY id DESC"
    )
    for (room_state_json,) in cursor:
        entries = batch_room_states(room_state_json)
        for entry in reversed(entries):
            if entry.get("key"):
                return str(entry["key"])
    return None


def pending_room_bundle(
    conn: sqlite3.Connection, key: str,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """pending (未消費) の同部屋の**最初の一致** (:func:`first_room_bundle` の一枚)。

    材料探し (:func:`_latest_room_bundle`) の pending 段と、その見積もり
    (sea/runtime_context._room_reseat_projection) が同じこの読みを通る。実物の
    材料探しは pending を先に見て、最初の一致が旧形式の遺物なら材料なし =
    fresh_bundle の無い hook 経路の置き直しは発火しない — pending を見ない
    見積もりが提示列の有効束でコストを加算すると、起きない置き直しのぶん
    境界が必要以上に進み、まだ提示できた履歴を不可逆に下ろす (2026-09-06
    Codex 指摘。バッチ記録の遺物は六巡目で揃えたが、pending の遺物が漏れて
    いた)。

    **読み取りに失敗したら例外をそのまま送出する** —
    :func:`find_current_room_key` と同じ契約 (2026-09-06 四巡目修正 1)。

    Returns:
        :func:`first_room_bundle` と同じ ``(matched, bundle)``。
    """
    rows = conn.execute(
        "SELECT metadata FROM perception_buffer "
        "WHERE consumed_at IS NULL AND kind = ? "
        "ORDER BY created_at DESC, id DESC",
        (ROOM_STATE_KIND,),
    ).fetchall()
    return first_room_bundle(
        (state for (metadata,) in rows
         if (state := _parse_item_state(metadata)) is not None),
        key,
    )


def _latest_room_bundle(
    conn: sqlite3.Connection, key: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
    """台帳が持つこの部屋の最新の束と、その記録の時刻 (提示可否を問わない)。

    置き直し (:func:`reseat_current_room`) の材料。最後の運搬役が付記や境界
    前進で下りる瞬間には「提示に見えている束」はもう無いので、下りたバッチも
    含めて最新を探す。pending が最新 (時刻は None = 客観時間ではまだ提示位置を
    持たない)。

    **同部屋の走査で最初に見つかる記録が旧形式 (文字列 snapshot) なら、その
    時点で材料なし (None)** — さらに古い構造化束へは遡らない (2026-09-06 五巡目
    修正 1)。旧形式は連なりの外 (:func:`is_legacy_entry`) で、次の入室 push が
    全文を積み直す契約 (intent §9) なのに、ここが旧形式を飛ばして古い束を
    返すと、fresh_bundle の無い hook 経路 (付記・境界前進の置き直し) がその
    古い部屋の様子を「今の部屋」として最古端に立てる。置き直しが立たなくても、
    旧形式の次の入室 push (全文) と検知の自己回復 (§6-2) が受け皿になる。
    この止まり方は :func:`first_room_bundle` の一枚 — 下ろし計画の見積もり
    (sea/runtime_context._room_reseat_projection) も同じ関数で判定する
    (2026-09-06 六巡目修正)。

    **読み取りに失敗したら例外をそのまま送出する** (「材料なし」の None に
    化かさない — :func:`find_current_room_key` と同じ契約、2026-09-06 四巡目
    修正 1)。材料なしを装うと、hook は置き直しなしで付記・境界前進を commit
    してしまう。
    """
    matched, bundle = pending_room_bundle(conn, key)
    if matched:
        # bundle が None なら「最新が旧形式・不正束 — 古い束へは遡らない (§9)」。
        return bundle, None
    cursor = conn.execute(
        "SELECT room_state_json, consumed_at FROM perception_batches "
        "WHERE room_state_json IS NOT NULL ORDER BY id DESC"
    )
    for room_state_json, consumed_at in cursor:
        matched, bundle = first_room_bundle(
            reversed(batch_room_states(room_state_json)), key,
        )
        if matched:
            if bundle is None:
                return None, None  # 最新が旧形式・不正束 — 古い束へは遡らない (§9)
            return bundle, int(consumed_at)
    return None, None


def pending_has_room(conn: sqlite3.Connection, key: str) -> bool:
    """pending (未消費) にこの部屋のエントリが居るか — 置き直しの発火の門。

    True なら次の消費が部屋を運ぶので、置き直しは発火しない。数えるのは
    **回収を生き残る有効なエントリだけ** — 旧形式の遺物 (:func:`is_legacy_entry`)
    は直後の消費の回収 (:func:`reclaim_pending_perceptions` — intent §11-2) が
    落とすので、「次の消費が運ぶ」の前提が成立しない (遺物をキー一致だけで
    数えると、移行したペルソナの最初の Pulse が部屋なしで送信される —
    2026-09-06 実機)。

    **読み取りに失敗したら例外をそのまま送出する** (「pending なし」の False に
    化かさない — 2026-09-06 四巡目修正 1)。False を装うと、失敗しただけの回に
    不要な置き直しが積まれ、下ろし計画 (sea/runtime_context) は起きない
    置き直しのコストで境界を必要以上に進める。
    """
    rows = conn.execute(
        "SELECT metadata FROM perception_buffer "
        "WHERE consumed_at IS NULL AND kind = ?",
        (ROOM_STATE_KIND,),
    ).fetchall()
    for (metadata,) in rows:
        state = _parse_item_state(metadata)
        if state and state.get("key") == key and not is_legacy_entry(state):
            return True
    return False


def _front_of_messages(conn: sqlite3.Connection) -> int:
    """提示のどの生ログよりも古い時刻 (置き直しを最古端に置くための既定値)。"""
    try:
        row = conn.execute("SELECT MIN(created_at) FROM messages").fetchone()
        if row is not None and row[0] is not None:
            return int(row[0]) - 1
    except sqlite3.OperationalError:
        # 位置決めだけの読み (発火判定・材料には関与しない)。残る提示が無い回
        # にしか使われず、その回はどの時刻でも最古端に立つ — 安全側に倒してよい。
        pass
    return int(time.time())


def reseat_current_room(
    conn: sqlite3.Connection, *, fresh_bundle: Optional[Mapping[str, Any]] = None,
    dry_run: bool = False, assume_cutoff: Optional[int] = None,
    in_window: Optional[Callable[[Any], bool]] = None,
) -> Union[int, Tuple[str, List[Dict[str, Any]], int], None]:
    """今いる部屋の全文を提示の最古端へ置き直す (運搬役が居なければ)。**commit しない**。

    intent §6 の「機構の置き直し」の器。呼び出しは三つ:

    1. **最後の運搬役が下りる回** (§6-4) —
       :func:`~sai_memory.perception_buffer.mark_batches_annexed` /
       :func:`~sai_memory.perception_buffer.advance_presentation_cutoff` が
       付記・境界前進と同一トランザクションで呼ぶ (``fresh_bundle`` なし =
       台帳の最新の束を使う)。畳みでその位置から下のキャッシュはどうせ割れる
       ので、先頭に置くコストは無い。
    2. **滞在中の自己回復 / ブートストラップ** (§6-2 / §6-3) — 検知の瞬間
       (sea/head_pipeline/integration.py) が「部屋の様子が提示に見えない」と
       判定した回に、今の世界を読んだ束 (``fresh_bundle``) で呼ぶ。

    **置き場所の原則 (2026-09-06 まはー裁定)**: 体験として新しく見た回 (入室) は
    末尾 = 出来事。機構の都合で置き直すこの全文は先頭 = 背景 — ずっと背景として
    そこにある部屋の描写の位置 (提示の最古端) に置く。実装上は ``consumed_at``
    を「残る提示のどれよりも古い時刻」にして順序で最古端に立たせる (台帳の行の
    書き換えではなく、新しいバッチ行の追加)。

    発火しない条件 (運搬役が居る回):

    - pending にこの部屋のエントリがある (次の消費が運ぶ — 描画は消費時
      :func:`render_pending_room_states` が行い、土台が無ければ全文になる)。
    - 提示に出るバッチのうち **``in_window`` の篩を通るもの** で、この部屋の
      **最新**の一致が valid な束を持つ (:func:`first_room_bundle` の一枚 —
      最新の一致が旧形式・不正束なら運搬役なし。2026-09-06 七巡目修正 2)。

    ``in_window`` は提示窓の篩 (バッチを受けて bool、None = 窓なし = 全部
    見える)。Chronicle 無効ペルソナの窓 (anchor) より古いバッチは付記なしで
    提示から下りる — そこに居る運搬役を「生きている」と数えると、窓絞りの
    自己回復 (この関数の主目的の一つ、intent §6-2) がまさにその形で空振り
    する。判定の規則は提示の組成と同じ一枚
    (:func:`sai_memory.perception_buffer.batch_in_window`) を呼び出し側が
    渡す — 検知の自己回復 (saiverse_memory/adapter.reseat_room_state) は
    実行 model の anchor から、測るだけの下見 (sea/runtime_context) は組成と
    同じ窓の述語から、境界前進の hook
    (:func:`~sai_memory.perception_buffer.advance_presentation_cutoff`) は
    呼び出し元の組成の篩を同名引数で中継する (Chronicle 無効の組成も境界を
    進めるため — 2026-09-06 九巡目修正 1)。付記の hook
    (:func:`~sai_memory.perception_buffer.mark_batches_annexed` — 編纂 =
    Chronicle 有効のみの経路で、窓の概念がない) だけは渡さない。置き場所の
    決定 (最古端) は篩を通さず提示の全バッチで行う — 置き直しはどの窓から
    見ても最古端に立つべきもので、境界キーは最新の message なので窓の内側に
    入る。

    **下見モード** (``dry_run=True``): INSERT せず、実際に積むはずの内容
    ``(rendered_text, media, consumed_at)`` を返す (発火しない回は None)。
    発火条件・材料の選定・位置決めは実 INSERT と同じこの一本を通る — 判定
    ロジックの二枚目を作らないための口。``assume_cutoff`` は「下ろし境界が
    この id まで進んだと仮定する」入力で、実物は境界を書いた**後**の提示可視性
    (運搬役が残っているか) で判定するので、下見も進めたつもりの世界で判定する
    必要がある。None なら DB の実境界を読む (実 INSERT の経路は挙動不変)。

    この下見の利用者だった「測るだけの提示組成」は 2026-09-09 に消えた (知覚の
    合計上限の廃止で、組成が境界を進めなくなったため)。口は残してある — 境界を
    進める機構が将来また現れたら、その勘定は同じこの一本を通す。

    **読み取り失敗の契約 (2026-09-06 四巡目修正 1)**: 発火判定・材料の読み
    (:func:`find_current_room_key` / :func:`pending_has_room` /
    :func:`_latest_room_bundle` / 提示バッチの走査)、および INSERT 直前の
    境界キーの読み
    (:func:`~sai_memory.perception_buffer.latest_message_boundary` の
    ``strict=True`` — 五巡目修正 3: キーなしの置き直しは窓の外に立って重複を
    生む) は失敗を例外で伝える — None (「置き直し不要・材料なし」) に
    化かさない。hook (付記・境界前進) は例外を受けたら tx ごと rollback して
    畳みを見送り、自己回復 (adapter) はその回を見送って次の検知でやり直す。

    Returns: 置き直したバッチの id (下見モードは積むはずの内容のタプル)。
    置き直し不要・材料なしは None。
    """
    from sai_memory.perception_buffer import (
        insert_presentation_batch,
        latest_message_boundary,
        list_presented_batches,
    )

    if fresh_bundle is not None and bundle_is_valid(fresh_bundle):
        key = room_key(str(fresh_bundle.get("building_id") or "")) \
            if fresh_bundle.get("building_id") else None
    else:
        fresh_bundle = None
        key = find_current_room_key(conn)
    if not key:
        return None
    if pending_has_room(conn, key):
        return None
    presented = list_presented_batches(conn, cutoff=assume_cutoff)
    # 運搬役の存在も「同部屋の**最新**の一致」で判定する (first_room_bundle の
    # 一枚 — 材料探し・土台探し・見積もりと同じ止まり方)。古い順の走査で
    # valid を一つでも見つけたら止める形だと、「最新の同部屋記録が旧形式・
    # 不正束、より古い valid 記録が提示に残っている」並びで古い記録が運搬役
    # 扱いになり、検知 (latest_room_snapshot = None) が呼んだ fresh_bundle
    # つきの自己回復をこの門だけが覆す (2026-09-06 七巡目修正 2)。窓の篩
    # (in_window) は従来どおり通す — 窓の外のバッチはこの提示に出ない。
    _matched, carrier = first_room_bundle(
        (entry
         for batch in reversed(presented)
         if in_window is None or in_window(batch)
         for entry in reversed(batch_room_states(batch.room_state_json))),
        key,
    )
    if carrier is not None:
        return None  # 運搬役が生きている (同部屋の最新の一致が valid な束)

    if fresh_bundle is not None:
        bundle: Optional[Dict[str, Any]] = dict(fresh_bundle)
        _ledger_bundle, source_time = _latest_room_bundle(conn, key)
    else:
        bundle, source_time = _latest_room_bundle(conn, key)
    if bundle is None:
        return None

    # 最古端 = 残る提示のどのバッチよりも古い時刻。台帳にこの部屋の記録の時刻が
    # あればそれ以前 (歴史上の位置)、無ければ生ログの最古より前。
    candidates: List[int] = []
    if presented:
        candidates.append(min(b.consumed_at for b in presented) - 1)
    if source_time is not None:
        candidates.append(int(source_time))
    consumed_at = min(candidates) if candidates else _front_of_messages(conn)

    full_text = render_room_full(bundle)
    media = bundle_media(bundle)
    if dry_run:
        # 下見 — 積むはずの内容だけ返して何も書かない。発火判定 (上) と材料・
        # 位置決め (ここまで) は実 INSERT と完全に同じ道を通ってきた。
        return (full_text, media, consumed_at)
    entry = {
        "key": key,
        "is_diff": False,
        "block": full_text,
        "snapshot": bundle,
        "reseated": True,
    }
    # 境界キーは置き直しの可視性の生命線 — consumed_at は意図的に最古なので、
    # キーなしで積むと窓判定 (batch_in_window) の epoch フォールバックで窓の
    # 外に立ち、次の検知が「運搬役が見えない」と判定してまた置き直す (単発の
    # 読み取り失敗が全文バッチの重複を生む)。読みの失敗は strict で例外の
    # まま伝え、hook は tx ごと rollback、自己回復 (adapter) は見送って次の
    # 検知でやり直す — 四巡目修正 1 の読み取り失敗の契約と同じ向き
    # (2026-09-06 五巡目修正 3)。
    boundary_created_at, boundary_rowid = latest_message_boundary(
        conn, strict=True,
    )
    batch_id = insert_presentation_batch(
        conn,
        consumed_at=consumed_at,
        rendered_text=full_text,
        media=media or None,
        room_state_json=json.dumps([entry], ensure_ascii=False),
        boundary_created_at=boundary_created_at,
        boundary_rowid=boundary_rowid,
    )
    LOGGER.info(
        "[room_state] reseated the current room (%s) at the oldest end of the "
        "presentation as batch %s (no surviving carrier)", key, batch_id,
    )
    return batch_id
