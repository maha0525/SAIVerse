"""フォルダ名や URL を壊す文字を ID に含む部屋を、起動時に付け替える。

設計: docs/issues/building_id_contains_path_separator.md

v0.3.0 より前は部屋 ID に使える文字の制限が無く、名前に「/」を含む部屋の ID にも
「/」が残った (表示名「2/28」→ ``2/28_city_a``)。部屋 ID はフォルダ名・URL・
リンクの一部としてそのまま使われるので、「/」は会話ファイルを 2 段のフォルダの
奥に置き、過去ログの取り込みを毎起動空振りさせていた。

ここでは、この City の部屋のうち、次の文字をそれぞれ ``_`` に置き換えると ID が
変わるものを、置き換えた新しい ID へ付け替える (まはーの判断 2 (b)):
フォルダ名に使えない文字 (``manager/ids.py`` の ``_UNSAFE_PATH_CHARS``:
``/ \\ : * ? " < > |``)、URL の区切りになる ``#`` ``%``、制御文字、末尾に並ぶ
ドットと空白。日本語と全角文字は変えない。**表示名 (BUILDINGNAME) は変えない。**
部屋を指す DB の欄、JSON でまとめて保存している欄 (値の完全一致だけ)、ペルソナの
記憶のファイルの中の機械が読む印、会話ファイルのフォルダを一緒に付け替える。

**変えないもの**: 古いファイルでの元のメッセージ ID のうち既に値があるもの
(``building_messages.legacy_message_id`` — 起動時の確認処理がこの値で古いファイルと
突き合わせる。空の行にだけ、書き換える前のメッセージ ID を写す)、ペルソナに送る
本文の元 (``session_head_snapshot.SECTIONS_JSON`` — 書き換えるとプロンプト
キャッシュが割れる)、ペルソナの記憶のファイルの文面と添付 (まはーの判断 1 (b) で
書き換えるのは印だけ)、ユーザーが書いた文章の中の ID。

順番と、途中で止まったときの再開:

1. 付け替えの記録 ``cities/<city>/building_id_renames.json`` に「予定」を書く
2. DB を一つのトランザクションで書き換えて commit する
3. ペルソナの記憶のファイルの印を、ペルソナごとに複製を取ってから書き換える
4. フォルダを移し、空になった途中のフォルダを消す
5. 記録を「完了」にする (記録は消さない)

次の起動では「予定」のまま残った要素を DB の姿で振り分ける — DB に旧 ID が残って
いれば 2 から、旧 ID が無く新 ID があれば 3 から。3 は完全一致の置き換えなので、
途中まで済んだペルソナにもう一度行っても結果は変わらない。4 のフォルダの移動は、
移し終わったものが古い場所に無いので飛ばされる。3 で失敗したペルソナがいた回は
記録を「完了」にせず、次の起動でもう一度 3 から行う。

**起動は止めない。** 見送り (多重起動・バックアップの失敗・安全な ID にできない) と
失敗は、startup_alerts と同じ形の警告として戻り値で返す。付け替えが成功しただけの
ときは警告を出さない (ログだけ)。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from sqlalchemy import text

# _UNSAFE_PATH_CHARS は作成の口の検査 (is_safe_path_component) と同じ一覧。
# 付け替えで置き換える文字をそこから取り、「フォルダ名として使えない」の基準を二つにしない。
from manager.ids import _UNSAFE_PATH_CHARS, is_safe_path_component
from sai_memory.room_state import ROOM_STATE_META_KEY, bundle_is_valid, snapshot_digest

LOGGER = logging.getLogger(__name__)

#: 付け替えの記録ファイルの名前 (``cities/<city>/`` の直下に置く)
RENAMES_FILENAME = "building_id_renames.json"
RECORD_FORMAT_VERSION = 1
STATUS_PLANNED = "planned"
STATUS_DONE = "done"
#: 記録の「予定」に対応する部屋が DB に無かった (付け替える前に部屋が削除された等)
NOTE_ROOM_NOT_FOUND = "room_not_found"

#: Discord 連携の「チャンネルと部屋の対応表」を持つ環境変数 (discord_gateway/mapping.py)
DISCORD_CHANNEL_MAP_ENV = "SAIVERSE_GATEWAY_CHANNEL_MAP"

#: URL の区切りになる文字。フォルダ名には使えるが、URL・リンクの途中で切れる。
_URL_BREAKING_CHARS = frozenset("#%")
#: 付け替えで ``_`` に置き換える文字 (制御文字と末尾のドット・空白は別に扱う)
_REPLACED_CHARS = _UNSAFE_PATH_CHARS | _URL_BREAKING_CHARS
#: Windows のフォルダ名に使えない文字のうち、区切り記号でないもの
_WINDOWS_INVALID_NAME_CHARS = _UNSAFE_PATH_CHARS - {"/", "\\"}
#: 末尾にあると Windows がフォルダ名から黙って落とす文字
_TRAILING_DROPPED_BY_WINDOWS = frozenset(". ")
_MAX_SUFFIX = 10_000
_LOG_PREFIX = "[building-id-repair]"

#: (列, 比較, 値)。「列 比較 値」を満たす行だけを対象にする。値がタプルなら
#: ``IN`` / ``NOT IN`` の一覧として扱う。
_RowFilter = Optional[Tuple[str, str, Union[str, Tuple[str, ...]]]]

#: 部屋の様子の記録の鍵の書き出し (sai_memory/room_state.room_key と同じ形)
_ROOM_KEY_PREFIX = "building:"

#: 部屋を指す欄 (値そのものが部屋 ID)。新しい ID の空き判定にも使う。
DIRECT_REFERENCE_COLUMNS: Tuple[Tuple[str, str, _RowFilter], ...] = (
    ("user", "CURRENT_BUILDINGID", None),
    ("ai", "PRIVATE_ROOM_ID", None),
    ("region", "ENTRANCE_BUILDING_ID", None),
    ("item_location", "OWNER_ID", ("OWNER_KIND", "=", "building")),
    ("fixture", "BUILDING_ID", None),
    ("realtime_spell_binding", "OWNER_ID", ("OWNER_KIND", "=", "building")),
    ("building_messages", "building_id", None),
    ("persona_pulse_cursor", "BUILDING_ID", None),
    ("persona_building_state", "BUILDING_ID", None),
    ("building_occupancy_log", "BUILDINGID", None),
    ("episodes", "BUILDING_ID", None),
    ("llm_usage_log", "BUILDING_ID", None),
    # 部屋専用 Playbook の持ち主。sea/runtime.py と list_available_playbooks が
    # この値の完全一致で「この部屋の Playbook」を選ぶので、残すと部屋から消える。
    ("playbooks", "building_id", None),
    # 読む処理は無いが、古い ID を残さない。
    ("building_tool_link", "BUILDINGID", None),
)

#: 実行台帳の「終わった」状態 (saiverse/execution_ledger.py の STATUS_COMPLETED /
#: STATUS_FAILED)。failed の行は再試行されるとき別の行として作り直される
#: (claim_execution) ので、その入力が読み直されることは無い。
_LEDGER_FINISHED_STATUSES = ("completed", "failed")

#: JSON でまとめて保存している欄。値の完全一致だけを置き換える
#: (:func:`replace_exact_strings_nested` — 文字列の中に JSON を入れ子で持つ欄にも届く)。
JSON_COLUMNS: Tuple[Tuple[str, str, _RowFilter], ...] = (
    ("persona_day_plan", "slots_json", None),
    ("persona_timetable_template", "SLOTS_JSON", None),
    ("phenomenon_rule", "CONDITION_JSON", None),
    ("persona_schedule", "PLAYBOOK_PARAMS", None),
    # 未配達の作業だけ。配達済みの行は実行時点で凍結された記録なので触らない。
    # 知覚の配達 (perception.push) は metadata を JSON の文字列で持つので、
    # 入れ子の中の部屋の様子の鍵まで置き換える。
    ("execution_outbox", "PAYLOAD_JSON", ("STATUS", "<>", "delivered")),
    # まだ終わっていない実行の入力だけ (prepared / running / applied / unknown)。
    ("execution_ledger", "PAYLOAD_JSON", ("STATUS", "NOT IN", _LEDGER_FINISHED_STATUSES)),
    # 前回ペルソナに知らせた状態。値は「Section 名 → その Section の snapshot の
    # JSON 文字列」なので、入れ子の中の部屋 ID を置き換える。付け替えないと、
    # 付け替えた部屋にいるペルソナに起きていない移動の通知が作られる
    # (sea/head_pipeline/sections/building.py の diff_to_notifications)。
    # 同じ行の SECTIONS_JSON (ペルソナに送る本文の元) には触らない。
    ("session_head_snapshot", "LAST_NOTIFIED_JSON", None),
    ("persona_building_state", "BASELINE_JSON", None),
    ("persona_building_state", "LAST_NOTIFIED_JSON", None),
    ("persona_event_log", "PAYLOAD", None),
    ("region", "STATE_JSON", None),
    ("region", "CONFIG_JSON", None),
    ("addon_config", "params_json", None),
    ("addon_persona_config", "params_json", None),
    # 移動の記録 (移動元・移動先の部屋 ID)
    ("building_messages", "event_data", None),
)

#: 付け替えの後の数え上げで「文字列の列」とみなす宣言型
_STRING_TYPE_MARKERS = ("CHAR", "TEXT", "CLOB", "STRING")


# ---------------------------------------------------------------------------
# 小さな部品
# ---------------------------------------------------------------------------

def repaired_building_id(building_id: str) -> str:
    """フォルダ名や URL を壊す文字を、それぞれ ``_`` に置き換えた ID (付け替え先の候補の元)。

    置き換えるのは ``manager/ids.py`` の ``_UNSAFE_PATH_CHARS`` (``/ \\ : * ? " < > |``)、
    ``#`` ``%``、制御文字 (コード 32 未満)、末尾に並ぶドットと空白 (Windows が
    フォルダ名から黙って落とすため、``alice.`` と ``alice`` が同じ場所を指す)。
    それ以外 (日本語・全角の「？」「：」など) は変えない。
    """
    chars = [
        "_" if ch in _REPLACED_CHARS or ord(ch) < 32 else ch
        for ch in building_id
    ]
    index = len(chars)
    while index > 0 and chars[index - 1] in _TRAILING_DROPPED_BY_WINDOWS:
        chars[index - 1] = "_"
        index -= 1
    return "".join(chars)


def needs_building_id_repair(building_id: str) -> bool:
    """付け替える部屋か (置き換えで ID が変わるか)。"""
    return repaired_building_id(building_id) != building_id


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        # 確かめられない名前は「使われている」側に倒す
        return True


def choose_new_building_id(
    old_id: str,
    *,
    taken: Set[str],
    folder_roots: Sequence[Path],
    preferred: Optional[str] = None,
    preferred_folder_may_exist: bool = False,
) -> Optional[str]:
    """付け替え先の部屋 ID を決める。

    :func:`repaired_building_id` の結果を元に、次のどれかに当てはまる間は末尾に
    ``_2``、``_3`` … を足す:

    - ``taken`` (**小文字にした**使用済みの ID) に含まれる。Windows のファイル
      システムは大文字小文字を区別しないので、比べるのは小文字どうし。
    - 付け替え先のフォルダが、``folder_roots`` のどれかの下に既にある。

    ``preferred`` は前に決めて記録に書いた ID。空いていればそれを使う (記録と違う
    ID を黙って選び直さない)。``preferred_folder_may_exist`` は「以前この部屋を付け
    替えてフォルダを移した後に、DB だけが控えから戻された」場合 — そのフォルダは
    自分が移したものなので、あっても空きとみなす。

    戻り値がフォルダ名として安全でない ID のことがある (置き換えても Windows の
    予約名 ``CON`` などに当たる)。安全かどうかの判定は呼び出し側が持つ。None は
    候補を使い切ったとき。
    """
    if (
        preferred is not None
        and preferred.lower() not in taken
        and is_safe_path_component(preferred)
        and (
            preferred_folder_may_exist
            or not any(_path_exists(root / preferred) for root in folder_roots)
        )
    ):
        return preferred

    base = repaired_building_id(old_id)
    for n in range(1, _MAX_SUFFIX + 1):
        candidate = base if n == 1 else f"{base}_{n}"
        if candidate.lower() in taken:
            continue
        if not is_safe_path_component(candidate):
            return candidate
        if any(_path_exists(root / candidate) for root in folder_roots):
            continue
        return candidate
    return None


def _on_windows() -> bool:
    return os.name == "nt"


def legacy_folder_parts(
    building_id: str, *, windows: Optional[bool] = None,
) -> Optional[List[str]]:
    """旧 ID をそのままパスに繋いだとき、実際のフォルダが何段の何という名前になるか。

    v0.2 は ``buildings/<部屋ID>`` を素の結合で作ったので、この OS の区切り記号は段の
    区切りになった (Windows では ``/`` と ``\\`` の両方、それ以外では ``/`` だけ)。
    ``windows`` は判定する OS (None ならこの OS)。

    空の段 (``a//b``・末尾の ``/``)、``.``・``..`` を含むときは None を返す — 素の
    結合が別の部屋のフォルダや buildings の外を指しうるので、そういう部屋は付け替え
    ない。

    それ以外の段にフォルダ名として使えない文字 (``:`` など) があっても None には
    しない。Windows ではそのフォルダは作れなかったはずで (``:`` はドライブ名や
    ファイルの別の流れの区切りとして解釈される)、移すものが無いだけ — 判定は
    :func:`legacy_folder_can_exist`。POSIX では普通のフォルダ名として作られている。
    """
    if windows is None:
        windows = _on_windows()
    separators = ("/", "\\") if windows else ("/",)
    parts = [building_id]
    for sep in separators:
        parts = [piece for chunk in parts for piece in chunk.split(sep)]
    for part in parts:
        if part in {"", ".", ".."}:
            return None
    return parts


def legacy_folder_can_exist(
    parts: Sequence[str], *, windows: Optional[bool] = None,
) -> bool:
    """:func:`legacy_folder_parts` の段が、その OS でフォルダとして作れた名前か。

    Windows ではフォルダ名に使えない文字 (``: * ? " < > |``) と制御文字を含む段は
    作れなかったので、古いフォルダは無い。False のとき、フォルダの移動は飛ばして
    ID の付け替えは進める (パスとして組み立てると ``a:b`` がドライブ名 ``a:`` に
    解釈され、buildings の外を指すので、組み立てもしない)。
    """
    if windows is None:
        windows = _on_windows()
    if not windows:
        return True
    return not any(
        ch in _WINDOWS_INVALID_NAME_CHARS or ord(ch) < 32
        for part in parts
        for ch in part
    )


def replace_exact_strings(value: Any, replacements: Mapping[str, str]) -> Tuple[Any, bool]:
    """JSON として読んだ値を辿り、``replacements`` のキーと完全一致する文字列だけを置き換える。

    対象は文字列の値と辞書のキー。**文章の途中に含まれる ID は変えない** — 部分一致で
    置き換えると、ユーザーやペルソナが書いた文章を書き換えることになる。

    辞書のキーの置き換え先が同じ辞書に既にあるときは、そのキーを置き換えない
    (片方の値を黙って消さない)。

    戻り値は (新しい値, 変わったか)。変わらなかったときは元のオブジェクトを返す。
    """
    if isinstance(value, str):
        replaced = replacements.get(value)
        if replaced is None:
            return value, False
        return replaced, True
    if isinstance(value, list):
        changed = False
        items = []
        for item in value:
            new_item, item_changed = replace_exact_strings(item, replacements)
            items.append(new_item)
            changed = changed or item_changed
        return (items if changed else value), changed
    if isinstance(value, dict):
        changed = False
        out: Dict[Any, Any] = {}
        for key, item in value.items():
            new_key = key
            if isinstance(key, str) and key in replacements:
                target = replacements[key]
                if target in value or target in out:
                    LOGGER.warning(
                        "%s JSON の辞書に付け替え先のキー %r が既にあるので、キー %r は"
                        "そのままにします",
                        _LOG_PREFIX, target, key,
                    )
                else:
                    new_key = target
                    changed = True
            new_item, item_changed = replace_exact_strings(item, replacements)
            changed = changed or item_changed
            out[new_key] = new_item
        return (out if changed else value), changed
    return value, False


def replace_exact_strings_nested(
    value: Any, replacements: Mapping[str, str],
) -> Tuple[Any, bool]:
    """:func:`replace_exact_strings` に加えて、JSON を文字列で入れ子に持つ値の中も置き換える。

    DB の JSON の欄には、値そのものが JSON の文字列になっているものがある —
    ``session_head_snapshot.LAST_NOTIFIED_JSON`` (Section ごとの snapshot の
    JSON 文字列)、``execution_outbox.PAYLOAD_JSON`` の知覚の配達 (``metadata``)。
    文字列の値が JSON の辞書か一覧として読めるときだけ中を辿り、変わったら
    ``ensure_ascii=False`` で書き戻す (書き手の head_pipeline・room_state と同じ流儀)。
    JSON として読めない文字列 (文章) は、完全一致のほかは触らない。
    """
    replaced, changed = replace_exact_strings(value, replacements)
    return _replace_inside_json_strings(replaced, replacements, changed)


def _replace_inside_json_strings(
    value: Any, replacements: Mapping[str, str], changed: bool,
) -> Tuple[Any, bool]:
    if isinstance(value, str):
        stripped = value.lstrip()
        if not stripped or stripped[0] not in "{[":
            return value, changed
        try:
            inner = json.loads(value)
        except (ValueError, RecursionError):
            return value, changed
        if not isinstance(inner, (dict, list)):
            return value, changed
        new_inner, inner_changed = replace_exact_strings_nested(inner, replacements)
        if not inner_changed:
            return value, changed
        return json.dumps(new_inner, ensure_ascii=False), True
    if isinstance(value, list):
        items = []
        any_changed = False
        for item in value:
            new_item, item_changed = _replace_inside_json_strings(item, replacements, False)
            items.append(new_item)
            any_changed = any_changed or item_changed
        return (items if any_changed else value), (changed or any_changed)
    if isinstance(value, dict):
        out: Dict[Any, Any] = {}
        any_changed = False
        for key, item in value.items():
            new_item, item_changed = _replace_inside_json_strings(item, replacements, False)
            out[key] = new_item
            any_changed = any_changed or item_changed
        return (out if any_changed else value), (changed or any_changed)
    return value, changed


# ---------------------------------------------------------------------------
# 付け替えの記録
# ---------------------------------------------------------------------------

def rename_record_path(saiverse_home: Path, city_slug: str) -> Path:
    """付け替えの記録ファイルの場所。"""
    return Path(saiverse_home) / "cities" / city_slug / RENAMES_FILENAME


def load_rename_record(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    """記録を読む。戻り値は (記録, 読めない理由)。ファイルが無ければ空の記録。"""
    if not path.exists():
        return {"format_version": RECORD_FORMAT_VERSION, "renames": []}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("renames"), list):
        return None, "記録の形が想定と違います (renames の一覧がありません)"
    return data, None


def _save_record(path: Path, record: dict) -> None:
    """同じフォルダの一時ファイルに書いてから差し替える (書きかけの記録を残さない)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

#: {小文字のテーブル名: (実際のテーブル名, {小文字の列名: (実際の列名, 宣言型)})}
_Schema = Dict[str, Tuple[str, Dict[str, Tuple[str, str]]]]


@dataclass(frozen=True)
class _Column:
    table: str
    column: str
    row_filter: _RowFilter = None

    @property
    def label(self) -> str:
        return f"{self.table}.{self.column}"

    def where_sql(self) -> str:
        if self.row_filter is None:
            return ""
        column, op, value = self.row_filter
        if isinstance(value, tuple):
            names = ", ".join(f":filter_value_{i}" for i in range(len(value)))
            return f" AND {_quote(column)} {op} ({names})"
        return f" AND {_quote(column)} {op} :filter_value"

    def params(self, **extra: Any) -> Dict[str, Any]:
        params = dict(extra)
        if self.row_filter is not None:
            value = self.row_filter[2]
            if isinstance(value, tuple):
                for i, item in enumerate(value):
                    params[f"filter_value_{i}"] = item
            else:
                params["filter_value"] = value
        return params


@dataclass
class _Plan:
    old_id: str
    display_name: str
    new_id: Optional[str] = None
    entry: Optional[dict] = None


def _read_schema(db) -> _Schema:
    schema: _Schema = {}
    tables = [
        str(row[0])
        for row in db.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ))
    ]
    for table in tables:
        columns: Dict[str, Tuple[str, str]] = {}
        for row in db.execute(text(f"PRAGMA table_info({_quote(table)})")):
            columns[str(row[1]).lower()] = (str(row[1]), str(row[2] or ""))
        schema[table.lower()] = (table, columns)
    return schema


def _resolve_columns(
    schema: _Schema, specs: Sequence[Tuple[str, str, _RowFilter]],
) -> List[_Column]:
    """一覧の欄を、この DB に実在する名前に解決する。無い欄は飛ばす (古い DB・消えた列)。"""
    resolved: List[_Column] = []
    for table, column, row_filter in specs:
        entry = schema.get(table.lower())
        if entry is None or column.lower() not in entry[1]:
            LOGGER.debug("%s %s.%s はこの DB に無いので飛ばします", _LOG_PREFIX, table, column)
            continue
        real_table, columns = entry
        real_filter: _RowFilter = None
        if row_filter is not None:
            filter_column = columns.get(row_filter[0].lower())
            if filter_column is None:
                # 絞り込めないのに全行を書き換えると、部屋以外の行まで変わる
                LOGGER.debug(
                    "%s %s.%s は絞り込みの列 %s が無いので飛ばします",
                    _LOG_PREFIX, table, column, row_filter[0],
                )
                continue
            real_filter = (filter_column[0], row_filter[1], row_filter[2])
        resolved.append(_Column(real_table, columns[column.lower()][0], real_filter))
    return resolved


def _collect_taken_ids(
    db, direct_columns: Sequence[_Column], building_ids: Sequence[str],
) -> Set[str]:
    """小文字にした使用済みの ID: 全 City の部屋 ID と、部屋を指す欄に現れる値。

    部屋を指す欄の値まで数えるのは、削除済みの部屋の行が残っていると、同じ ID に
    付け替えた部屋にその行が混ざるため。
    """
    taken = {b.lower() for b in building_ids}
    for column in direct_columns:
        rows = db.execute(
            text(
                f"SELECT DISTINCT {_quote(column.column)} FROM {_quote(column.table)} "
                f"WHERE {_quote(column.column)} IS NOT NULL{column.where_sql()}"
            ),
            column.params(),
        )
        for (value,) in rows:
            if isinstance(value, str):
                taken.add(value.lower())
    return taken


def room_replacements(renames: Mapping[str, str]) -> Dict[str, str]:
    """完全一致で置き換える対応表: 部屋 ID と、部屋の様子の記録の鍵 ``building:<ID>``。"""
    out: Dict[str, str] = {}
    for old_id, new_id in renames.items():
        out[old_id] = new_id
        out[_ROOM_KEY_PREFIX + old_id] = _ROOM_KEY_PREFIX + new_id
    return out


def _rewrite_database(
    db, schema: _Schema, plans: Sequence[_Plan],
) -> Tuple[Dict[str, Dict[str, int]], Dict[str, int]]:
    """付け替えを 1 つのトランザクションの中で書く。commit はしない (呼び出し側が持つ)。

    戻り値は (部屋ごとの {欄: 書き換えた行数}, JSON の欄ごとの書き換えた行数)。
    一意制約にぶつかったら例外がそのまま上がる — 呼び出し側がこの回の付け替えを
    まるごと巻き戻す。
    """
    # 外部キーの強制はこの DB ではオフだが、オンの環境でも主キーの書き換えを
    # commit 時点まで待たせる。
    db.execute(text("PRAGMA defer_foreign_keys = ON"))

    direct_columns = _resolve_columns(schema, DIRECT_REFERENCE_COLUMNS)
    json_columns = _resolve_columns(schema, JSON_COLUMNS)
    building_pk = _resolve_columns(schema, (("building", "BUILDINGID", None),))
    message_cols = _resolve_columns(
        schema,
        (("building_messages", "building_id", None), ("building_messages", "message_id", None)),
    )
    legacy_cols = _resolve_columns(
        schema, (("building_messages", "legacy_message_id", None),),
    )
    addon_cols = _resolve_columns(schema, (("addon_message_metadata", "message_id", None),))
    if not building_pk:
        raise RuntimeError("building.BUILDINGID がこの DB にありません")
    building_col = building_pk[0]

    per_plan: Dict[str, Dict[str, int]] = {}
    message_map: Dict[str, str] = {}
    for plan in plans:
        assert plan.new_id is not None
        old_id, new_id = plan.old_id, plan.new_id
        counts: Dict[str, int] = {}
        prefix = old_id + ":"

        # メッセージ ID (旧ID:番号 → 新ID:番号)。部屋の行を新しい ID へ移す前に、旧 ID で引く。
        #
        # 書き換える行で legacy_message_id が空なら、書き換える前のメッセージ ID を写す。
        # 起動時の確認処理と取り込み処理は、古いファイルのメッセージ ID を DB の
        # message_id と legacy_message_id の和集合と突き合わせる — message_id だけで
        # 一致していた行 (二重書き込み期の行など) が、書き換え後に「まだ移していない」
        # と数えられて二重に取り込まれるのを防ぐ。既に値がある行は変えない。
        plan_message_map: Dict[str, str] = {}
        if len(message_cols) == 2:
            bid_col, mid_col = message_cols
            legacy_col = legacy_cols[0] if legacy_cols else None
            legacy_select = _quote(legacy_col.column) if legacy_col is not None else "NULL"
            rows = db.execute(
                text(
                    f"SELECT rowid, {_quote(mid_col.column)}, {legacy_select} "
                    f"FROM {_quote(mid_col.table)} "
                    f"WHERE {_quote(bid_col.column)} = :old_id"
                ),
                {"old_id": old_id},
            ).fetchall()
            updates = []
            legacy_updates = []
            for rowid, message_id, legacy_message_id in rows:
                if isinstance(message_id, str) and message_id.startswith(prefix):
                    new_message_id = new_id + ":" + message_id[len(prefix):]
                    plan_message_map[message_id] = new_message_id
                    updates.append({"rid": rowid, "value": new_message_id})
                    if legacy_col is not None and legacy_message_id in (None, ""):
                        legacy_updates.append({"rid": rowid, "value": message_id})
            if updates:
                db.execute(
                    text(
                        f"UPDATE {_quote(mid_col.table)} SET {_quote(mid_col.column)} = :value "
                        "WHERE rowid = :rid"
                    ),
                    updates,
                )
                counts[mid_col.label] = len(updates)
            if legacy_updates:
                assert legacy_col is not None
                db.execute(
                    text(
                        f"UPDATE {_quote(legacy_col.table)} "
                        f"SET {_quote(legacy_col.column)} = :value WHERE rowid = :rid"
                    ),
                    legacy_updates,
                )
                counts[legacy_col.label] = len(legacy_updates)

        # 部屋そのもの
        result = db.execute(
            text(
                f"UPDATE {_quote(building_col.table)} SET {_quote(building_col.column)} = :new_id "
                f"WHERE {_quote(building_col.column)} = :old_id"
            ),
            {"new_id": new_id, "old_id": old_id},
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"部屋 {old_id!r} の行を付け替えられませんでした (対象の行数 {result.rowcount})"
            )
        counts[building_col.label] = result.rowcount

        # 部屋を指す欄
        for column in direct_columns:
            result = db.execute(
                text(
                    f"UPDATE {_quote(column.table)} SET {_quote(column.column)} = :new_id "
                    f"WHERE {_quote(column.column)} = :old_id{column.where_sql()}"
                ),
                column.params(new_id=new_id, old_id=old_id),
            )
            if result.rowcount:
                counts[column.label] = result.rowcount

        # アドオンのメタデータ: 付け替えたメッセージ ID と完全一致するものだけ
        if addon_cols and plan_message_map:
            addon_col = addon_cols[0]
            rows = db.execute(
                text(
                    f"SELECT rowid, {_quote(addon_col.column)} FROM {_quote(addon_col.table)} "
                    f"WHERE substr({_quote(addon_col.column)}, 1, :n) = :prefix"
                ),
                {"n": len(prefix), "prefix": prefix},
            ).fetchall()
            updates = [
                {"rid": rowid, "value": plan_message_map[message_id]}
                for rowid, message_id in rows
                if message_id in plan_message_map
            ]
            if updates:
                db.execute(
                    text(
                        f"UPDATE {_quote(addon_col.table)} SET {_quote(addon_col.column)} = :value "
                        "WHERE rowid = :rid"
                    ),
                    updates,
                )
                counts[addon_col.label] = len(updates)

        message_map.update(plan_message_map)
        per_plan[old_id] = counts

    # JSON の欄は、この回に付け替える全部屋ぶんの対応表で一度だけ読む
    replacements: Dict[str, str] = dict(message_map)
    replacements.update(
        room_replacements({plan.old_id: plan.new_id for plan in plans if plan.new_id})
    )
    json_counts: Dict[str, int] = {}
    for column in json_columns:
        rows = db.execute(
            text(
                f"SELECT rowid, {_quote(column.column)} FROM {_quote(column.table)} "
                f"WHERE {_quote(column.column)} IS NOT NULL{column.where_sql()}"
            ),
            column.params(),
        ).fetchall()
        updates = []
        for rowid, raw in rows:
            if not isinstance(raw, str) or not raw:
                continue
            try:
                data = json.loads(raw)
                new_data, changed = replace_exact_strings_nested(data, replacements)
            except (ValueError, RecursionError):
                continue  # JSON として読めない値は触らない
            if changed:
                updates.append({"rid": rowid, "value": json.dumps(new_data, ensure_ascii=False)})
        if updates:
            db.execute(
                text(
                    f"UPDATE {_quote(column.table)} SET {_quote(column.column)} = :value "
                    "WHERE rowid = :rid"
                ),
                updates,
            )
            json_counts[column.label] = len(updates)
    return per_plan, json_counts


# ---------------------------------------------------------------------------
# フォルダ
# ---------------------------------------------------------------------------

def _remove_empty_intermediate_folders(root: Path, parts: Sequence[str]) -> List[Path]:
    """旧 ID の途中の段にあったフォルダを、深いほうから、空のときだけ消す。"""
    removed: List[Path] = []
    for depth in range(len(parts) - 1, 0, -1):
        folder = root.joinpath(*parts[:depth])
        try:
            if not folder.is_dir() or any(folder.iterdir()):
                break
            folder.rmdir()
        except OSError:
            break
        removed.append(folder)
    return removed


def _move_room_folders(
    folder_roots: Sequence[Path], old_id: str, new_id: str,
) -> Tuple[bool, List[str], Optional[str]]:
    """部屋のフォルダを旧 ID の場所から新 ID の場所へ移す。

    戻り値は (最後まで済んだか, ログに残す内容, 済まなかった理由)。フォルダは消さず
    移すだけ — ``log.json`` の脇の ``log.json.corrupted_*`` なども一緒に移る。
    旧フォルダが無ければ飛ばす (ログを持たなかった部屋、または移し終わった部屋)。
    """
    parts = legacy_folder_parts(old_id)
    if parts is None:
        return False, [], "古いフォルダの場所を安全に決められません"
    if not legacy_folder_can_exist(parts):
        return True, [
            f"旧 ID {old_id!r} にはこの OS のフォルダ名に使えない文字が含まれるので、"
            "古いフォルダは作られていません。移すフォルダはありません"
        ], None
    notes: List[str] = []
    for root in folder_roots:
        src = root.joinpath(*parts)
        dst = root / new_id
        if src.is_dir():
            if _path_exists(dst):
                return False, notes, f"付け替え先のフォルダが既にあります: {dst}"
            try:
                os.rename(src, dst)
            except OSError as exc:
                return False, notes, f"フォルダを移せませんでした ({src} -> {dst}): {exc}"
            notes.append(f"フォルダを移しました: {src} -> {dst}")
        elif _path_exists(src):
            return False, notes, f"古い場所にフォルダではないものがあります: {src}"
        for folder in _remove_empty_intermediate_folders(root, parts):
            notes.append(f"空になった途中のフォルダを消しました: {folder}")
    return True, notes, None


def _deepest_first(plans: Sequence[_Plan]) -> List[_Plan]:
    """深い段の部屋から移す (``a/b/c`` を先に出さないと ``a/b`` と一緒に動いてしまう)。"""
    return sorted(plans, key=lambda plan: -len(legacy_folder_parts(plan.old_id) or [plan.old_id]))


# ---------------------------------------------------------------------------
# ペルソナの記憶のファイル (まはーの判断 1 (b))
# ---------------------------------------------------------------------------
#
# 書き換えるのは機械が読む印だけ。ペルソナが読む文面 (perception_buffer.content、
# perception_batches.rendered_text、messages.content) と添付 (media) には触らない。
#
# - perception_buffer.metadata (JSON): 部屋 ID と部屋の様子の記録の鍵を完全一致で
# - perception_buffer.reduce_key: 値が ``building:<旧ID>`` と完全一致するときだけ
# - perception_batches.room_state_json (JSON): 部屋 ID と記録の鍵を完全一致で
# - messages.metadata の building_msg_ref (写した会話の目印) だけ
#
# 部屋の様子の記帳の束 (snapshot) は部屋 ID を含み、差分の記帳は土台の束の指紋
# (base_digest = 束の正準 JSON の sha256) を持つ。束の部屋 ID を書き換えると指紋が
# 変わるので、同じ書き換えで base_digest も新しい束の指紋に付け直す — 付け直さないと
# chain_is_intact (sai_memory/room_state.py) が「土台が見えていない」と判定し、提示の
# 差分を全文へ開き直す (提示の書き換えと、同じ部屋の様子が二枚並ぶこと)。

_MEMORY_BUSY_TIMEOUT_MS = 5000
#: 写した会話の目印のキー (builtin_data/tools/get_building_messages.BUILDING_MSG_REF_KEY)
_BUILDING_MSG_REF_KEY = "building_msg_ref"
#: 完全一致の置き換えで読む JSON の欄 (テーブル, 列)
_MEMORY_JSON_COLUMNS = (
    ("perception_buffer", "metadata"),
    ("perception_batches", "room_state_json"),
)


@dataclass(frozen=True)
class MemoryUpdate:
    """記憶のファイルの 1 行 1 欄の書き換え。``before`` は読んだときの値 (書く直前に照合する)。"""

    table: str
    column: str
    rowid: int
    before: str
    after: str

    @property
    def label(self) -> str:
        return f"{self.table}.{self.column}"


def rewrite_building_msg_ref(ref: str, renames: Mapping[str, str]) -> str:
    """写した会話の目印 ``<部屋ID>:<メッセージID>`` の旧 ID を新 ID にする。

    先頭が ``旧ID:`` なら ``新ID:`` に、続くメッセージ ID の先頭が ``旧ID:`` なら
    そこも ``新ID:`` に。旧 ID どうしが前方一致しうるので、長い旧 ID から試す。
    """
    for old_id in sorted(renames, key=len, reverse=True):
        prefix = old_id + ":"
        if not ref.startswith(prefix):
            continue
        new_id = renames[old_id]
        rest = ref[len(prefix):]
        if rest.startswith(prefix):
            rest = new_id + ":" + rest[len(prefix):]
        return new_id + ":" + rest
    return ref


def _id_needles(old_ids: Sequence[str]) -> List[str]:
    """事前の絞り込みで探す文字列: 旧 ID と、JSON の文字列の中での書かれ方。"""
    needles: Set[str] = set()
    for old_id in old_ids:
        forms = {
            old_id,
            json.dumps(old_id, ensure_ascii=False)[1:-1],
            json.dumps(old_id)[1:-1],
        }
        for form in forms:
            needles.add(form)
            # JSON の文字列の中にもう一段 JSON がある書き方
            needles.add(json.dumps(form, ensure_ascii=False)[1:-1])
    return sorted(needle for needle in needles if needle)


def _memory_columns(conn: sqlite3.Connection) -> Dict[str, Set[str]]:
    """{テーブル名: {列名}} (小文字)。古い memory.db ではテーブルや列が無いことがある。"""
    tables: Dict[str, Set[str]] = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall():
        columns = {
            str(row[1]).lower()
            for row in conn.execute(f"PRAGMA table_info({_quote(str(name))})").fetchall()
        }
        tables[str(name).lower()] = columns
    return tables


def _candidate_rows(
    conn: sqlite3.Connection, table: str, column: str, needles: Sequence[str],
) -> List[Tuple[int, Any]]:
    where = " OR ".join(f"instr({_quote(column)}, ?) > 0" for _ in needles)
    return [
        (int(rowid), value)
        for rowid, value in conn.execute(
            f"SELECT rowid, {_quote(column)} FROM {_quote(table)} "
            f"WHERE {_quote(column)} IS NOT NULL AND ({where})",
            tuple(needles),
        ).fetchall()
    ]


def _room_state_records(data: Any, column: str) -> List[Any]:
    """JSON の中の部屋の様子の記帳 (バッチは一覧の要素、知覚の記録は metadata の一項目)。"""
    if column == "room_state_json":
        return list(data) if isinstance(data, list) else []
    if isinstance(data, dict):
        return [data.get(ROOM_STATE_META_KEY)]
    return []


def plan_memory_rewrite(
    conn: sqlite3.Connection, renames: Mapping[str, str],
) -> List[MemoryUpdate]:
    """記憶のファイルで書き換えが要る行を集める (読むだけ、書かない)。

    完全一致の置き換えなので、書き換え済みのファイルに対しては空の一覧になる
    (再開でもう一度呼ばれても結果が変わらない)。
    """
    if not renames:
        return []
    columns = _memory_columns(conn)
    replacements = room_replacements(renames)
    needles = _id_needles(list(renames))
    updates: List[MemoryUpdate] = []

    def has(table: str, column: str) -> bool:
        return column in columns.get(table, set())

    # JSON の欄: まず完全一致で置き換え、書き換わった束の指紋の対応表を作る
    parsed: List[Tuple[str, str, int, str, Any, Any, bool]] = []
    for table, column in _MEMORY_JSON_COLUMNS:
        if not has(table, column):
            continue
        for rowid, raw in _candidate_rows(conn, table, column, needles):
            if not isinstance(raw, str) or not raw:
                continue
            try:
                data = json.loads(raw)
            except (ValueError, RecursionError):
                continue  # JSON として読めない値は触らない
            new_data, changed = replace_exact_strings(data, replacements)
            parsed.append((table, column, rowid, raw, data, new_data, changed))

    digest_map: Dict[str, str] = {}
    for _table, column, _rowid, _raw, data, new_data, changed in parsed:
        if not changed:
            continue
        for before, after in zip(
            _room_state_records(data, column), _room_state_records(new_data, column),
        ):
            if not isinstance(before, dict) or not isinstance(after, dict):
                continue
            old_snapshot, new_snapshot = before.get("snapshot"), after.get("snapshot")
            if old_snapshot is new_snapshot or not bundle_is_valid(old_snapshot):
                continue
            if not bundle_is_valid(new_snapshot):
                continue
            old_digest = snapshot_digest(old_snapshot)
            new_digest = snapshot_digest(new_snapshot)
            if old_digest != new_digest:
                digest_map[old_digest] = new_digest

    for table, column, rowid, raw, _data, new_data, changed in parsed:
        if digest_map and any(
            isinstance(record, dict)
            and isinstance(record.get("base_digest"), str)
            and record["base_digest"] in digest_map
            for record in _room_state_records(new_data, column)
        ):
            # replace_exact_strings は変わらなかった部分を元のオブジェクトと共有する
            new_data = copy.deepcopy(new_data)
            for record in _room_state_records(new_data, column):
                if not isinstance(record, dict):
                    continue
                base = record.get("base_digest")
                if isinstance(base, str) and base in digest_map:
                    record["base_digest"] = digest_map[base]
            changed = True
        if changed:
            updates.append(MemoryUpdate(
                table, column, rowid, raw, json.dumps(new_data, ensure_ascii=False),
            ))

    # 知覚の reduce の鍵: 値が部屋の様子の記録の鍵と完全一致するときだけ
    if has("perception_buffer", "reduce_key"):
        keys = {
            _ROOM_KEY_PREFIX + old_id: _ROOM_KEY_PREFIX + new_id
            for old_id, new_id in renames.items()
        }
        placeholders = ", ".join("?" for _ in keys)
        for rowid, value in conn.execute(
            "SELECT rowid, reduce_key FROM perception_buffer "
            f"WHERE reduce_key IN ({placeholders})",
            tuple(keys),
        ).fetchall():
            updates.append(MemoryUpdate(
                "perception_buffer", "reduce_key", int(rowid), value, keys[value],
            ))

    # 記憶の行の写した会話の目印 (ほかの項目は変えない)
    if has("messages", "metadata"):
        for rowid, raw in _candidate_rows(conn, "messages", "metadata", needles):
            if not isinstance(raw, str) or not raw:
                continue
            try:
                data = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if not isinstance(data, dict):
                continue
            ref = data.get(_BUILDING_MSG_REF_KEY)
            if not isinstance(ref, str):
                continue
            new_ref = rewrite_building_msg_ref(ref, renames)
            if new_ref == ref:
                continue
            new_data = dict(data)
            new_data[_BUILDING_MSG_REF_KEY] = new_ref
            updates.append(MemoryUpdate(
                "messages", "metadata", rowid, raw,
                json.dumps(new_data, ensure_ascii=False),
            ))
    return updates


def apply_memory_rewrite(
    conn: sqlite3.Connection, updates: Sequence[MemoryUpdate],
) -> Dict[str, int]:
    """書き換えを一つのトランザクションで書く。戻り値は欄ごとの書き換えた行数。

    書く直前に、読んだときの値と同じかを照合する。読んだ後に変わった行があれば、
    このファイルの書き換えをまるごと巻き戻して例外を上げる。
    """
    counts: Dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for update in updates:
            cursor = conn.execute(
                f"UPDATE {_quote(update.table)} SET {_quote(update.column)} = ? "
                f"WHERE rowid = ? AND {_quote(update.column)} = ?",
                (update.after, update.rowid, update.before),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"{update.label} の行 {update.rowid} が、読んだ後に変わっていました"
                )
            counts[update.label] = counts.get(update.label, 0) + 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return counts


def _backup_memory_db(conn: sqlite3.Connection, dest: Path) -> None:
    """記憶のファイルを SQLite のバックアップ機能で丸ごと複製する (書き込み途中の状態を写さない)。

    一時的な名前に書いてから差し替える — 途中で失敗した複製を、完全な控えと
    取り違えないため。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    try:
        target = sqlite3.connect(str(partial))
        try:
            conn.backup(target)
        finally:
            target.close()
        os.replace(partial, dest)
    except BaseException:
        try:
            partial.unlink()  # この関数が作りかけた複製だけを片付ける
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 警告
# ---------------------------------------------------------------------------

def _alert(alert_id: str, title: str, message: str, details: dict) -> dict:
    return {
        "id": alert_id,
        "level": "warning",
        "title": title,
        "message": message,
        "details": details,
    }


def _rooms_text(plans: Sequence[_Plan]) -> str:
    return "、".join(f"「{plan.display_name}」" for plan in plans)


def _plans_details(plans: Sequence[_Plan]) -> List[dict]:
    return [
        {"building_id": plan.old_id, "new_building_id": plan.new_id, "name": plan.display_name}
        for plan in plans
    ]


def _pending_sentence(plans: Sequence[_Plan]) -> str:
    return (
        f"部屋{_rooms_text(plans)}は、内部の名前にフォルダ名や URL に使えない文字"
        "（「/」「?」など）を含む古い形式のままで、名前の付け替えが済んでいません。"
    )


_NOT_SHOWN_SENTENCE = "付け替えが済むまで、この部屋の過去の会話が表示されないことがあります。"
_TITLE_SKIPPED = "古い形式の部屋の名前の付け替えを見送りました"
_TITLE_FAILED = "古い形式の部屋の名前の付け替えに失敗しました"


def _skipped_running_alert(plans: Sequence[_Plan], owner: str) -> dict:
    return _alert(
        "building_id_repair_skipped_running",
        _TITLE_SKIPPED,
        _pending_sentence(plans)
        + "同じデータを使う別の SAIVerse が動いているため、今回は付け替えを見送りました。"
        "別の SAIVerse を止めてから起動し直すと、付け替えます。"
        + _NOT_SHOWN_SENTENCE,
        {"reason": "another_process_owns_db", "owner": owner, "buildings": _plans_details(plans)},
    )


def _skipped_backup_alert(plans: Sequence[_Plan], error: str) -> dict:
    return _alert(
        "building_id_repair_skipped_backup",
        _TITLE_SKIPPED,
        _pending_sentence(plans)
        + "付け替えの前にデータベースの控え（バックアップ）を作ろうとしましたが、"
        "作れなかったため見送りました。次の起動でもう一度試します。"
        + _NOT_SHOWN_SENTENCE,
        {"reason": "backup_failed", "error": error, "buildings": _plans_details(plans)},
    )


def _skipped_record_alert(plans: Sequence[_Plan], path: Path, error: str) -> dict:
    return _alert(
        "building_id_repair_skipped_record",
        _TITLE_SKIPPED,
        _pending_sentence(plans)
        + "付け替えの記録ファイルに書き込めなかったため見送りました。"
        "次の起動でもう一度試します。"
        + _NOT_SHOWN_SENTENCE,
        {
            "reason": "record_write_failed",
            "path": str(path),
            "error": error,
            "buildings": _plans_details(plans),
        },
    )


def _unsafe_id_alert(plan: _Plan, candidate: Optional[str]) -> dict:
    return _alert(
        f"building_id_repair_unsafe_{plan.old_id}",
        f"部屋「{plan.display_name}」の内部の名前を付け替えられません",
        "この部屋の内部の名前は、フォルダ名や URL に使えない文字（「/」「?」など）を"
        "「_」に置き換えても、フォルダ名として使えない形（Windows が特別に扱う名前など）"
        "のままです。自動では安全な名前を決められないため、付け替えていません。"
        "この部屋の過去の会話が表示されないことがあります。",
        {"reason": "unsafe_new_id", "building_id": plan.old_id, "candidate": candidate},
    )


def _unsafe_folder_alert(plan: _Plan) -> dict:
    return _alert(
        f"building_id_repair_unsafe_{plan.old_id}",
        f"部屋「{plan.display_name}」の内部の名前を付け替えられません",
        "この部屋の内部の名前は、古い会話が入ったフォルダの場所を安全に決められない形を"
        "しています（区切り記号が続いている、先頭や末尾にある など）。自動では付け替え"
        "られないため、付け替えていません。この部屋の過去の会話が表示されないことがあります。",
        {"reason": "unsafe_legacy_folder", "building_id": plan.old_id},
    )


def _database_failed_alert(plans: Sequence[_Plan], exc: BaseException) -> dict:
    return _alert(
        "building_id_repair_failed",
        _TITLE_FAILED,
        _pending_sentence(plans)
        + "付け替えの途中で問題が起きたため、データベースは付け替える前の状態に戻しました。"
        "次の起動でもう一度試します。"
        + _NOT_SHOWN_SENTENCE,
        {"error": f"{type(exc).__name__}: {exc}", "buildings": _plans_details(plans)},
    )


def _folder_failed_alert(plan: _Plan, problem: Optional[str]) -> dict:
    return _alert(
        f"building_id_repair_folder_{plan.new_id}",
        f"部屋「{plan.display_name}」の古い会話のフォルダを移せませんでした",
        "この部屋の内部の名前は付け替えましたが、古い会話が入ったフォルダを新しい名前の"
        "場所へ移せませんでした。フォルダは消さずに残しています。次の起動でもう一度"
        "試します。移せるまで、この部屋の過去の会話は表示されません。",
        {
            "reason": "folder_move_failed",
            "building_id": plan.old_id,
            "new_building_id": plan.new_id,
            "problem": problem,
        },
    )


_MEMORY_NOT_REWRITTEN_SENTENCE = (
    "書き換わるまで、このペルソナに付け替えた部屋の様子が二重に届いたり、"
    "その部屋の会話の一部が二重に記憶されたりすることがあります。"
)


def _memory_backup_failed_alert(persona_id: str, name: str, path: Path, exc: BaseException) -> dict:
    return _alert(
        f"building_id_repair_memory_backup_{persona_id}",
        f"ペルソナ「{name}」の記憶の中の部屋の名前を書き換えられませんでした",
        "部屋の内部の名前を付け替えたので、ペルソナの記憶のファイルに記録されている"
        "部屋の内部の名前も書き換える必要があります。書き換える前に記憶のファイルの"
        "控え（バックアップ）を作ろうとしましたが、作れなかったため、このペルソナの記憶は"
        "書き換えていません。次の起動でもう一度試します。"
        + _MEMORY_NOT_REWRITTEN_SENTENCE,
        {
            "reason": "memory_backup_failed",
            "persona_id": persona_id,
            "path": str(path),
            "error": f"{type(exc).__name__}: {exc}",
        },
    )


def _memory_rewrite_failed_alert(persona_id: str, name: str, path: Path, exc: BaseException) -> dict:
    return _alert(
        f"building_id_repair_memory_rewrite_{persona_id}",
        f"ペルソナ「{name}」の記憶の中の部屋の名前を書き換えられませんでした",
        "部屋の内部の名前を付け替えたので、ペルソナの記憶のファイルに記録されている"
        "部屋の内部の名前も書き換えようとしましたが、途中で問題が起きました。"
        "このペルソナの記憶は、書き換える前の状態のままです。次の起動でもう一度試します。"
        + _MEMORY_NOT_REWRITTEN_SENTENCE,
        {
            "reason": "memory_rewrite_failed",
            "persona_id": persona_id,
            "path": str(path),
            "error": f"{type(exc).__name__}: {exc}",
        },
    )


def _memory_list_failed_alert(exc: BaseException) -> dict:
    return _alert(
        "building_id_repair_memory_list_failed",
        "ペルソナの記憶の中の部屋の名前を書き換えられませんでした",
        "部屋の内部の名前を付け替えた後、ペルソナの一覧を読めなかったため、"
        "ペルソナの記憶のファイルに記録されている部屋の内部の名前を書き換えていません。"
        "次の起動でもう一度試します。"
        + _MEMORY_NOT_REWRITTEN_SENTENCE,
        {"reason": "memory_list_failed", "error": f"{type(exc).__name__}: {exc}"},
    )


def _record_unreadable_alert(path: Path, error: Optional[str]) -> dict:
    return _alert(
        "building_id_repair_record_unreadable",
        "部屋の名前の付け替えの記録が読めません",
        "フォルダ名や URL に使えない文字（「/」など）を含む古い形式の部屋の名前を付け替えた記録ファイルが"
        "壊れていて読めないため、付け替えの確認と続きの作業を見送りました。"
        "記録ファイルは消さずに残しています。部屋の過去の会話が表示されない場合は、"
        "この警告の内容を添えて開発者に知らせてください。",
        {"reason": "record_unreadable", "path": str(path), "error": error},
    )


def unexpected_failure_alert(exc: BaseException) -> dict:
    """付け替えの処理そのものが例外で倒れたとき (起動は続ける) の警告。"""
    return _alert(
        "building_id_repair_failed",
        _TITLE_FAILED,
        "フォルダ名や URL に使えない文字（「/」など）を含む古い形式の部屋の名前を"
        "確かめて付け替える処理が、"
        "途中で止まりました。起動は続けています。次の起動でもう一度試します。"
        "部屋の過去の会話が表示されない場合は、この警告の内容を添えて開発者に"
        "知らせてください。",
        {"error": f"{type(exc).__name__}: {exc}"},
    )


# ---------------------------------------------------------------------------
# Discord の対応表 (SAIVerse からは直せない)
# ---------------------------------------------------------------------------

def _mapped_building_ids(raw: str) -> List[str]:
    """対応表の JSON から building_id を集める。

    ``ChannelMapping.from_json`` (discord_gateway/mapping.py) が受け付ける 2 つの形 —
    項目の一覧、またはチャンネル ID をキーにした辞書 — を同じように読む。
    ChannelMapping には項目を列挙する公開の口が無いので、形の読み方だけを揃える。
    """
    payload = json.loads(raw)
    if isinstance(payload, dict):
        entries = list(payload.values())
    elif isinstance(payload, list):
        entries = payload
    else:
        raise ValueError("Channel mapping must be list or dict")
    return [
        str(entry["building_id"])
        for entry in entries
        if isinstance(entry, Mapping) and "building_id" in entry
    ]


def discord_mapping_alerts(
    record: dict, environ: Optional[Mapping[str, str]] = None,
) -> List[dict]:
    """付け替え済みの旧 ID が Discord の対応表に残っていれば、書き換えをお願いする。

    付け替えた回だけでなく毎起動確かめる — 一度だけ出して消えると、対応表は旧 ID の
    まま誰にも気づかれない。環境変数が無い・読めないときは黙って飛ばす (ログだけ)。
    """
    env = os.environ if environ is None else environ
    raw = env.get(DISCORD_CHANNEL_MAP_ENV, "")
    if not raw:
        return []
    renamed: Dict[str, str] = {}
    for entry in record.get("renames", []):
        if not isinstance(entry, dict) or not entry.get("db_renamed_at"):
            continue
        old_id, new_id = entry.get("old_id"), entry.get("new_id")
        if isinstance(old_id, str) and isinstance(new_id, str):
            renamed[old_id] = new_id
    if not renamed:
        return []
    try:
        mapped = _mapped_building_ids(raw)
    except Exception:
        LOGGER.info(
            "%s 環境変数 %s が読めないので、Discord の対応表の確認を飛ばします",
            _LOG_PREFIX, DISCORD_CHANNEL_MAP_ENV, exc_info=True,
        )
        return []
    hits = sorted({(building_id, renamed[building_id]) for building_id in mapped if building_id in renamed})
    if not hits:
        return []
    pairs = "に、".join(f"`{old_id}` を `{new_id}`" for old_id, new_id in hits)
    LOGGER.warning(
        "%s Discord の対応表 (%s) に付け替え前の部屋 ID が残っています: %s",
        _LOG_PREFIX, DISCORD_CHANNEL_MAP_ENV, hits,
    )
    return [_alert(
        "building_id_repair_discord_mapping",
        "Discord 連携の設定に、付け替える前の部屋の名前が残っています",
        f"Discord のチャンネルと部屋の対応表（環境変数 {DISCORD_CHANNEL_MAP_ENV}）に、"
        f"付け替える前の部屋の名前が残っています。対応表の {pairs} に書き換えてください。"
        "書き換えるまで、そのチャンネルと部屋が正しく結びつかない可能性があります。",
        {
            "reason": "discord_channel_map",
            "env": DISCORD_CHANNEL_MAP_ENV,
            "renames": [{"old_id": o, "new_id": n} for o, n in hits],
        },
    )]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

class _Repair:
    """1 回の起動ぶんの付け替え。警告は self.alerts に溜める。"""

    def __init__(
        self,
        *,
        session_factory,
        db_path,
        city_id: int,
        saiverse_home: Path,
        folder_roots: Sequence[Path],
        record: dict,
        record_path: Path,
    ) -> None:
        self.session_factory = session_factory
        self.db_path = db_path
        self.city_id = city_id
        self.saiverse_home = Path(saiverse_home)
        self.folder_roots = list(folder_roots)
        self.record = record
        self.record_path = record_path
        self.renames: List[dict] = record["renames"]
        self.alerts: List[dict] = []
        # 記憶のファイルの複製の置き場。最初に複製するときに決める (この回の起動で一つ)。
        self._memory_backup_dir: Optional[Path] = None

    # -- 全体の流れ ----------------------------------------------------------

    def run(self) -> List[dict]:
        buildings = self._load_buildings()
        city_rooms = {
            building_id: (name or building_id)
            for building_id, city_id, name in buildings
            if str(city_id) == str(self.city_id)
        }
        db_plans, folder_plans, record_dirty = self._classify(city_rooms)

        if not db_plans and not folder_plans:
            if record_dirty and not self._another_process_owns_db()[0]:
                self._save_record_best_effort()
            return self.alerts

        owned, owner = self._another_process_owns_db()
        if owned:
            LOGGER.warning(
                "%s 同じ DB を使う別の SAIVerse が動いているので、付け替えを見送ります: %s",
                _LOG_PREFIX, owner,
            )
            self.alerts.append(_skipped_running_alert([*db_plans, *folder_plans], owner))
            return self.alerts

        if record_dirty:
            self._save_record_best_effort()
        # DB は付け替え済みで、記憶のファイルの印とフォルダが残っている部屋
        # (手順 2 の後で止まった)。印の書き換えは完全一致の置き換えなので、途中まで
        # 済んだペルソナにもう一度行っても結果は変わらない。
        if folder_plans:
            memories_done = self._rewrite_persona_memories(folder_plans)
            for plan in _deepest_first(folder_plans):
                self._move_folders(plan, finish=memories_done)
        if db_plans:
            self._rename(db_plans, [building_id for building_id, _, _ in buildings])
        return self.alerts

    def _another_process_owns_db(self) -> Tuple[bool, str]:
        from saiverse.runtime_marker import another_running_process_owns_db

        return another_running_process_owns_db(self.db_path)

    def _load_buildings(self) -> List[Tuple[str, Any, Optional[str]]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                text('SELECT "BUILDINGID", "CITYID", "BUILDINGNAME" FROM "building"')
            ).fetchall()
        finally:
            db.close()
        return [(str(row[0]), row[1], row[2]) for row in rows if row[0] is not None]

    def _classify(
        self, city_rooms: Dict[str, str],
    ) -> Tuple[List[_Plan], List[_Plan], bool]:
        """記録の「予定」と DB の姿から、やることを振り分ける。

        戻り値は (DB の書き換えからやる部屋, フォルダの移動からやる部屋, 記録を書き直すか)。
        """
        db_plans: List[_Plan] = []
        folder_plans: List[_Plan] = []
        dirty = False
        seen: Set[str] = set()
        for entry in self.renames:
            if not isinstance(entry, dict) or entry.get("status") != STATUS_PLANNED:
                continue
            old_id, new_id = entry.get("old_id"), entry.get("new_id")
            if (
                not isinstance(old_id, str)
                or not isinstance(new_id, str)
                or not needs_building_id_repair(old_id)
                or old_id in seen
            ):
                LOGGER.warning(
                    "%s 記録の予定を読み飛ばします (形が想定と違うか、同じ部屋の予定が重複): %r",
                    _LOG_PREFIX, entry,
                )
                continue
            seen.add(old_id)
            if old_id in city_rooms:
                db_plans.append(_Plan(old_id, city_rooms[old_id], new_id=new_id, entry=entry))
            elif new_id in city_rooms:
                if not entry.get("db_renamed_at"):
                    entry["db_renamed_at"] = _now()
                    dirty = True
                folder_plans.append(_Plan(old_id, city_rooms[new_id], new_id=new_id, entry=entry))
            else:
                LOGGER.warning(
                    "%s 記録にある付け替えの予定 %r -> %r の部屋が DB にありません。"
                    "フォルダは動かさず、予定を閉じます",
                    _LOG_PREFIX, old_id, new_id,
                )
                entry["status"] = STATUS_DONE
                entry["done_at"] = _now()
                entry["note"] = NOTE_ROOM_NOT_FOUND
                dirty = True
        for building_id, name in city_rooms.items():
            if needs_building_id_repair(building_id) and building_id not in seen:
                db_plans.append(_Plan(building_id, name))
        return db_plans, folder_plans, dirty

    # -- DB の書き換え -------------------------------------------------------

    def _rename(self, plans: List[_Plan], all_building_ids: Sequence[str]) -> None:
        db = self.session_factory()
        try:
            schema = _read_schema(db)
            taken = _collect_taken_ids(
                db, _resolve_columns(schema, DIRECT_REFERENCE_COLUMNS), all_building_ids,
            )
        finally:
            db.close()

        accepted = self._decide_new_ids(plans, taken)
        if not accepted:
            return

        try:
            from database.backup import backup_saiverse_db

            backup_path = backup_saiverse_db(Path(self.db_path), kind="building_id_repair")
            if backup_path is None:
                raise RuntimeError(f"控えを作る元のデータベースが見つかりません: {self.db_path}")
        except Exception as exc:
            LOGGER.error(
                "%s データベースの控えを作れなかったので、付け替えを見送ります",
                _LOG_PREFIX, exc_info=True,
            )
            self.alerts.append(_skipped_backup_alert(accepted, f"{type(exc).__name__}: {exc}"))
            return
        LOGGER.info("%s 付け替えの前にデータベースの控えを作りました: %s", _LOG_PREFIX, backup_path)

        # 手順 1: 記録に「予定」を書く
        planned_at = _now()
        for plan in accepted:
            if plan.entry is None:
                plan.entry = {
                    "old_id": plan.old_id,
                    "new_id": plan.new_id,
                    "building_name": plan.display_name,
                    "status": STATUS_PLANNED,
                    "planned_at": planned_at,
                }
                self.renames.append(plan.entry)
            elif plan.entry.get("new_id") != plan.new_id:
                LOGGER.info(
                    "%s 記録の予定の ID %r が空いていないので、%r に決め直します",
                    _LOG_PREFIX, plan.entry.get("new_id"), plan.new_id,
                )
                plan.entry["new_id"] = plan.new_id
                plan.entry["planned_at"] = planned_at
        try:
            _save_record(self.record_path, self.record)
        except OSError as exc:
            LOGGER.error(
                "%s 付け替えの記録を書き込めないので、付け替えを見送ります: %s",
                _LOG_PREFIX, self.record_path, exc_info=True,
            )
            self.alerts.append(
                _skipped_record_alert(accepted, self.record_path, f"{type(exc).__name__}: {exc}")
            )
            return

        # 手順 2: DB を一つのトランザクションで書き換える
        db = self.session_factory()
        try:
            per_plan, json_counts = _rewrite_database(db, schema, accepted)
            db.commit()
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                LOGGER.debug("%s rollback にも失敗しました", _LOG_PREFIX, exc_info=True)
            LOGGER.error(
                "%s 付け替えの途中で失敗したので、データベースを元に戻しました",
                _LOG_PREFIX, exc_info=True,
            )
            self.alerts.append(_database_failed_alert(accepted, exc))
            return
        finally:
            db.close()

        renamed_at = _now()
        for plan in accepted:
            assert plan.entry is not None
            plan.entry["db_renamed_at"] = renamed_at
            LOGGER.info(
                "%s 部屋 %r (表示名 %r) の ID を %r に付け替えました。書き換えた行数: %s",
                _LOG_PREFIX, plan.old_id, plan.display_name, plan.new_id, per_plan.get(plan.old_id),
            )
        if json_counts:
            LOGGER.info("%s JSON の欄で書き換えた行数: %s", _LOG_PREFIX, json_counts)
        self._save_record_best_effort()
        self._log_remaining_references(schema, [plan.old_id for plan in accepted])

        # 手順 3: ペルソナの記憶のファイルの印を書き換える
        memories_done = self._rewrite_persona_memories(accepted)
        # 手順 4・5: フォルダを移し、記録を「完了」にする
        for plan in _deepest_first(accepted):
            self._move_folders(plan, finish=memories_done)

    def _decide_new_ids(self, plans: Sequence[_Plan], taken: Set[str]) -> List[_Plan]:
        accepted: List[_Plan] = []
        for plan in plans:
            if legacy_folder_parts(plan.old_id) is None:
                LOGGER.warning(
                    "%s 部屋 %r は古いフォルダの場所を安全に決められないので、付け替えません",
                    _LOG_PREFIX, plan.old_id,
                )
                self.alerts.append(_unsafe_folder_alert(plan))
                continue
            preferred = plan.new_id
            reuse_folder = False
            if preferred is None:
                preferred = self._previous_new_id(plan.old_id)
                reuse_folder = preferred is not None
            new_id = choose_new_building_id(
                plan.old_id,
                taken=taken,
                folder_roots=self.folder_roots,
                preferred=preferred,
                preferred_folder_may_exist=reuse_folder,
            )
            if new_id is None or not is_safe_path_component(new_id):
                LOGGER.warning(
                    "%s 部屋 %r の付け替え先 %r がフォルダ名として安全でないので、付け替えません",
                    _LOG_PREFIX, plan.old_id, new_id,
                )
                self.alerts.append(_unsafe_id_alert(plan, new_id))
                continue
            taken.add(new_id.lower())
            plan.new_id = new_id
            accepted.append(plan)
        return accepted

    def _previous_new_id(self, old_id: str) -> Optional[str]:
        """この部屋を以前付け替え終えた記録があれば、そのときの新 ID。

        控え (バックアップ) から DB だけを戻すと、部屋は旧 ID に戻るがフォルダは新 ID の
        場所にある。同じ新 ID に付け替え直せば、フォルダと部屋がまた揃う。
        """
        for entry in reversed(self.renames):
            if (
                isinstance(entry, dict)
                and entry.get("old_id") == old_id
                and entry.get("status") == STATUS_DONE
                and entry.get("note") != NOTE_ROOM_NOT_FOUND
                and isinstance(entry.get("new_id"), str)
            ):
                return entry["new_id"]
        return None

    def _log_remaining_references(self, schema: _Schema, old_ids: Sequence[str]) -> None:
        """付け替えの後、文字列の欄に旧 ID を含む値が残っていないかを数えてログに出す。

        書き換えはしない。一覧から漏れた欄を見つける手がかりにする。
        """
        try:
            db = self.session_factory()
            try:
                total = 0
                for _key, (table, columns) in sorted(schema.items()):
                    for _col_key, (column, declared) in sorted(columns.items()):
                        if not any(marker in declared.upper() for marker in _STRING_TYPE_MARKERS):
                            continue
                        for old_id in old_ids:
                            count = db.execute(
                                text(
                                    f"SELECT COUNT(*) FROM {_quote(table)} "
                                    f"WHERE instr({_quote(column)}, :old_id) > 0"
                                ),
                                {"old_id": old_id},
                            ).scalar() or 0
                            if count:
                                total += count
                                LOGGER.info(
                                    "%s 付け替えの後も %s.%s の %d 行に旧 ID %r を含む値があります",
                                    _LOG_PREFIX, table, column, count, old_id,
                                )
                LOGGER.info(
                    "%s 旧 ID を含む値の数え上げ: 合計 %d 行 (building_messages.legacy_message_id、"
                    "session_head_snapshot.SECTIONS_JSON、配達済み・終わった実行台帳の記録、"
                    "文章の中の ID は変えない決まりなので、そこに数が出るのは正常です)",
                    _LOG_PREFIX, total,
                )
            finally:
                db.close()
        except Exception:
            LOGGER.warning(
                "%s 旧 ID が残っていないかを数えられませんでした", _LOG_PREFIX, exc_info=True,
            )

    # -- フォルダと記録 ------------------------------------------------------

    def _move_folders(self, plan: _Plan, *, finish: bool = True) -> None:
        """フォルダを移し、``finish`` なら記録を「完了」にする。

        ``finish`` が False なのは、記憶のファイルの印を書き換えられなかったペルソナが
        いた回 — 記録を「予定」のまま残し、次の起動で印の書き換えからやり直す。
        """
        assert plan.new_id is not None and plan.entry is not None
        ok, notes, problem = _move_room_folders(self.folder_roots, plan.old_id, plan.new_id)
        for note in notes:
            LOGGER.info("%s %s", _LOG_PREFIX, note)
        if not ok:
            LOGGER.error(
                "%s 部屋 %r -> %r のフォルダを移せませんでした: %s",
                _LOG_PREFIX, plan.old_id, plan.new_id, problem,
            )
            self.alerts.append(_folder_failed_alert(plan, problem))
            return
        if not finish:
            LOGGER.warning(
                "%s 部屋 %r -> %r: 記憶のファイルの書き換えが済んでいないペルソナがいるので、"
                "記録は「予定」のまま残します (次の起動で続きを行います)",
                _LOG_PREFIX, plan.old_id, plan.new_id,
            )
            return
        plan.entry["status"] = STATUS_DONE
        plan.entry["done_at"] = _now()
        self._save_record_best_effort()

    # -- ペルソナの記憶のファイル ------------------------------------------

    def _load_personas(self) -> List[Tuple[str, Optional[str]]]:
        db = self.session_factory()
        try:
            rows = db.execute(text('SELECT "AIID", "AINAME" FROM "ai"')).fetchall()
        finally:
            db.close()
        return [(str(row[0]), row[1]) for row in rows if row[0] is not None]

    def _rewrite_persona_memories(self, plans: Sequence[_Plan]) -> bool:
        """DB に登録されている全ペルソナの記憶のファイルの印を書き換える。

        戻り値は、書き換えが要ったペルソナの全員について済んだか (失敗した
        ペルソナがいれば False — 警告は self.alerts に載せてある)。
        """
        renames = {plan.old_id: plan.new_id for plan in plans if plan.new_id}
        if not renames:
            return True
        try:
            personas = self._load_personas()
        except Exception as exc:
            LOGGER.error(
                "%s ペルソナの一覧を読めないので、記憶のファイルの印を書き換えられません",
                _LOG_PREFIX, exc_info=True,
            )
            self.alerts.append(_memory_list_failed_alert(exc))
            return False
        all_done = True
        for persona_id, name in personas:
            if not is_safe_path_component(persona_id):
                LOGGER.warning(
                    "%s ペルソナ ID %r がフォルダ名として使えないので、記憶のファイルを確かめません",
                    _LOG_PREFIX, persona_id,
                )
                continue
            path = self.saiverse_home / "personas" / persona_id / "memory.db"
            if not path.is_file():
                continue
            if not self._rewrite_one_memory(persona_id, name or persona_id, path, renames):
                all_done = False
        return all_done

    def _rewrite_one_memory(
        self, persona_id: str, name: str, path: Path, renames: Mapping[str, str],
    ) -> bool:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(str(path), timeout=_MEMORY_BUSY_TIMEOUT_MS / 1000)
            conn.execute(f"PRAGMA busy_timeout = {_MEMORY_BUSY_TIMEOUT_MS}")
            updates = plan_memory_rewrite(conn, renames)
            if not updates:
                LOGGER.debug(
                    "%s ペルソナ %r の記憶のファイルに書き換える印はありません", _LOG_PREFIX, persona_id,
                )
                return True
            dest = self._memory_backup_path(persona_id)
            try:
                _backup_memory_db(conn, dest)
            except Exception as exc:
                LOGGER.error(
                    "%s ペルソナ %r の記憶のファイルの複製を作れなかったので、書き換えません: %s",
                    _LOG_PREFIX, persona_id, dest, exc_info=True,
                )
                self.alerts.append(_memory_backup_failed_alert(persona_id, name, path, exc))
                return False
            LOGGER.info(
                "%s ペルソナ %r の記憶のファイルを複製しました: %s", _LOG_PREFIX, persona_id, dest,
            )
            counts = apply_memory_rewrite(conn, updates)
            LOGGER.info(
                "%s ペルソナ %r の記憶のファイルの印を書き換えました。欄ごとの行数: %s",
                _LOG_PREFIX, persona_id, counts,
            )
            return True
        except Exception as exc:
            LOGGER.error(
                "%s ペルソナ %r の記憶のファイルの印を書き換えられませんでした: %s",
                _LOG_PREFIX, persona_id, path, exc_info=True,
            )
            self.alerts.append(_memory_rewrite_failed_alert(persona_id, name, path, exc))
            return False
        finally:
            if conn is not None:
                conn.close()

    def _memory_backup_path(self, persona_id: str) -> Path:
        """``<ホーム>/backups/building_id_repair/<日時>/<persona_id>_memory.db`` (既にあれば番号を足す)。"""
        if self._memory_backup_dir is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = self.saiverse_home / "backups" / "building_id_repair"
            folder = base / stamp
            for n in range(2, _MAX_SUFFIX + 2):
                if not _path_exists(folder):
                    break
                folder = base / f"{stamp}_{n}"
            self._memory_backup_dir = folder
        dest = self._memory_backup_dir / f"{persona_id}_memory.db"
        for n in range(2, _MAX_SUFFIX + 2):
            if not _path_exists(dest):
                break
            dest = self._memory_backup_dir / f"{persona_id}_memory_{n}.db"
        return dest

    def _save_record_best_effort(self) -> None:
        try:
            _save_record(self.record_path, self.record)
        except OSError:
            LOGGER.warning(
                "%s 付け替えの記録を書き込めませんでした。次の起動で DB とフォルダの姿から"
                "続きを判断します: %s",
                _LOG_PREFIX, self.record_path, exc_info=True,
            )


def repair_unsafe_building_ids(
    *,
    session_factory,
    db_path,
    saiverse_home: Path,
    city_id: int,
    city_slug: str,
    environ: Optional[Mapping[str, str]] = None,
) -> List[dict]:
    """この City の部屋のうち、ID にフォルダ名や URL を壊す文字を含むものを付け替える。

    起動時、``_init_city_config`` の直後・``_init_buildings`` の前に呼ぶ
    (manager/initialization.py)。対象が無ければ何もしない。

    Args:
        session_factory: saiverse.db のセッションを作る呼び出し可能オブジェクト
        db_path: saiverse.db のパス (多重起動の確認とバックアップに使う)
        saiverse_home: ``~/.saiverse`` (テストでは一時フォルダ)。部屋のフォルダ、
            ペルソナの記憶のファイル (``personas/<AIID>/memory.db``)、その複製の置き場
            (``backups/building_id_repair/``) はすべてこの下
        city_id / city_slug: この City の CITYID と CITY_SLUG
        environ: Discord の対応表を読む環境変数 (既定は os.environ)

    Returns:
        startup_alerts に載せる警告の一覧。付け替えが成功しただけなら空。
    """
    if not is_safe_path_component(city_slug):
        LOGGER.warning(
            "%s City の識別子 %r がフォルダ名として使えないので、付け替えを確かめません",
            _LOG_PREFIX, city_slug,
        )
        return []
    saiverse_home = Path(saiverse_home)
    record_path = rename_record_path(saiverse_home, city_slug)
    record, record_error = load_rename_record(record_path)
    if record is None:
        LOGGER.error(
            "%s 付け替えの記録が読めないので、付け替えを見送ります: %s (%s)",
            _LOG_PREFIX, record_path, record_error,
        )
        return [_record_unreadable_alert(record_path, record_error)]

    city_dir = saiverse_home / "cities" / city_slug
    repair = _Repair(
        session_factory=session_factory,
        db_path=db_path,
        city_id=city_id,
        saiverse_home=saiverse_home,
        folder_roots=[city_dir / "buildings", saiverse_home / "buildings"],
        record=record,
        record_path=record_path,
    )
    alerts = repair.run()
    alerts.extend(discord_mapping_alerts(record, environ))
    return alerts


__all__ = [
    "DIRECT_REFERENCE_COLUMNS",
    "JSON_COLUMNS",
    "MemoryUpdate",
    "RENAMES_FILENAME",
    "STATUS_DONE",
    "STATUS_PLANNED",
    "apply_memory_rewrite",
    "choose_new_building_id",
    "discord_mapping_alerts",
    "legacy_folder_can_exist",
    "legacy_folder_parts",
    "load_rename_record",
    "needs_building_id_repair",
    "plan_memory_rewrite",
    "rename_record_path",
    "repair_unsafe_building_ids",
    "repaired_building_id",
    "replace_exact_strings",
    "replace_exact_strings_nested",
    "rewrite_building_msg_ref",
    "room_replacements",
    "unexpected_failure_alert",
]
