"""ペルソナまるごとパック (persona-pack) の取り込み — 新規作成と合流。

正典: docs/intent/persona_pack.md §5。export.py が書いた ZIP を、取り込み先の
既存ペルソナ (ai 行と memory.db が既にあるもの) へ取り込む。ペルソナ自体の作成
(ai 行・私室・フォルダ) は呼び出し側 (API 層) が既存の作成経路で行う。

mode:

- ``"new"``: 空のペルソナへの合流 + ``items/`` と ``extensions/saiverse/`` の取り込み。
  取り込み先の memory.db に messages が 1 行でもあれば書き込む前に拒否する
  (``extensions/`` の機械的な行の挿入は、空の取り込み先でしか意味が定義できない)。
- ``"merge"``: ``chatlog/`` ``knowledge/`` ``journal/`` ``attachments/`` だけを §5 の
  規則で混ぜる。``items/`` と ``extensions/`` は取り込まず、スキップ件数として
  内訳に載せる (intent §5「合流で混ぜる範囲」)。例外は取り込んだ Chronicle
  ページの chunk_page_edges と、下の「片側だけ commit された取り込みの再開」。

``extensions/saiverse/saiverse_db/`` は export.SAIVERSE_DB_TABLE_OWNERS にある表だけを
取り込み、各行の持ち主の列は ZIP の値によらず取り込み先のペルソナ ID にする
(子表は同じ取り込みで入った親の行を指すものだけ、addon_message_metadata は
取り込み先の memory.db にあるメッセージ宛てのものだけ)。表に無いテーブルは警告して
取り込まない — ZIP の行が別のペルソナ宛てに書き込まれないようにするため。
文字列の主キー (WORLD_STRING_KEY_TABLES) は、取り込み先の
ペルソナ ID と元の値から決まる uuid5 に振り直し、親子の参照の列も同じ写像で
付け替える — 同じ世界に同じパックから二体目を迎えても主キーが衝突しない。
task_book の TASK_ID は例外で、元の値を保ち、取り込み先で別のペルソナの行と
衝突したときだけ同じ uuid5 に振り直す (:meth:`_ImportJob.stage_task_book`)。
task_book は合流でも入るので、常に振り直すと環境 A → B → A の往復で元の行と
照合できなくなるため。振り直した行は元の値を ORIGIN_TASK_ID に残し、照合は
TASK_ID と ORIGIN_TASK_ID のどちらでも行う (振り直された約束を A へ戻すときのため)。

``extensions/saiverse/memory_db/`` は、書き出す側がこのフォルダへ書く表
(export.is_extension_memory_table) だけを受ける。共通フォルダの表 (messages など) と
運ばない表は警告して取り込まない — 共通フォルダ側の検査を通らない行を入れないため。

``items/`` (mode="new") は世界へ登録する: 取り込み先のペルソナの私室 (ai.PRIVATE_ROOM_ID が
指す、作成経路が作った building 行) に ZIP の私室の見た目と設定 (ROOM_COPIED_COLUMNS と
IMAGE_PATH のファイル) を写し、fixture 行をその部屋に入れ、item / item_location 行を
登録する。ファイルは共有メディアフォルダへ復元して、その場所を SAIVERSE_HOME からの
相対パスで指す (manager/items.py が FILE_PATH に書く形)。ITEM_ID は取り込み先の
ペルソナ ID と元の ITEM_ID から決まる UUID (uuid5) で振り直す — 同じパックを同じ
ペルソナへもう一度入れても同じ ID になり、二重に登録されない。

添付の参照 (message metadata の images / documents / media と、mode="new" で
extensions/saiverse/memory_db/ から入る知覚台帳の media 列 — export.PERCEPTION_MEDIA_TABLES)
のうち、ZIP から実体を復元できたものは、元の uri / path を引き継がず復元先のフォルダの種類と実際の
ファイル名から uri / path を組み直し、復元できなかったものは uri と path の両方を None に
する (件数を警告に載せる)。ZIP が書いたホスト側のパスや ``saiverse://image/../x`` のような
uri を取り込み先に残すと、再エクスポートや会話のメディア読み込みが共有メディアフォルダの
外のファイル (本体 DB など) を読みに行き、``saiverse://image/x.png`` のような形の整った uri
でも、取り込み先に同名の別のファイルがあればそれに解決されて別の画像を指すため。
復元先は、同名のファイルが取り込み先にあっても中身 (sha256) が同じときだけ再利用し、
違えば ``<stem>_<n><ext>`` へ避ける (:meth:`_ImportJob.choose_media_dest`)。

知覚台帳の部屋の様子の記帳 (export.ROOM_STATE_COLUMNS — perception_batches.room_state_json と
perception_buffer.metadata の room_state) の束のパッケージの media も同じ規則で書き換え、
復元できなかった項目はパッケージの media から外す (束の media の path は空でない文字列で
なければ束ごと無効になるため)。束を書き換えると指紋 (sai_memory/room_state.snapshot_digest)
が変わるので、差分の記帳の base_digest を書き換えた土台の指紋に付け直す
(:meth:`_ImportJob.room_state_digest_map`)。縮小済みの記帳 (束の代わりに dropped_media を
持つ) は、dropped_media の各 path を、同じ画像の知覚台帳の media の項目が取り込み後に
持つ path へ写す (:meth:`_ImportJob.relocate_dropped_media`)。

profile/ のアイコン・外見画像は、元の値が ``/api/media/images/<名前>`` なら共有メディア
フォルダの image/ へ復元して同じ形の URL で指し、それ以外は user_data/icons へ復元して
絶対パスで指す (:meth:`_ImportJob.stage_profile`)。

JSON 文字列の列 (export.JSON_TEXT_COLUMNS) の ``{"__raw_text__": s}`` は、s が JSON の
object / array として解釈できるとき行ごと取り込まない (警告とスキップ件数)。正規の
書き出しはスカラーか解釈できない文字列しか包まない (format.encode_json_text の契約) ので、
そうした包みは細工か壊れたパックで、包んだまま保存すると上の書き換えを通らない添付の
参照が、保存後の読み手 (json.loads) には object として届くため。

ID の扱いは native_export.py の「移植」(2026-07-16 まはー裁定) を継承する:

- ZIP 内のスレッド ID の前半 (``<persona>:`` の部分) を取り込み先のペルソナ ID に
  置き換える (threads / messages / stelis_threads の親 / Chronicle ページの
  metadata.thread_id)。``resource_id`` が元のペルソナ ID そのものなら取り込み先へ。
- 取り込んだ各 message の ``metadata.transplanted_from`` に元の
  ``{"persona_id", "thread_id"}`` を残す (元と取り込み先が同じ ID なら残さない —
  native_export の移植と同じ)。

照合と採否 (intent §5):

- 追記型 (messages / threads / stelis_threads / 手帳 / clips / purpose_tags /
  task_book) は ID (UUID・uuid 列・TASK_ID) で照合し、無いものだけ挿入する。
  既にあるものは変更しない。task_book は ZIP の行の TASK_ID / ORIGIN_TASK_ID が
  取り込み先のペルソナの行の TASK_ID / ORIGIN_TASK_ID と一致すればスキップし、
  元の TASK_ID が別のペルソナの行にあれば振り直した値 (上の uuid5) で照合する。
- memopedia_pages は id で照合し、本文 (content) が違い、かつ ZIP 側の
  updated_at が新しいときだけ上書きする。上書き前の本文は
  memopedia_page_edit_history に before_* として記録する (rollback_page で戻せる)。
- 合流時の Chronicle (chronicle 分類の trunk でないページ) は、取り込み先に無い
  id の行のうち、取り込み先の被覆と重ならない枝だけを入れる
  (:meth:`_ImportJob.plan_chronicle_merge`)。Lv1 は source_ids が取り込み先の
  全 Lv1 と先に受理した ZIP の Lv1 の source_ids に交わらないもの、上位は子が全員取り込む行か死んだ参照の
  もの。取り込み先に同じ id がある行は上書きしない。親が取り込まれない行は
  root_chronicle の未統合に置き、上位への縁組は bands.reconnect_contained_orphans
  に任せる。取り込んだページの chunk_page_edges も合流する
  (:meth:`_ImportJob.stage_merge_recall_edges`)。memopedia_fragments は合流しない。
  新規取り込みでは全部入れる。

短縮番号 (memopedia_pages.short_id / clips.short_id / コア記憶の metadata.core_id /
Chronicle の metadata.short_id) は DB ごとの連番 (docs/intent/reference_addressing.md
§2.1)。挿入する行の元の番号が取り込み先の取り込み開始時の MAX より大きければ
その番号のまま入れる (取り込み先で一度も使われていない番号なので一意性も
再利用禁止も破らず、本文に埋まった ``m:N`` も指し先が変わらない)。そうでない行は
MAX+1 から振り直し、元の番号を行の metadata に ``original_short_id`` /
``original_core_id`` / ``original_chronicle_short_id`` として残す
(clips は metadata 列を持たないため元の番号を残せない)。

書き込みは memory.db と saiverse.db それぞれ単一トランザクションで行い、全段が
成功したときだけ commit する。途中で失敗したら両方 rollback し、この取り込みが
作ったファイルを消す。ZIP の JSON の解釈と形の検査は書き込みより前に全部済ませる。

片側だけ commit された取り込みの再開: 二つの DB の commit は別々なので、memory.db の
commit 後に saiverse.db の commit が失敗すると片側が残る。mode="new" は memory.db を
commit する前にペルソナフォルダへ印 (export.WORLD_PENDING_MARKER。manifest の
persona_id・format・version、ZIP 全体の sha256 (zip_sha256)、取り込みの識別子
(import_id = zip_sha256 と取り込み先のペルソナ ID から決まる値)、overwrite_profile) を
書き、saiverse.db の commit が成功したら消す。世界側の工程は、同じトランザクションで
saiverse.db の persona_pack_imports に import_id を書く。印が残ったペルソナへ同じ
パック (manifest の persona_id と、zip_sha256 / import_id が一致) を mode="merge" で
取り込むと、合流の工程に加えて mode="new" の世界側の工程 (extensions/saiverse/
saiverse_db/・items/・印に記録した overwrite_profile による profile/) を行い、
saiverse.db の commit が成功したら印を消す。persona_pack_imports に import_id が既に
あれば (世界側の commit 後、印を消す前に止まった) 世界側の工程はやり直さず印だけ
消す — 整数の主キーを振り直す表は INSERT OR IGNORE では二重登録を防げないため。
印はファイルなので memory.db の COMMIT と一緒に巻き戻らず、印を書いた後、memory.db の
COMMIT 前に止まると印だけが残る。これを見分けるため、mode="new" の memory.db 側の
工程も同じトランザクションで memory.db の persona_pack_imports (_MEMORY_IMPORTS_TABLE)
に import_id を書く。再開の合流は memory.db にその記録が無ければ、memory.db 側の
mode="new" の工程 (extensions/saiverse/memory_db/・extensions/saiverse/persona_dir/・
合流の規則によらない Chronicle の取り込み・知覚台帳の添付) も行う (memory_new)。
persona_id は一致するが zip_sha256 / import_id が違う ZIP では再開せず、印を残して
警告する。task_book は合流でも入る。memory.db 側とファイルは照合 (UUID・
INSERT OR IGNORE・同じ中身のファイルの再利用) で二重にならない。

embeddings は運ばれてこない。取り込んだ message のうち content が空白だけでない
ものは、同じトランザクションで空のベクトル行 (``storage.DEFERRED_VECTOR``) を
持って入る — 起動時の検査と ``SAIMemoryAdapter.reembed_stale_messages`` が拾う
(import_chatlog.py と同じ形)。content が空のメッセージには付けない (埋め込む本文が
無いので、再埋め込みの走査対象に入らなくても失うものが無い)。
Memopedia ページ・Chronicle・Fragment は「埋め込みの無い行」として
``SessionLifecycle.ensure_recall_embeddings`` の対象に自然に入る。上書きした
ページは memopedia_embeddings の行を消して、同じ経路で作り直させる。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sai_memory.presented_reduction import DROPPED_MEDIA_FIELD
from sai_memory.room_state import snapshot_digest

from .export import (
    API_MEDIA_IMAGES_PREFIX,
    _ATTACHMENT_KEYS,
    _DAY_REPORTS_DIR,
    _MEDIA_DIR_NAMES,
    _MEDIA_URI_DIRS,
    _MEMORY_DB_FILES,
    _SUMMARY_SUFFIX,
    EPOCH_COLUMNS,
    JSON_TEXT_COLUMNS,
    PACK_IMPORTS_MEMORY_TABLE,
    PERCEPTION_MEDIA_TABLES,
    ROOM_STATE_COLUMNS,
    SAIVERSE_DB_TABLE_OWNERS,
    WORLD_PENDING_MARKER,
    _attachment_key,
    _attachment_refs,
    _media_entry_refs,
    is_extension_memory_table,
    parse_media_column,
    parse_room_state_column,
    room_state_media_refs,
    room_state_records,
)
from .format import FORMAT_NAME, MANIFEST_NAME, RAW_TEXT_MARKER, decode_value, iso_to_epoch

LOGGER = logging.getLogger(__name__)

MODES = ("new", "merge")

# profile/ の上書きで書き換えない ai の列 (識別子・世界の中の位置・取り込み先の
# 運用状態)。AVATAR_IMAGE / APPEARANCE_IMAGE_PATH は値ではなくファイルを復元して
# 取り込み先のパスを書く (_PROFILE_FILE_COLUMNS)。
PROFILE_PROTECTED_COLUMNS: Tuple[str, ...] = (
    "AIID",                    # 識別子
    "HOME_CITYID",             # 取り込み先の街との結び付き
    "PRIVATE_ROOM_ID",         # 取り込み先の私室 (building 行)
    "PERSONA_ROLE",            # 'ruler' は Region との結び付き (世界の中の位置)
    "IS_DISPATCHED",           # 街間移動の運用状態 (凍結済み)
    "METABOLISM_ANCHORS",      # 旧 anchor の控え (運ばない session_anchor の旧形)
    "LAST_KNOWN_VERSION",      # 取り込み先で動いた版 (アップグレード処理の判定)
    "AUTO_COUNT",              # 自律発話の回数 (運用状態)
    "LAST_AUTO_PROMPT_TIMES",  # 自律発話の時刻 (運用状態)
)
_PROFILE_FILE_COLUMNS = ("AVATAR_IMAGE", "APPEARANCE_IMAGE_PATH")

# items/private_room.json の building 行のうち、取り込み先の私室へ写す列 (見た目と設定)。
# 写さない列: CITYID / BUILDINGID (取り込み先の私室の識別子と街)、MAP_X / MAP_Y /
# REGION_ID / FACILITY_ROLES (世界の中の位置と役割)、PHYSICAL_VESSEL_ID (取り込み先の
# 機体との結び付き)、EXTRA_PROMPT_FILES (指すファイルを運ばない)。IMAGE_PATH は値では
# なくファイルを復元して取り込み先の場所を書く。BUILDINGNAME は同じ街に同名の建物が
# 無いときだけ写す (building の UNIQUE(CITYID, BUILDINGNAME))。
ROOM_COPIED_COLUMNS: Tuple[str, ...] = (
    "BUILDINGNAME",
    "CAPACITY",
    "SYSTEM_INSTRUCTION",
    "ENTRY_PROMPT",
    "AUTO_PROMPT",
    "DESCRIPTION",
    "AUTO_INTERVAL_SEC",
    "ITEM_DISPLAY_LIMIT",
    "SHOW_MOVEMENT_NOTICES",
)

# items/ で振り直す ITEM_ID / FIXTURE_ID と WORLD_STRING_KEY_TABLES の主キーの
# uuid5 の名前空間 (値は固定。変えると、
# 変える前に取り込んだ行を同じパックの再取り込みで照合できなくなる)
_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, f"saiverse://{FORMAT_NAME}")

# 取り込みで振り直す saiverse.db の文字列の主キーと、同じ写像で付け替える参照の列。
# {表: (主キーの列, ((参照の列, 参照先の表), ...))}。値は _ImportJob.world_key
# (= derived_id(表, 元の値)。取り込み先のペルソナ ID と元の値から決まる uuid5) に
# 置き換える。同じペルソナへの再実行では同じ値になり二重に登録されず、同じ世界に
# 同じパックから別のペルソナを迎えても主キーが衝突しない。
# 表と列は export.SAIVERSE_DB_TABLE_OWNERS の表のうち、主キーが文字列の
# ものと database/models.py の定義 (ForeignKey と、参照先を持つ文字列の列) から決めた
# (テストで models.py と突き合わせている)。整数の主キーは stage_ext_saiverse が
# 取り込み先の連番で振り直す。persona_task.note_id は死んだ列 (models.py) なので
# 付け替えない。task_book はここに載せない — TASK_ID は衝突したときだけ振り直す
# (stage_task_book。task_book を参照する表が無いので、元の値を保っても付け替えは要らない)。
WORLD_STRING_KEY_TABLES: Dict[str, Tuple[str, Tuple[Tuple[str, str], ...]]] = {
    "persona_task": ("id", (("active_step_id", "persona_task_step"), ("track_id", "action_track"))),
    "persona_task_step": ("id", (("task_id", "persona_task"),)),
    "persona_task_history": ("id", (("task_id", "persona_task"), ("step_id", "persona_task_step"))),
    "episodes": ("EPISODE_ID", ()),
    "episode_inheritance": (
        "EDGE_ID", (("CHILD_EPISODE_ID", "episodes"), ("PARENT_EPISODE_ID", "episodes")),
    ),
    "action_track": ("track_id", ()),
    "track_local_log": ("log_id", (("track_id", "action_track"),)),
    "meta_judgment_log": ("judgment_id", (("track_at_judgment_id", "action_track"),)),
}

# 世界側の工程の適用済み記録の表。正典は database/models.py の PersonaPackImport
# (起動時のマイグレーションが作る)。ここはそれより古い DB に備えた自衛で、
# 列を変えるときは models.py と揃える。
_IMPORTS_TABLE = "persona_pack_imports"
_IMPORTS_TABLE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_IMPORTS_TABLE} ("
    "IMPORT_ID VARCHAR(64) NOT NULL PRIMARY KEY, "
    "PERSONA_ID VARCHAR(255) NOT NULL, "
    "APPLIED_AT INTEGER NOT NULL)"
)

# memory.db 側の工程の適用済み記録の表 (export.PACK_IMPORTS_MEMORY_TABLE)。
# 新規作成の memory.db 側の工程 (_ImportJob.memory_new) を行ったことを、memory.db の
# 同じトランザクションで IMPORT_ID (_import_id) として書く。印 (WORLD_PENDING_MARKER)
# はファイルなので memory.db の COMMIT と一緒に巻き戻らない — 印を書いた後、
# memory.db の COMMIT 前に止まると印だけが残る。印による再開
# (_ImportJob.check_pending_marker) はこの表で memory.db 側が commit 済みかを見分ける。
# sai_memory の init が作る表ではなく、この機能だけが読み書きする表なので、
# 定義の正典はここ。取り込み先の運用状態なので書き出さない
# (export.NOT_CARRIED_MEMORY_TABLES)。
_MEMORY_IMPORTS_TABLE = PACK_IMPORTS_MEMORY_TABLE
_MEMORY_IMPORTS_TABLE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_MEMORY_IMPORTS_TABLE} ("
    "IMPORT_ID TEXT NOT NULL PRIMARY KEY, "
    "APPLIED_AT INTEGER NOT NULL)"
)

_EDIT_SOURCE = "persona_pack_import"
_CATEGORY_CORE = "core"           # sai_memory/core_memory.py の CATEGORY_CORE
_CATEGORY_CHRONICLE = "chronicle"  # sai_memory/arasuji/storage.py の CATEGORY_CHRONICLE
_ROOT_CHRONICLE = "root_chronicle"  # sai_memory/arasuji/storage.py の ROOT_CHRONICLE_ID
_CHUNK = 500

# ZIP を展開したときの合計サイズ (infolist の file_size の和) の上限。超えるパックは
# 書き込み前に拒否する (ZIP 爆弾の頭打ち)。取り込みは JSON を丸ごとメモリに読むので、
# 圧縮の効いた小さな ZIP が展開で巨大になるのを入口で止める。値は API のアップロード
# 上限 (api/routes/people/persona_pack.py の PERSONA_PACK_IMPORT_MAX_BYTES = 2 GiB) の 8 倍。
MAX_UNCOMPRESSED_BYTES = 8 * 2 * 1024 * 1024 * 1024


@dataclass
class ImportCounts:
    """1 カテゴリの内訳。"""

    added: int = 0
    updated: int = 0
    skipped: int = 0


@dataclass
class PersonaPackImportResult:
    """取り込みの結果 (intent §5「結果の内訳」)。

    ``counts`` のキー: ``threads`` ``stelis_threads`` ``messages`` ``attachments``
    ``pages`` ``chronicle_pages`` ``activities`` ``memos`` ``clips``
    ``purpose_tags`` ``task_book`` ``day_reports`` ``profile`` ``items`` と、
    ``extensions/memory_db/<table>`` ``extensions/saiverse_db/<table>``
    ``extensions/persona_dir``。ZIP に無いカテゴリは載らない。
    """

    mode: str
    source_persona_id: str
    target_persona_id: str
    counts: Dict[str, ImportCounts] = field(default_factory=dict)
    # 振り直した短縮番号の件数: "pages.short_id" / "pages.core_id" /
    # "pages.chronicle_short_id" / "clips.short_id"
    renumbered: Dict[str, int] = field(default_factory=dict)
    # 空のベクトル行を付けて入れた message の件数 (再生成待ち)
    embeddings_deferred: int = 0
    # profile の上書きで書き換えた ai の列
    profile_columns_overwritten: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def count(self, key: str) -> ImportCounts:
        return self.counts.setdefault(key, ImportCounts())


# ---------------------------------------------------------------------------
# ZIP の読み取りと値の戻し方
# ---------------------------------------------------------------------------

def _safe_relpath(rel: str, where: str) -> PurePosixPath:
    """ZIP 由来の相対パスを検査する (絶対パス・``..``・ドライブ指定を拒否)。"""
    p = PurePosixPath(rel)
    if (
        not rel
        or "\\" in rel
        or ":" in rel
        or p.is_absolute()
        or any(part in ("", ".", "..") for part in p.parts)
    ):
        raise ValueError(f"persona pack: unsafe path in {where}: {rel!r}")
    return p


class _PackReader:
    def __init__(self, zf: zipfile.ZipFile) -> None:
        self.zf = zf
        self.names: Set[str] = {n for n in zf.namelist() if not n.endswith("/")}

    def read(self, name: str) -> bytes:
        return self.zf.read(name)

    def load_json(self, name: str) -> Any:
        try:
            return json.loads(self.zf.read(name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"persona pack: {name} is not valid JSON: {exc}") from exc

    def rows(self, name: str, required: Sequence[str] = ()) -> List[Dict[str, Any]]:
        """JSON 配列 (要素は dict) を読む。無ければ空。必須キーの欠けは ValueError。"""
        if name not in self.names:
            return []
        data = self.load_json(name)
        if not isinstance(data, list):
            raise ValueError(f"persona pack: {name} must be a JSON array")
        for i, row in enumerate(data):
            if not isinstance(row, dict):
                raise ValueError(f"persona pack: {name}[{i}] must be an object")
            for key in required:
                if row.get(key) is None or row.get(key) == "":
                    raise ValueError(f"persona pack: {name}[{i}] missing {key!r}")
        return data

    def files_under(self, prefix: str) -> List[str]:
        return sorted(n for n in self.names if n.startswith(prefix))


def check_uncompressed_size(zf: zipfile.ZipFile) -> None:
    """ZIP の展開後の合計サイズが MAX_UNCOMPRESSED_BYTES を超えたら ValueError (書き込み前の検査)。"""
    limit = MAX_UNCOMPRESSED_BYTES
    total = sum(info.file_size for info in zf.infolist())
    if total > limit:
        raise ValueError(
            f"persona pack: the archive expands to {total} bytes, over the limit of "
            f"{limit} bytes; refusing to import it"
        )


def _decode_json_text(value: Any) -> Tuple[bool, Any]:
    """export の encode_json_text の逆の前半。

    返り値は (元が ``__raw_text__`` に包まれた文字列 — JSON の object / array として
    展開されなかった文字列 — だったか, 値)。
    """
    if isinstance(value, dict) and len(value) == 1 and isinstance(value.get(RAW_TEXT_MARKER), str):
        return True, value[RAW_TEXT_MARKER]
    return False, decode_value(value)


def _raw_text_is_structured(value: Any) -> bool:
    """``{"__raw_text__": s}`` の s が JSON の object / array として解釈できるか。

    format.encode_json_text の契約 (包むのはスカラーか解釈できない文字列だけ) に反する
    包み。正規の書き出しでは生まれない。入れ子が深すぎて解釈が止まる文字列も
    含める (取り込み先の読み手がどう扱うかを決められないため、拒否する側に倒す)。
    """
    if not (isinstance(value, dict) and len(value) == 1 and isinstance(value.get(RAW_TEXT_MARKER), str)):
        return False
    try:
        parsed = json.loads(value[RAW_TEXT_MARKER])
    except RecursionError:
        return True
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, (dict, list))


def _refuse_structured_raw_text(
    rows: List[Dict[str, Any]], table: str, file_name: str, pack: "_PackData"
) -> List[Dict[str, Any]]:
    """JSON_TEXT_COLUMNS の列が :func:`_raw_text_is_structured` の行を除いて返す。

    除いた行は pack.refused_raw_text に (内訳のキー, 警告文) として記録する
    (_ImportJob.report_refused_raw_text が警告とスキップ件数にする)。
    """
    columns = JSON_TEXT_COLUMNS.get(table, ())
    kept: List[Dict[str, Any]] = []
    for i, row in enumerate(rows):
        bad = [c for c in columns if _raw_text_is_structured(row.get(c))]
        if not bad:
            kept.append(row)
            continue
        if table == "memopedia_pages":
            is_chronicle = row.get("category") == _CATEGORY_CHRONICLE and not row.get("is_trunk")
            key = "chronicle_pages" if is_chronicle else "pages"
        else:
            key = table
        pack.refused_raw_text.append((
            key,
            f"{file_name}[{i}] (id={row.get('id')!r}): {', '.join(bad)} wraps a JSON object or "
            f"array in {RAW_TEXT_MARKER!r}; a persona pack only wraps scalar or unparsable "
            "text there, so the row was not imported",
        ))
    return kept


def _encode_json_text(is_raw: bool, value: Any) -> Any:
    if is_raw:
        return value
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _decode_row(row: Dict[str, Any], table: Optional[str] = None) -> Dict[str, Any]:
    """export の _encode_row の逆。epoch 列・JSON 文字列列・bytes を元の値へ戻す。"""
    epoch_cols = EPOCH_COLUMNS.get(table or "", ())
    json_cols = JSON_TEXT_COLUMNS.get(table or "", ())
    out: Dict[str, Any] = {}
    for key, value in row.items():
        if key in epoch_cols:
            out[key] = iso_to_epoch(value)
        elif key in json_cols:
            out[key] = _encode_json_text(*_decode_json_text(value))
        else:
            out[key] = decode_value(value)
    return out


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _epoch_key(value: Any) -> Tuple[int, float]:
    """created_at の並びの鍵。NULL は SQLite と同じく先頭側。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return (0, 0.0)
    return (1, float(value))


def _plan_numbers(originals: Sequence[Optional[int]], start_max: int) -> List[Optional[int]]:
    """挿入する行の短縮番号を決める (モジュール docstring の規則)。

    元の番号が ``start_max`` より大きく、挿入する行の中で重複していなければ
    そのまま使う。それ以外は MAX+1 から振る (MAX は取り込み開始時の値と、
    そのまま使う番号の大きい方)。元が None の行は None のまま。
    """
    kept = {o for o in originals if o is not None and o > start_max}
    top = max([start_max, *kept])
    result: List[Optional[int]] = list(originals)
    used: Set[int] = set()
    order = sorted(
        range(len(originals)),
        key=lambda i: (originals[i] is None, originals[i] or 0, i),
    )
    for i in order:
        o = originals[i]
        if o is None:
            continue
        if o > start_max and o not in used:
            used.add(o)
            result[i] = o
            continue
        top += 1
        result[i] = top
    return result


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _import_id(zip_sha256: str, target_persona_id: str) -> str:
    """取り込みの識別子: ZIP 全体の sha256 と取り込み先のペルソナ ID から決まる sha256。

    印 (WORLD_PENDING_MARKER) の import_id と persona_pack_imports.IMPORT_ID に使う。
    """
    return hashlib.sha256(f"{zip_sha256}\n{target_persona_id}".encode("utf-8")).hexdigest()


# saiverse://<種類>/ の接頭辞を共有メディアフォルダ名から引く (_MEDIA_URI_DIRS の逆)
_URI_PREFIX_BY_DIR: Dict[str, str] = {dirname: prefix for prefix, dirname in _MEDIA_URI_DIRS}


def _media_dir_for(original_path: Any, item_type: Any) -> str:
    """items/ のファイルを復元する共有メディアフォルダの名前。

    元の FILE_PATH の中に共有メディアフォルダ名があればそれ、無ければ TYPE
    (picture / audio / video) から決め、どちらでもなければ documents。
    """
    if isinstance(original_path, str) and original_path.strip():
        parts = original_path.strip().replace("\\", "/").split("/")
        for part in reversed(parts[:-1]):
            if part in _MEDIA_DIR_NAMES:
                return part
    kind = str(item_type or "").strip().lower()
    return {"picture": "image", "audio": "audio", "video": "video"}.get(kind, "documents")


# ---------------------------------------------------------------------------
# 読み込んだ ZIP の中身 (書き込み前に全部検査する)
# ---------------------------------------------------------------------------

@dataclass
class _PackData:
    source_id: str
    contents: Set[str]
    version: str = ""
    threads: List[Dict[str, Any]] = field(default_factory=list)
    stelis_threads: List[Dict[str, Any]] = field(default_factory=list)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    pages: List[Dict[str, Any]] = field(default_factory=list)
    activities: List[Dict[str, Any]] = field(default_factory=list)
    memos: List[Dict[str, Any]] = field(default_factory=list)
    clips: List[Dict[str, Any]] = field(default_factory=list)
    purpose_tags: List[Dict[str, Any]] = field(default_factory=list)
    task_book: List[Dict[str, Any]] = field(default_factory=list)
    day_reports: List[Tuple[str, PurePosixPath]] = field(default_factory=list)
    attachment_index: List[Dict[str, Any]] = field(default_factory=list)
    profile: Optional[Dict[str, Any]] = None
    # items/private_room.json と items/items.json (形は _load_items で検査済み)
    private_room: Optional[Dict[str, Any]] = None
    items: Optional[Dict[str, Any]] = None
    ext_memory_tables: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    # extensions/saiverse/memory_db/ にあったが、書き出す側がこのフォルダへ書かない表
    # (export.is_extension_memory_table が False) の行数。取り込まない
    ext_memory_refused: Dict[str, int] = field(default_factory=dict)
    ext_saiverse_tables: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    ext_persona_files: List[Tuple[str, PurePosixPath]] = field(default_factory=list)
    # JSON_TEXT_COLUMNS の列の __raw_text__ が object / array を包んでいたため除いた行:
    # (内訳のキー, 警告文)。_refuse_structured_raw_text が記録する
    refused_raw_text: List[Tuple[str, str]] = field(default_factory=list)


_EXT = "extensions/saiverse/"
_EXT_MEMORY = _EXT + "memory_db/"
_EXT_SAIVERSE = _EXT + "saiverse_db/"
_EXT_PERSONA = _EXT + "persona_dir/"
_TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _load_pack(reader: _PackReader) -> _PackData:
    """ZIP を読み、取り込みに使う全 JSON を解釈して形を検査する (書き込みはしない)。"""
    if MANIFEST_NAME not in reader.names:
        raise ValueError("persona pack: manifest.json is missing")
    manifest = reader.load_json(MANIFEST_NAME)
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_NAME:
        raise ValueError(f"persona pack: unsupported format {manifest.get('format') if isinstance(manifest, dict) else None!r}")
    version = str(manifest.get("version") or "")
    if version.split(".", 1)[0] != "1":
        raise ValueError(f"persona pack: unsupported version {version!r}")
    source_id = manifest.get("persona_id")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("persona pack: manifest.persona_id is missing")
    contents = manifest.get("contents")
    if not isinstance(contents, list) or not all(isinstance(c, str) for c in contents):
        raise ValueError("persona pack: manifest.contents must be a list of folder names")

    pack = _PackData(source_id=source_id, contents=set(contents), version=version)
    has = pack.contents.__contains__

    if has("chatlog"):
        pack.threads = reader.rows("chatlog/threads.json", ("id",))
        pack.stelis_threads = reader.rows("chatlog/stelis_threads.json", ("thread_id",))
        pack.messages = _refuse_structured_raw_text(
            reader.rows("chatlog/messages.json", ("id", "thread_id")),
            "messages", "chatlog/messages.json", pack,
        )
    if has("knowledge"):
        pack.pages = _refuse_structured_raw_text(
            reader.rows("knowledge/pages.json", ("id", "category")),
            "memopedia_pages", "knowledge/pages.json", pack,
        )
    if has("journal"):
        pack.activities = reader.rows("journal/activities.json", ("id",))
        pack.memos = reader.rows("journal/memos.json", ("activity_id",))
        pack.clips = reader.rows("journal/clips.json", ("clip_id",))
        pack.purpose_tags = reader.rows("journal/purpose_tags.json", ("tag_id",))
        pack.task_book = reader.rows("journal/task_book.json", ("TASK_ID",))
        prefix = f"journal/{_DAY_REPORTS_DIR}/"
        pack.day_reports = [
            (name, _safe_relpath(name[len(prefix):], name)) for name in reader.files_under(prefix)
        ]
    if has("attachments") and "attachments/index.json" in reader.names:
        index = reader.load_json("attachments/index.json")
        if not isinstance(index, list) or not all(isinstance(e, dict) for e in index):
            raise ValueError("persona pack: attachments/index.json must be a list of objects")
        for i, entry in enumerate(index):
            path = entry.get("path")
            if path is None:
                continue
            if path not in reader.names:
                raise ValueError(f"persona pack: attachments/index.json[{i}] points to a missing file {path!r}")
            # export が書く形は attachments/<種類>/<ファイル名> だけ (plan_attachments が分解する)
            parts = _safe_relpath(path, "attachments/index.json").parts
            if len(parts) != 3 or parts[0] != "attachments":
                raise ValueError(f"persona pack: unexpected attachment path {path!r}")
        pack.attachment_index = index
    if has("profile") and "profile/profile.json" in reader.names:
        profile = reader.load_json("profile/profile.json")
        if not isinstance(profile, dict) or not isinstance(profile.get("ai"), dict):
            raise ValueError("persona pack: profile/profile.json must have an 'ai' object")
        pack.profile = profile
    if has("items"):
        _load_items(reader, pack)
    if has("extensions/saiverse"):
        pack.ext_persona_files = [
            (name, _safe_relpath(name[len(_EXT_PERSONA):], name))
            for name in reader.files_under(_EXT_PERSONA)
        ]
        for prefix, target in ((_EXT_MEMORY, pack.ext_memory_tables),
                               (_EXT_SAIVERSE, pack.ext_saiverse_tables)):
            for name in reader.files_under(prefix):
                table = name[len(prefix):]
                if not table.endswith(".json") or not _TABLE_NAME_RE.match(table[:-5]):
                    raise ValueError(f"persona pack: unexpected extensions file {name!r}")
                # 合流でも形を検査する (不正なパックは書き込み前に拒否する)。合流は
                # chunk_page_edges をここから取り込み、残りの表も件数を内訳に載せる
                rows = reader.rows(name)
                if target is pack.ext_memory_tables and not is_extension_memory_table(table[:-5]):
                    pack.ext_memory_refused[table[:-5]] = len(rows)
                    continue
                target[table[:-5]] = rows
    return pack


def _check_zip_file_ref(reader: _PackReader, value: Any, where: str) -> None:
    """items/ の JSON が指す ZIP 内のファイルを検査する (None か、ZIP にある items/ 配下の安全な名前)。"""
    if value is None:
        return
    if not isinstance(value, str) or value not in reader.names or not value.startswith("items/"):
        raise ValueError(f"persona pack: {where} points to a missing file {value!r}")
    _safe_relpath(value[len("items/"):], where)


def _load_items(reader: _PackReader, pack: _PackData) -> None:
    """items/private_room.json と items/items.json を読み、形を検査する (書き込みはしない)。"""
    name = "items/private_room.json"
    if name in reader.names:
        room = reader.load_json(name)
        if (
            not isinstance(room, dict)
            or not isinstance(room.get("building"), dict)
            or not isinstance(room.get("fixtures", []), list)
            or not all(isinstance(f, dict) for f in room.get("fixtures", []))
            or not isinstance(room.get("files") or {}, dict)
        ):
            raise ValueError(f"persona pack: {name} must have a 'building' object and a 'fixtures' list")
        files = room.get("files") or {}
        _check_zip_file_ref(reader, files.get("IMAGE_PATH"), f"{name} files.IMAGE_PATH")
        fixture_files = files.get("fixtures") or {}
        if not isinstance(fixture_files, dict):
            raise ValueError(f"persona pack: {name} files.fixtures must be an object")
        for key, value in fixture_files.items():
            _check_zip_file_ref(reader, value, f"{name} files.fixtures[{key!r}]")
        for i, fx in enumerate(room.get("fixtures", [])):
            if not isinstance(fx.get("FIXTURE_ID"), str) or not fx.get("FIXTURE_ID"):
                raise ValueError(f"persona pack: {name} fixtures[{i}] missing 'FIXTURE_ID'")
        pack.private_room = room
    name = "items/items.json"
    if name in reader.names:
        doc = reader.load_json(name)
        if (
            not isinstance(doc, dict)
            or not isinstance(doc.get("items"), list)
            or not isinstance(doc.get("locations"), list)
            or not isinstance(doc.get("files") or {}, dict)
        ):
            raise ValueError(f"persona pack: {name} must have 'items' and 'locations' lists")
        for i, item in enumerate(doc["items"]):
            if not isinstance(item, dict) or not isinstance(item.get("ITEM_ID"), str) or not item["ITEM_ID"]:
                raise ValueError(f"persona pack: {name} items[{i}] missing 'ITEM_ID'")
        for i, loc in enumerate(doc["locations"]):
            if not isinstance(loc, dict) or not all(
                isinstance(loc.get(k), str) and loc.get(k) for k in ("ITEM_ID", "OWNER_KIND", "OWNER_ID")
            ):
                raise ValueError(
                    f"persona pack: {name} locations[{i}] needs ITEM_ID, OWNER_KIND and OWNER_ID"
                )
        for key, value in (doc.get("files") or {}).items():
            _check_zip_file_ref(reader, value, f"{name} files[{key!r}]")
        pack.items = doc


# ---------------------------------------------------------------------------
# 取り込み本体
# ---------------------------------------------------------------------------

@dataclass
class _Relocation:
    """添付 1 件の取り込み先 (共有メディアフォルダの中のファイル)。"""

    dest: Path


@dataclass
class _FilePlan:
    zip_name: str
    dest: Path


@dataclass
class _ChroniclePlan:
    """合流で取り込む Chronicle ページの判定 (:meth:`_ImportJob.plan_chronicle_merge`)。"""

    # 取り込む ZIP 側の chronicle ページの id
    accepted: Set[str] = field(default_factory=set)
    # accepted のうち、parent_id を root_chronicle に・is_consolidated を 0 にする id
    to_root: Set[str] = field(default_factory=set)


def validate_pack(zip_path: Path) -> None:
    """パック全体を、どこにも書き込まずに検査する (取り込み先を問わない部分)。

    import_persona_pack が書き込みの前に行う ZIP の検査 (check_uncompressed_size と
    _load_pack — 全 JSON の解釈と形の検査、ZIP 内のファイル参照の検査) と同じもの。
    新規取り込みの API がペルソナを作る前に呼び、作成後に形の不正で失敗するのを防ぐ。
    取り込み先に依存する検査 (ai 行・memory.db・mode="new" の空の確認) は含まない。

    Raises:
        zipfile.BadZipFile: ZIP として読めない。
        ValueError: 形が不正・展開後の合計サイズが MAX_UNCOMPRESSED_BYTES を超える。
    """
    with zipfile.ZipFile(Path(zip_path)) as zf:
        check_uncompressed_size(zf)
        _load_pack(_PackReader(zf))


def import_persona_pack(
    zip_path: Path,
    *,
    mode: str,
    target_persona_id: str,
    saiverse_db_path: Path,
    saiverse_home: Path,
    overwrite_profile: bool = False,
) -> PersonaPackImportResult:
    """ペルソナまるごとパックを既存のペルソナへ取り込む。

    Args:
        zip_path: export_persona_pack が書いた ZIP。
        mode: ``"new"`` (空のペルソナへ全部) か ``"merge"`` (既存のペルソナへ合流)。
        target_persona_id: 取り込み先のペルソナ ID。ai 行と
            ``<saiverse_home>/personas/<id>/memory.db`` が既にあること。
        saiverse_db_path: 取り込み先の本体 DB (saiverse.db)。
        saiverse_home: 取り込み先の SAIVERSE_HOME 相当のルート。
        overwrite_profile: True のときだけ ai 行を profile/profile.json の値で
            上書きする (PROFILE_PROTECTED_COLUMNS を除く)。

    Raises:
        FileNotFoundError: saiverse.db / memory.db が無い。
        ValueError: mode が不正・ZIP の形が不正・展開後の合計サイズが
            MAX_UNCOMPRESSED_BYTES を超える・ai 行が無い・mode="new" で
            取り込み先に messages がある。いずれも書き込み前に投げる。
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    zip_path = Path(zip_path)
    saiverse_db_path = Path(saiverse_db_path)
    home = Path(saiverse_home)
    persona_dir = home / "personas" / target_persona_id
    memory_db_path = persona_dir / "memory.db"
    if not saiverse_db_path.is_file():
        raise FileNotFoundError(f"saiverse.db not found: {saiverse_db_path}")
    if not memory_db_path.is_file():
        raise FileNotFoundError(f"memory.db not found for persona {target_persona_id}: {memory_db_path}")

    zip_sha256 = _sha256_file(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        check_uncompressed_size(zf)
        reader = _PackReader(zf)
        pack = _load_pack(reader)
        result = PersonaPackImportResult(
            mode=mode, source_persona_id=pack.source_id, target_persona_id=target_persona_id,
        )
        job = _ImportJob(
            reader=reader, pack=pack, result=result, mode=mode,
            target=target_persona_id, home=home, persona_dir=persona_dir,
            overwrite_profile=overwrite_profile, zip_sha256=zip_sha256,
        )
        job.run(memory_db_path, saiverse_db_path)

    LOGGER.info(
        "[persona_pack] imported %s (source=%s) into %s mode=%s counts=%s renumbered=%s "
        "deferred_embeddings=%d warnings=%d",
        zip_path, result.source_persona_id, target_persona_id, mode,
        {k: (v.added, v.updated, v.skipped) for k, v in result.counts.items()},
        result.renumbered, result.embeddings_deferred, len(result.warnings),
    )
    return result


def _open_memory_db(path: Path) -> sqlite3.Connection:
    """取り込み先の memory.db を開き、各モジュールの init でスキーマを揃える。

    init はそれぞれ冪等で、本体の SAIMemoryAdapter が起動時に通すものと同じ。
    取り込みの書き込みはこの後の単一トランザクションで行う。
    """
    from sai_memory.clips import init_clips_tables
    from sai_memory.memopedia.storage import init_memopedia_tables
    from sai_memory.memory.storage import init_db
    from sai_memory.purpose_tags import init_purpose_tags_tables

    conn = init_db(str(path))
    try:
        init_memopedia_tables(conn)
        init_clips_tables(conn)
        init_purpose_tags_tables(conn)
        if conn.in_transaction:
            conn.commit()
        conn.isolation_level = None
        conn.execute("PRAGMA busy_timeout = 30000")
    except BaseException:
        conn.close()
        raise
    return conn


class _ImportJob:
    def __init__(
        self,
        *,
        reader: _PackReader,
        pack: _PackData,
        result: PersonaPackImportResult,
        mode: str,
        target: str,
        home: Path,
        persona_dir: Path,
        overwrite_profile: bool,
        zip_sha256: str,
    ) -> None:
        self.reader = reader
        self.pack = pack
        self.result = result
        self.mode = mode
        self.target = target
        self.source = pack.source_id
        self.home = home
        self.persona_dir = persona_dir
        self.overwrite_profile = overwrite_profile
        self.thread_map: Dict[str, str] = {}
        self.relocations: Dict[str, _Relocation] = {}
        # 知覚台帳の media の項目の元の path → 取り込み後のその項目の path の一覧
        # (plan_attachments が作る)。縮小済みの部屋の記帳の dropped_media の付け替えに使う
        # (relocate_dropped_media)
        self.path_relocations: Dict[str, List[str]] = {}
        # relocate_media_entries が uri / path を None にした添付参照の鍵 (report_invalidated_media)
        self.invalidated_media_keys: Set[str] = set()
        self.file_plans: List[_FilePlan] = []
        self.reserved: Set[str] = set()
        self.created_files: List[Path] = []
        self.created_dirs: List[Path] = []
        self._columns_cache: Dict[Tuple[int, str], List[str]] = {}
        self._dropped_warned: Set[Tuple[str, str]] = set()
        # 取り込んだ chronicle ページの id (合流の chunk_page_edges の照合に使う)
        self.imported_chronicle_ids: Set[str] = set()
        # 合流で取り込んだ extensions/saiverse/memory_db/ の表 (count_merge_skips が数えない)
        self._merged_ext_tables: Set[str] = set()
        # ZIP の messages の id (stage_chatlog の後は全部が取り込み先の memory.db にある)。
        # addon_message_metadata の取り込みをこのメッセージ宛ての行に限る
        self.message_ids: Set[str] = set()
        # 片側だけ commit された mode="new" の取り込みを、この合流で再開するか
        # (モジュール docstring)。True なら世界側の mode="new" の工程も行う
        self.resume_world = False
        # 印が残った mode="new" の取り込みの memory.db 側が commit されていない
        # (_MEMORY_IMPORTS_TABLE に IMPORT_ID が無い) とき True。この合流で
        # memory.db 側の mode="new" の工程も行う
        self.resume_memory = False
        # 取り込みが成功したら印を消すか (mode="new"、再開、両側が適用済みの印)
        self.clear_marker = mode == "new"
        self.marker_path = persona_dir / WORLD_PENDING_MARKER
        self.zip_sha256 = zip_sha256
        self.import_id = _import_id(zip_sha256, target)

    @property
    def world_new(self) -> bool:
        """世界側 (saiverse.db の extensions/・items/) の mode="new" の工程を行うか。"""
        return self.mode == "new" or self.resume_world

    @property
    def memory_new(self) -> bool:
        """memory.db 側 (extensions/saiverse/memory_db/・ペルソナフォルダのファイル・
        Chronicle の全件取り込み・知覚台帳の添付) の mode="new" の工程を行うか。"""
        return self.mode == "new" or self.resume_memory

    # --- 共通 ---------------------------------------------------------------

    def warn(self, message: str) -> None:
        self.result.warnings.append(message)
        LOGGER.warning("[persona_pack] %s", message)

    def columns(self, conn: sqlite3.Connection, table: str) -> List[str]:
        key = (id(conn), table)
        if key not in self._columns_cache:
            rows = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
            self._columns_cache[key] = [r[1] for r in rows]
        return self._columns_cache[key]

    def table_exists(self, conn: sqlite3.Connection, table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None

    def insert(
        self,
        conn: sqlite3.Connection,
        table: str,
        row: Dict[str, Any],
        *,
        rowid: Optional[int] = None,
        or_ignore: bool = False,
    ) -> sqlite3.Cursor:
        """取り込み先の表にある列だけで 1 行挿入する (無い列は警告して落とす)。"""
        cols = set(self.columns(conn, table))
        names: List[str] = []
        values: List[Any] = []
        for key, value in row.items():
            if key not in cols:
                if (table, key) not in self._dropped_warned:
                    self._dropped_warned.add((table, key))
                    self.warn(f"{table}.{key} does not exist in the target database; values dropped")
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            names.append(key)
            values.append(value)
        if rowid is not None:
            names.insert(0, "rowid")
            values.insert(0, rowid)
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        sql = (
            f"{verb} INTO {_quote(table)} ({', '.join(_quote(n) for n in names)}) "
            f"VALUES ({', '.join('?' for _ in names)})"
        )
        return conn.execute(sql, values)

    def existing_keys(
        self, conn: sqlite3.Connection, table: str, column: str, keys: Iterable[Any]
    ) -> Set[Any]:
        keys = [k for k in dict.fromkeys(keys) if k is not None]
        found: Set[Any] = set()
        for i in range(0, len(keys), _CHUNK):
            part = keys[i:i + _CHUNK]
            rows = conn.execute(
                f"SELECT {_quote(column)} FROM {_quote(table)} "
                f"WHERE {_quote(column)} IN ({', '.join('?' for _ in part)})",
                part,
            ).fetchall()
            found.update(r[0] for r in rows)
        return found

    def existing_keys_of_target(
        self, conn: sqlite3.Connection, table: str, column: str, owner_column: str, keys: Iterable[Any]
    ) -> Set[Any]:
        """existing_keys のうち、owner_column が取り込み先のペルソナ ID の行のもの。"""
        keys = [k for k in dict.fromkeys(keys) if k is not None]
        found: Set[Any] = set()
        for i in range(0, len(keys), _CHUNK):
            part = keys[i:i + _CHUNK]
            rows = conn.execute(
                f"SELECT {_quote(column)} FROM {_quote(table)} "
                f"WHERE {_quote(owner_column)} = ? "
                f"AND {_quote(column)} IN ({', '.join('?' for _ in part)})",
                [self.target, *part],
            ).fetchall()
            found.update(r[0] for r in rows)
        return found

    def world_key(self, table: str, value: Any) -> Any:
        """WORLD_STRING_KEY_TABLES の表 table の元の主キー value を、取り込み先の値にする。

        空でない文字列だけを derived_id(table, value) に置き換え、それ以外 (None など) は
        そのまま返す。参照の列も参照先の表の名前で同じ関数を通すので、親子の対応が保たれる。
        """
        if not isinstance(value, str) or not value:
            return value
        return self.derived_id(table, value)

    def remap_thread_id(self, thread_id: Any) -> Any:
        """スレッド ID の前半 (ペルソナ ID の部分) を取り込み先に置き換える。

        前半を持たない (コロンを含まない) ID はそのまま使い、恒等写像として
        thread_map に入れる — stage_chatlog の既存スレッドの照合は thread_map の
        値で行うので、入れないと取り込み先に同じ ID があるスレッドを再挿入して
        主キーが衝突する。
        """
        if not isinstance(thread_id, str):
            return thread_id
        if thread_id in self.thread_map:
            return self.thread_map[thread_id]
        if ":" not in thread_id:
            self.warn(f"thread id {thread_id!r} has no persona prefix; kept as is")
            self.thread_map[thread_id] = thread_id
            return thread_id
        mapped = f"{self.target}:{thread_id.split(':', 1)[1]}"
        self.thread_map[thread_id] = mapped
        return mapped

    def remap_resource(self, value: Any) -> Any:
        return self.target if value == self.source else value

    def remap_exact(self, value: Any) -> Any:
        """extensions/ の値のうち、元のペルソナ ID・既知のスレッド ID と完全一致するものだけ置き換える。"""
        if not isinstance(value, str):
            return value
        if value == self.source:
            return self.target
        if value in self.thread_map:
            return self.thread_map[value]
        return value

    def plan_file(self, zip_name: str, dest: Path) -> None:
        self.reserved.add(_norm(dest))
        self.file_plans.append(_FilePlan(zip_name, dest))

    def is_reserved(self, path: Path) -> bool:
        return _norm(path) in self.reserved

    # --- 実行 ---------------------------------------------------------------

    def run(self, memory_db_path: Path, saiverse_db_path: Path) -> None:
        world = sqlite3.connect(str(saiverse_db_path), timeout=30, isolation_level=None)
        memory: Optional[sqlite3.Connection] = None
        try:
            if world.execute("SELECT 1 FROM ai WHERE AIID = ?", (self.target,)).fetchone() is None:
                raise ValueError(f"persona not found in saiverse.db: {self.target}")
            memory = _open_memory_db(memory_db_path)
            if self.mode == "new":
                (n_messages,) = memory.execute("SELECT COUNT(*) FROM messages").fetchone()
                if n_messages:
                    raise ValueError(
                        f"mode='new' requires a persona without messages; "
                        f"{self.target} has {n_messages}. Use mode='merge'."
                    )
            else:
                self.check_pending_marker(world, memory)
            self.report_refused_raw_text()
            memory_committed = False
            try:
                memory.execute("BEGIN IMMEDIATE")
                self.stage_chatlog(memory)
                self.stage_knowledge(memory)
                self.stage_journal_memory(memory)
                if self.memory_new:
                    self.stage_ext_memory(memory)
                    self.record_memory_applied(memory)
                else:
                    self.stage_merge_recall_edges(memory)
                world.execute("BEGIN IMMEDIATE")
                self.stage_task_book(world)
                self.stage_profile(world)
                if self.world_new:
                    self.stage_ext_saiverse(world)
                    self.stage_items(world)
                    self.record_world_applied(world)
                self.stage_day_reports()
                if self.memory_new:
                    self.stage_persona_dir_files()
                if self.mode == "merge":
                    self.count_merge_skips()
                self.report_invalidated_media()
                self.write_files()
                if self.mode == "new":
                    self.write_pending_marker()
                memory.execute("COMMIT")
                memory_committed = True
                self.commit_world(world)
            except BaseException:
                for conn in (world, memory) if not memory_committed else (world,):
                    try:
                        if conn.in_transaction:
                            conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        LOGGER.warning("[persona_pack] rollback failed", exc_info=True)
                if not memory_committed:
                    self.remove_created_files()
                else:
                    LOGGER.error(
                        "[persona_pack] memory.db was committed but saiverse.db was not "
                        "(persona=%s); %s is left in the persona folder. Importing the same "
                        "pack with mode='merge' finishes the saiverse.db part",
                        self.target, WORLD_PENDING_MARKER,
                    )
                raise
            if self.clear_marker:
                self.remove_pending_marker()
        finally:
            world.close()
            if memory is not None:
                memory.close()

    def report_refused_raw_text(self) -> None:
        """_PackData.refused_raw_text の行を警告してスキップ件数に載せる。"""
        for key, message in self.pack.refused_raw_text:
            self.result.count(key).skipped += 1
            self.warn(message)

    def commit_world(self, world: sqlite3.Connection) -> None:
        """saiverse.db の commit (memory.db の commit の後。テストはここで失敗を起こす)。"""
        world.execute("COMMIT")

    # --- 片側だけ commit された取り込みの印 ---------------------------------------

    def write_pending_marker(self) -> None:
        """mode="new": memory.db の commit 前に印を書く (失敗したら created_files で消える)。"""
        payload = {
            "persona_id": self.source,
            "format": FORMAT_NAME,
            "version": self.pack.version,
            "zip_sha256": self.zip_sha256,
            "import_id": self.import_id,
            "overwrite_profile": self.overwrite_profile,
        }
        tmp = self.marker_path.with_name(self.marker_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.marker_path)
        self.created_files.append(self.marker_path)

    def check_pending_marker(self, world: sqlite3.Connection, memory: sqlite3.Connection) -> None:
        """mode="merge": 印があり、同じパックなら commit されていない側の工程を再開する。

        同じパックの判定は、印の persona_id が manifest の persona_id と、印の
        zip_sha256 / import_id がこの ZIP とこのペルソナから決まる値と一致すること。
        一致しなければ再開せず (印の overwrite_profile も使わない)、印を残して警告する
        — 利用者が正しい ZIP で入れ直せるように。

        一致したら、両方の DB の適用済み記録 (memory.db と saiverse.db の
        persona_pack_imports) を import_id で見る:

        - memory.db に無い (印を書いた後、memory.db の COMMIT 前に止まった):
          memory.db 側の mode="new" の工程を行う (resume_memory)。
        - saiverse.db に無い: 世界側の mode="new" の工程を行う (resume_world)。
        - 両方にある (世界側の commit の後、印を消す前に止まった): 工程は
          やり直さずに印だけ消す。

        どちらの工程も、照合 (UUID・INSERT OR IGNORE・同じ中身のファイルの再利用) で
        既に入っている行とファイルを二重にしない。
        """
        if not self.marker_path.is_file():
            return
        try:
            marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.warn(f"{WORLD_PENDING_MARKER} could not be read ({exc}); the earlier import is not resumed")
            return
        if not isinstance(marker, dict) or marker.get("format") != FORMAT_NAME:
            self.warn(f"{WORLD_PENDING_MARKER} is not a persona pack marker; the earlier import is not resumed")
            return
        if marker.get("persona_id") != self.source:
            self.warn(
                f"an earlier import of the pack of {marker.get('persona_id')!r} is unfinished in this "
                f"persona; this pack ({self.source!r}) does not resume it"
            )
            return
        if marker.get("zip_sha256") != self.zip_sha256 or marker.get("import_id") != self.import_id:
            self.warn(
                "an earlier import of a different file of this persona's pack is unfinished in this "
                "persona; this file does not resume it. Import the same file again with merge to "
                f"finish it ({WORLD_PENDING_MARKER} is kept)"
            )
            return
        self.clear_marker = True
        self.resume_memory = not self.memory_applied(memory)
        self.resume_world = not self.world_applied(world)
        if not self.resume_memory and not self.resume_world:
            self.warn(
                "the world part of an earlier new-persona import of this pack was already applied; "
                f"only {WORLD_PENDING_MARKER} is removed"
            )
            return
        if self.resume_world and marker.get("overwrite_profile") is True:
            self.overwrite_profile = True
        if self.resume_memory:
            self.warn(
                "resuming the unfinished part of an earlier new-persona import of this pack "
                "(the memory part: perception records, memopedia fragments and edit history, "
                "other memory tables and persona folder files)"
            )
        if self.resume_world:
            self.warn(
                "resuming the unfinished part of an earlier new-persona import of this pack "
                "(tasks, schedules, event log, items and private room)"
            )

    def memory_applied(self, memory: sqlite3.Connection) -> bool:
        """memory.db の _MEMORY_IMPORTS_TABLE にこの取り込みの import_id があるか (memory.db 側が commit 済みか)。"""
        if not self.table_exists(memory, _MEMORY_IMPORTS_TABLE):
            return False
        return memory.execute(
            f"SELECT 1 FROM {_MEMORY_IMPORTS_TABLE} WHERE IMPORT_ID = ?", (self.import_id,)
        ).fetchone() is not None

    def record_memory_applied(self, memory: sqlite3.Connection) -> None:
        """memory.db 側の mode="new" の工程を行ったことを、同じトランザクションで _MEMORY_IMPORTS_TABLE に書く。"""
        memory.execute(_MEMORY_IMPORTS_TABLE_DDL)
        memory.execute(
            f"INSERT OR REPLACE INTO {_MEMORY_IMPORTS_TABLE} (IMPORT_ID, APPLIED_AT) VALUES (?, ?)",
            (self.import_id, int(time.time())),
        )

    def world_applied(self, world: sqlite3.Connection) -> bool:
        """persona_pack_imports にこの取り込みの import_id があるか (世界側が commit 済みか)。"""
        if not self.table_exists(world, _IMPORTS_TABLE):
            return False
        return world.execute(
            f"SELECT 1 FROM {_IMPORTS_TABLE} WHERE IMPORT_ID = ?", (self.import_id,)
        ).fetchone() is not None

    def record_world_applied(self, world: sqlite3.Connection) -> None:
        """世界側の工程を行ったことを、同じトランザクションで persona_pack_imports に書く。"""
        world.execute(_IMPORTS_TABLE_DDL)
        world.execute(
            f"INSERT OR REPLACE INTO {_IMPORTS_TABLE} (IMPORT_ID, PERSONA_ID, APPLIED_AT) "
            "VALUES (?, ?, ?)",
            (self.import_id, self.target, int(time.time())),
        )

    def remove_pending_marker(self) -> None:
        """saiverse.db の commit が成功した後に印を消す (消せなくても取り込みは成功のまま)。"""
        try:
            self.marker_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.warn(f"{WORLD_PENDING_MARKER} could not be removed: {exc}")

    def write_files(self) -> None:
        """計画したファイルを書く。既存のファイルは決して上書きしない ("xb")。"""
        for plan in self.file_plans:
            missing: List[Path] = []
            directory = plan.dest.parent
            while not directory.exists():
                missing.append(directory)
                directory = directory.parent
            plan.dest.parent.mkdir(parents=True, exist_ok=True)
            self.created_dirs.extend(reversed(missing))
            with plan.dest.open("xb") as f:
                self.created_files.append(plan.dest)
                f.write(self.reader.read(plan.zip_name))

    def remove_created_files(self) -> None:
        for path in reversed(self.created_files):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                LOGGER.warning("[persona_pack] could not remove %s", path, exc_info=True)
        for directory in reversed(self.created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass

    # --- chatlog/ + attachments/ ------------------------------------------------

    def stage_chatlog(self, conn: sqlite3.Connection) -> None:
        pack = self.pack
        # スレッド ID の写像を先に全部決める (stelis の親・Chronicle の
        # metadata.thread_id・extensions/ の完全一致置き換えが参照する)
        for row in pack.threads:
            self.remap_thread_id(row["id"])
        for row in pack.stelis_threads:
            self.remap_thread_id(row["thread_id"])
            self.remap_thread_id(row.get("parent_thread_id"))
        for row in pack.messages:
            self.remap_thread_id(row["thread_id"])

        # threads
        counts = self.result.count("threads") if pack.threads else None
        existing = self.existing_keys(conn, "threads", "id", self.thread_map.values())
        for row in pack.threads:
            new_id = self.remap_thread_id(row["id"])
            if new_id in existing:
                counts.skipped += 1
                continue
            decoded = _decode_row(row, "threads")
            decoded["id"] = new_id
            decoded["resource_id"] = self.remap_resource(decoded.get("resource_id"))
            self.insert(conn, "threads", decoded)
            existing.add(new_id)
            counts.added += 1

        # stelis_threads
        if pack.stelis_threads:
            counts = self.result.count("stelis_threads")
            present = self.existing_keys(
                conn, "stelis_threads", "thread_id",
                (self.remap_thread_id(r["thread_id"]) for r in pack.stelis_threads),
            )
            for row in pack.stelis_threads:
                new_id = self.remap_thread_id(row["thread_id"])
                if new_id in present:
                    counts.skipped += 1
                    continue
                decoded = _decode_row(row, "stelis_threads")
                decoded["thread_id"] = new_id
                decoded["parent_thread_id"] = self.remap_thread_id(decoded.get("parent_thread_id"))
                self.insert(conn, "stelis_threads", decoded)
                present.add(new_id)
                counts.added += 1

        # 既にあるものは照合で飛ばし、無いものは下で入れるので、全部が取り込み先に揃う
        self.message_ids = {m["id"] for m in pack.messages}
        if not pack.messages:
            self.plan_attachments(conn, [])
            return

        # messages: id で照合し、無いものだけ入れる
        counts = self.result.count("messages")
        present = self.existing_keys(conn, "messages", "id", (m["id"] for m in pack.messages))
        new_messages: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for msg in pack.messages:
            if msg["id"] in present or msg["id"] in seen:
                counts.skipped += 1
                continue
            seen.add(msg["id"])
            new_messages.append(msg)

        self.plan_attachments(conn, new_messages)

        # メッセージの行が指すスレッドが無ければ器だけ作る
        thread_counts = self.result.count("threads")
        known_threads = self.existing_keys(
            conn, "threads", "id", (self.remap_thread_id(m["thread_id"]) for m in new_messages)
        )
        for msg in new_messages:
            tid = self.remap_thread_id(msg["thread_id"])
            if tid not in known_threads:
                self.insert(conn, "threads", {"id": tid, "resource_id": self.target})
                known_threads.add(tid)
                thread_counts.added += 1

        # 正典順 (created_at, seq) で挿入する (native_export の _canonical_import_order と同じ鍵)
        def _order(item: Tuple[int, Dict[str, Any]]) -> Tuple[Any, ...]:
            pos, msg = item
            seq = _as_int(msg.get("seq"))
            return (_epoch_key(iso_to_epoch(msg.get("created_at"))),
                    (0, seq) if seq is not None else (1, pos))

        ordered = [m for _, m in sorted(enumerate(new_messages), key=_order)]
        rowids = self.seq_rowids_if_free(conn, ordered)

        from sai_memory.memory.storage import write_deferred_message_embedding

        for idx, msg in enumerate(ordered):
            row = self.decode_message(msg)
            self.insert(conn, "messages", row, rowid=rowids[idx] if rowids else None)
            content = row.get("content")
            if isinstance(content, str) and content.strip():
                write_deferred_message_embedding(conn, row["id"], commit=False)
                self.result.embeddings_deferred += 1
            counts.added += 1

    def seq_rowids_if_free(
        self, conn: sqlite3.Connection, ordered: List[Dict[str, Any]]
    ) -> Optional[List[int]]:
        """seq (元の rowid) を取り込み先の rowid に使えるか (native_export の _seq_rowids_if_free と同じ規則)。"""
        seqs: List[int] = []
        for msg in ordered:
            seq = msg.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
                return None
            seqs.append(seq)
        if len(set(seqs)) != len(seqs):
            return None
        for i in range(0, len(seqs), _CHUNK):
            part = seqs[i:i + _CHUNK]
            (occupied,) = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE rowid IN ({', '.join('?' for _ in part)})",
                part,
            ).fetchone()
            if occupied:
                return None
        return seqs

    def decode_message(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        row: Dict[str, Any] = {}
        for key, value in msg.items():
            if key == "seq":
                continue
            if key == "created_at":
                row[key] = iso_to_epoch(value)
            elif key == "metadata":
                continue
            else:
                row[key] = decode_value(value)
        original_thread = msg["thread_id"]
        row["thread_id"] = self.remap_thread_id(original_thread)
        row["resource_id"] = self.remap_resource(row.get("resource_id"))

        is_raw, metadata = _decode_json_text(msg.get("metadata"))
        if not is_raw:
            metadata = self.relocate_metadata(metadata)
            if self.source != self.target:
                if metadata is None:
                    metadata = {}
                if isinstance(metadata, dict):
                    metadata = dict(metadata)
                    metadata["transplanted_from"] = {
                        "persona_id": self.source,
                        "thread_id": original_thread,
                    }
                else:
                    self.warn(f"message {msg['id']}: metadata is not an object; transplanted_from not recorded")
        elif self.source != self.target:
            self.warn(f"message {msg['id']}: metadata is not a JSON object or array; transplanted_from not recorded")
        row["metadata"] = _encode_json_text(is_raw, metadata)
        return row

    def perception_media_rows(self, conn: sqlite3.Connection) -> Iterable[Tuple[str, Dict[str, Any]]]:
        """取り込む知覚台帳の行 (表, ZIP の行)。

        知覚台帳は extensions/saiverse/memory_db/ で入るので memory.db 側の mode="new" の
        工程 (memory_new) のときだけで、取り込み先に表が無いもの (stage_ext_memory が
        スキップする) は含めない。
        """
        if not self.memory_new:
            return
        for table in PERCEPTION_MEDIA_TABLES:
            rows = self.pack.ext_memory_tables.get(table)
            if not rows or not self.table_exists(conn, table):
                continue
            for row in rows:
                yield table, row

    def plan_attachments(self, conn: sqlite3.Connection, new_messages: List[Dict[str, Any]]) -> None:
        """取り込む message と知覚台帳の行が参照する添付を共有メディアフォルダへ置く計画を立てる。"""
        if not self.pack.attachment_index:
            return
        counts = self.result.count("attachments")
        needed: Set[str] = set()
        # 知覚台帳の media の項目の (元の path, 参照の鍵)。path_relocations を作る
        perception_paths: List[Tuple[str, str]] = []
        for msg in new_messages:
            is_raw, metadata = _decode_json_text(msg.get("metadata"))
            if is_raw:
                continue
            for entry in _attachment_refs(metadata):
                needed.add(_attachment_key(entry))
        for table, row in self.perception_media_rows(conn):
            for entry in _media_entry_refs(parse_media_column(decode_value(row.get("media")))):
                needed.add(_attachment_key(entry))
                if isinstance(entry.get("path"), str) and entry["path"]:
                    perception_paths.append((entry["path"], _attachment_key(entry)))
            column = ROOM_STATE_COLUMNS.get(table)
            if column is not None:
                for entry in room_state_media_refs(table, decode_value(row.get(column))):
                    needed.add(_attachment_key(entry))
        for entry in self.pack.attachment_index:
            ref = entry.get("ref")
            zip_path = entry.get("path")
            if ref not in needed or not zip_path:
                counts.skipped += 1
                continue
            # 形 (attachments/<種類>/<ファイル名>) は _load_pack が検査済み
            _attachments, kind, name = PurePosixPath(zip_path).parts
            dest_dir = self.home / (kind if kind in _MEDIA_DIR_NAMES else "documents")
            data_hash = _sha256_bytes(self.reader.read(zip_path))
            summary_zip = entry.get("summary_path")
            if summary_zip is not None and summary_zip not in self.reader.names:
                summary_zip = None
            dest, reused = self.choose_media_dest(dest_dir, name, data_hash, summary_zip is not None)
            self.relocations[ref] = _Relocation(dest=dest)
            if reused:
                counts.skipped += 1
            else:
                self.plan_file(zip_path, dest)
                counts.added += 1
            if summary_zip is not None:
                summary_dest = dest.with_name(dest.name + _SUMMARY_SUFFIX)
                if not summary_dest.exists() and not self.is_reserved(summary_dest):
                    self.plan_file(summary_zip, summary_dest)
        # relocate_media_entries が知覚台帳の media の各項目の path に書く値 (同じ鍵の
        # relocations の復元先) を、元の path から引けるようにする
        for path, key in perception_paths:
            reloc = self.relocations.get(key)
            if reloc is None:
                continue
            dests = self.path_relocations.setdefault(path, [])
            if str(reloc.dest) not in dests:
                dests.append(str(reloc.dest))

    def choose_media_dest(
        self, directory: Path, name: str, data_hash: str, with_summary: bool
    ) -> Tuple[Path, bool]:
        """同名で中身が同じファイルがあれば再利用、違えば ``<stem>_<n><ext>`` へ避ける。"""
        stem, ext = Path(name).stem, Path(name).suffix
        n = 0
        while True:
            candidate = directory / (name if n == 0 else f"{stem}_{n}{ext}")
            n += 1
            if self.is_reserved(candidate):
                continue
            if candidate.exists():
                if candidate.is_file() and _sha256_file(candidate) == data_hash:
                    return candidate, True
                continue
            if with_summary and candidate.with_name(candidate.name + _SUMMARY_SUFFIX).exists():
                continue
            return candidate, False

    def relocate_metadata(self, metadata: Any) -> Any:
        """metadata の添付参照の uri / path を取り込み先に合わせる (モジュール docstring)。

        ZIP から実体を復元した参照は、元の uri / path を引き継がず、復元先の共有メディア
        フォルダの種類と実際のファイル名から uri (``saiverse://<種類>/<ファイル名>``) と
        path (復元先の絶対パス) を組み直す。元の uri が ``saiverse://image/../x`` の
        ような形でも、取り込み先にはその文字列が残らない。復元しなかった参照は
        uri と path の両方を None にする — ``saiverse://<種類>/<ファイル名>`` の uri を
        残すと、取り込み先の共有メディアフォルダに同名の別のファイルがあればそれに
        解決されてしまうため (件数は report_invalidated_media が警告に載せる)。
        """
        if not isinstance(metadata, dict):
            return metadata
        out = dict(metadata)
        for key in _ATTACHMENT_KEYS:
            entries = out.get(key)
            if not isinstance(entries, list):
                continue
            out[key] = self.relocate_media_entries(entries)
        return out

    def relocate_media_entries(self, entries: List[Any]) -> List[Any]:
        """添付の項目の list を :meth:`relocate_metadata` の規則で書き換えた写しを返す。

        messages.metadata の images / documents / media と、知覚台帳の media 列
        (export.PERCEPTION_MEDIA_TABLES) の両方がこれを通る。
        """
        rewritten = []
        for entry in entries:
            if isinstance(entry, dict) and (entry.get("uri") or entry.get("path")):
                key = _attachment_key(entry)
                reloc = self.relocations.get(key)
                entry = dict(entry)
                if reloc is not None:
                    entry["uri"] = _URI_PREFIX_BY_DIR[reloc.dest.parent.name] + reloc.dest.name
                    entry["path"] = str(reloc.dest)
                else:
                    self.invalidated_media_keys.add(key)
                    entry["uri"] = None
                    entry["path"] = None
            rewritten.append(entry)
        return rewritten

    def report_invalidated_media(self) -> None:
        """relocate_media_entries が uri / path を None にした添付参照の件数を警告に載せる。

        件数は参照の鍵 (_attachment_key) の種類の数 — 同じ参照を書き換える呼び出しが
        複数回あっても (room_state_digest_map と relocate_room_state_column) 一度だけ数える。
        """
        if self.invalidated_media_keys:
            self.warn(
                f"{len(self.invalidated_media_keys)} attachment reference(s) could not be restored "
                "from the pack; their uri and path were set to None"
            )

    def relocate_media_column(self, value: Any) -> Any:
        """知覚台帳の media 列の値 (decode 済み) を書き換えて、保存する値を返す。

        項目の list として読めた値は :meth:`relocate_media_entries` を通して JSON 文字列に
        する。読めない値 (None・list でない JSON・解釈できない文字列) はそのまま返す —
        読み手 (perception_buffer._decode_media) はそれを「メディアなし」として扱い、
        ファイルを読みに行かない。
        """
        entries = parse_media_column(value)
        if entries is None:
            return value
        return json.dumps(self.relocate_media_entries(entries), ensure_ascii=False)

    def relocate_room_state_snapshot(self, snapshot: Any) -> Any:
        """部屋の様子の束 (snapshot) の各パッケージの media を取り込み先に合わせた写しを返す。

        media の項目は :meth:`relocate_media_entries` の規則で書き換え、復元できず path が
        空になった項目はパッケージの media から外す。束の media の path は
        sai_memory/room_state.bundle_is_valid が空でない文字列を要求し、提示のときは
        そのまま LLM へ添付として渡る — path を None にして残すと束ごと無効になり、
        元の path を残すと取り込み先のメディアルートの外のファイルを読みうるため。
        packages を持つ dict でない値はそのまま返す (読み手は束として扱わない)。
        """
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("packages"), list):
            return snapshot
        packages: List[Any] = []
        for package in snapshot["packages"]:
            if isinstance(package, dict) and isinstance(package.get("media"), list):
                package = dict(package)
                package["media"] = [
                    m for m in self.relocate_media_entries(package["media"])
                    if not isinstance(m, dict) or (isinstance(m.get("path"), str) and m["path"])
                ]
            packages.append(package)
        out = dict(snapshot)
        out["packages"] = packages
        return out

    def room_state_digest_map(self) -> Dict[str, str]:
        """取り込む部屋の様子の束の指紋の対応 (元の束の指紋 → 書き換えた束の指紋)。

        差分の記帳の base_digest は土台の束の snapshot_digest (正準 JSON の sha256) で、
        束の media を :meth:`relocate_room_state_snapshot` で書き換えると土台の指紋も
        変わる。付け直さないと chain_is_intact が「土台が見えていない」と判定し、提示の
        差分が全文へ開き直される。土台の束は別の行 (別の表) にあることもあるので、
        行を入れる前に :data:`ROOM_STATE_COLUMNS` の全行から作る
        (saiverse/building_id_repair.plan_memory_rewrite と同じ付け直し)。
        """
        digests: Dict[str, str] = {}
        for table, column in ROOM_STATE_COLUMNS.items():
            for row in self.pack.ext_memory_tables.get(table) or []:
                data = parse_room_state_column(table, decode_value(row.get(column)))
                for record in room_state_records(table, data):
                    snapshot = record.get("snapshot")
                    if not isinstance(snapshot, dict):
                        continue
                    relocated = self.relocate_room_state_snapshot(snapshot)
                    if relocated != snapshot:
                        digests[snapshot_digest(snapshot)] = snapshot_digest(relocated)
        return digests

    def relocate_room_state_column(
        self, table: str, value: Any, digests: Dict[str, str]
    ) -> Any:
        """:data:`ROOM_STATE_COLUMNS` の列の値 (decode 済み) を書き換えて、保存する値を返す。

        各記帳の snapshot を :meth:`relocate_room_state_snapshot` で書き換え、base_digest が
        ``digests`` (:meth:`room_state_digest_map`) にあれば書き換えた土台の指紋に付け直し、
        dropped_media (縮小済みの記帳) を :meth:`relocate_dropped_media` で付け替える。
        書き換えが無ければ元の値をそのまま返す。読めない値もそのまま返す — 読み手
        (sai_memory/room_state.raw_room_entries と _parse_item_state) はそれを記帳なしと
        して扱う。
        """
        data = parse_room_state_column(table, value)
        if data is None:
            return value
        changed = False
        for record in room_state_records(table, data):
            snapshot = record.get("snapshot")
            relocated = self.relocate_room_state_snapshot(snapshot)
            if relocated != snapshot:
                record["snapshot"] = relocated
                changed = True
            base = record.get("base_digest")
            if isinstance(base, str) and base in digests:
                record["base_digest"] = digests[base]
                changed = True
            dropped = record.get(DROPPED_MEDIA_FIELD)
            if isinstance(dropped, list):
                relocated_dropped = self.relocate_dropped_media(dropped)
                if relocated_dropped != dropped:
                    record[DROPPED_MEDIA_FIELD] = relocated_dropped
                    changed = True
        if not changed:
            return value
        return json.dumps(data, ensure_ascii=False)

    def relocate_dropped_media(self, dropped: List[Any]) -> List[str]:
        """縮小済みの部屋の記帳の dropped_media (画像の path の一覧) を取り込み先に合わせる。

        dropped_media は sai_memory/presented_reduction.mark_presentation_reductions が書き、
        提示のとき _reduce_rooms がバッチの media (perception_batches.media) の各項目の
        path と文字列で比べ、一致した項目を提示から外す。バッチの media は
        :meth:`relocate_media_entries` で path が書き換わるので、dropped_media の各 path も
        同じ項目が取り込み後に持つ path (path_relocations) へ写す — 移植前に外れていた
        画像は移植後も外れる。path_relocations に無い path (復元しなかった項目) は一覧から
        外す: その項目の path は取り込みで None になり (relocate_media_entries)、_reduce_rooms
        の比較は空の path を一致させないので、元の環境の path を残しても外す相手が無く、
        元の環境のパスが取り込み先に残るだけになるため。
        """
        out: List[str] = []
        for path in dropped:
            if not isinstance(path, str) or not path:
                continue
            for dest in self.path_relocations.get(path, ()):
                if dest not in out:
                    out.append(dest)
        return out

    # --- knowledge/ -------------------------------------------------------------

    def stage_knowledge(self, conn: sqlite3.Connection) -> None:
        pages = self.pack.pages
        if not pages:
            return
        from sai_memory.memopedia.storage import generate_diff, record_page_edit

        target_rows: Dict[str, Tuple[Any, ...]] = {}
        ids = [p["id"] for p in pages]
        for i in range(0, len(ids), _CHUNK):
            part = ids[i:i + _CHUNK]
            for row in conn.execute(
                "SELECT id, title, summary, content, updated_at, metadata, category "
                f"FROM memopedia_pages WHERE id IN ({', '.join('?' for _ in part)})",
                part,
            ):
                target_rows[row[0]] = row

        chronicle_plan: Optional[_ChroniclePlan] = None
        if not self.memory_new:
            chronicle_plan = self.plan_chronicle_merge(conn, pages, target_rows)

        to_insert: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for page in pages:
            is_chronicle = page.get("category") == _CATEGORY_CHRONICLE and not page.get("is_trunk")
            counts = self.result.count("chronicle_pages" if is_chronicle else "pages")
            if page["id"] in seen:
                counts.skipped += 1
                continue
            seen.add(page["id"])
            if is_chronicle and chronicle_plan is not None:
                # 合流の Chronicle は plan_chronicle_merge の判定だけで決める。
                # 取り込み先に同じ id がある行も上書きしない (metadata の
                # parent_id / source_ids を書き換えると取り込み先の木が動く)。
                if page["id"] in chronicle_plan.accepted:
                    to_insert.append(page)
                else:
                    counts.skipped += 1
                continue
            existing = target_rows.get(page["id"])
            if existing is None:
                to_insert.append(page)
                continue
            decoded = _decode_row(page, "memopedia_pages")
            zip_updated = decoded.get("updated_at")
            target_updated = existing[4]
            if (
                decoded.get("content") == existing[3]
                or not isinstance(zip_updated, (int, float))
                or not isinstance(target_updated, (int, float))
                or zip_updated <= target_updated
            ):
                counts.skipped += 1
                continue
            self.update_page(conn, page, decoded, existing)
            record_page_edit(
                conn,
                page_id=page["id"],
                diff_text=generate_diff(existing[3] or "", decoded.get("content") or ""),
                edit_type="update",
                edit_source=_EDIT_SOURCE,
                before_title=existing[1],
                before_summary=existing[2],
                before_content=existing[3],
                commit=False,
            )
            conn.execute("DELETE FROM memopedia_embeddings WHERE page_id = ?", (page["id"],))
            counts.updated += 1

        if not to_insert:
            return
        decoded_pages = [_decode_row(p, "memopedia_pages") for p in to_insert]
        # [元が解釈できない文字列だったか, metadata の値] (番号の記録で書き換えるので list)
        metas: List[List[Any]] = [list(_decode_json_text(p.get("metadata"))) for p in to_insert]

        # 列の short_id
        (col_max,) = conn.execute("SELECT COALESCE(MAX(short_id), 0) FROM memopedia_pages").fetchone()
        originals = [_as_int(d.get("short_id")) for d in decoded_pages]
        planned = _plan_numbers(originals, int(col_max))
        for d, meta, orig, new in zip(decoded_pages, metas, originals, planned):
            if new != orig:
                d["short_id"] = new
                self.record_original(meta, "original_short_id", orig, d["id"])
                self.result.renumbered["pages.short_id"] = self.result.renumbered.get("pages.short_id", 0) + 1

        # metadata の番号 (コア記憶の core_id / Chronicle の short_id)
        for category, meta_key, original_key, counter, title_fmt in (
            (_CATEGORY_CORE, "core_id", "original_core_id", "pages.core_id", "コア記憶 c:{}"),
            (_CATEGORY_CHRONICLE, "short_id", "original_chronicle_short_id",
             "pages.chronicle_short_id", "chronicle:{}"),
        ):
            idx = [
                i for i, (d, (raw, meta)) in enumerate(zip(decoded_pages, metas))
                if d.get("category") == category and not raw and isinstance(meta, dict)
                and meta_key in meta
            ]
            if not idx:
                continue
            (meta_max,) = conn.execute(
                f"SELECT COALESCE(MAX(CAST(json_extract(metadata, '$.{meta_key}') AS INTEGER)), 0) "
                "FROM memopedia_pages WHERE category = ?",
                (category,),
            ).fetchone()
            originals = [_as_int(metas[i][1].get(meta_key)) for i in idx]
            planned = _plan_numbers(originals, int(meta_max or 0))
            for i, orig, new in zip(idx, originals, planned):
                if new == orig or new is None:
                    continue
                meta = metas[i][1]
                meta[meta_key] = new
                meta[original_key] = orig
                # 機構が書いた表題 (core_memory.add_core_memory / arasuji create_entry)
                # だけを新しい番号に合わせる
                if decoded_pages[i].get("title") == title_fmt.format(orig):
                    decoded_pages[i]["title"] = title_fmt.format(new)
                self.result.renumbered[counter] = self.result.renumbered.get(counter, 0) + 1

        for d, (is_raw, meta) in zip(decoded_pages, metas):
            if not is_raw and d.get("category") == _CATEGORY_CHRONICLE and isinstance(meta, dict):
                if isinstance(meta.get("thread_id"), str):
                    meta["thread_id"] = self.remap_thread_id(meta["thread_id"])
                if chronicle_plan is not None and d["id"] in chronicle_plan.to_root:
                    # 親が取り込まれない行・取り込み先と共有の行: 親の source_ids に
                    # 載らない子として下にぶら下げず、root_chronicle の未統合に置く
                    # (時間的に上位へ収まる子は bands.reconnect_contained_orphans が
                    # 次の束ねの冒頭で縁組する)。
                    d["parent_id"] = _ROOT_CHRONICLE
                    meta["is_consolidated"] = 0
            d["metadata"] = _encode_json_text(is_raw, meta)
            self.insert(conn, "memopedia_pages", d)
            is_chronicle = d.get("category") == _CATEGORY_CHRONICLE and not d.get("is_trunk")
            if is_chronicle:
                self.imported_chronicle_ids.add(d["id"])
            self.result.count("chronicle_pages" if is_chronicle else "pages").added += 1

    def plan_chronicle_merge(
        self,
        conn: sqlite3.Connection,
        pages: Sequence[Dict[str, Any]],
        target_rows: Dict[str, Tuple[Any, ...]],
    ) -> _ChroniclePlan:
        """合流で取り込む Chronicle ページを決める (intent §5)。書き込みはしない。

        取り込むのは「ZIP にあって取り込み先に同じ id が無い」行のうち:

        - Lv1 (metadata.level == 1): is_deleted でなく、source_ids が取り込み先の
          全 Lv1 と、この合流で先に受理した ZIP の Lv1 の source_ids と一つも
          交わらないもの。交わる行は、取り込み先で吸収・差し替え済みの旧記事か、
          取り込み先 (または ZIP の別の Lv1) と同じメッセージを覆う記事なので
          入れない (docs/issues/lv1_source_ids_duplicates_and_orphans.md)。
        - Lv2 以上: is_deleted でなく、source_ids の子が全員「取り込むと判定済み」
          か「ZIP にも取り込み先にも無い id」(sai_memory.arasuji.absorption.
          _sweep_broken_parents が掃除する) で、取り込む子が 1 本以上あるもの。
          取り込み先にある子を指す上位は入れない — その上位に delete_entry が
          かかると source_ids の子が root_chronicle へ戻され、取り込み先の木が動く。

        取り込む行の parent_id が取り込まない行・取り込み先にある行を指すときは、
        ``to_root`` に入れる (挿入時に parent_id = root_chronicle、
        is_consolidated = 0 にする)。
        """
        plan = _ChroniclePlan()

        # 取り込み先の chronicle ページの id と、全 Lv1 の source_ids
        target_ids: Set[str] = set()
        target_lv1_sources: Set[str] = set()
        for page_id, meta_json, is_deleted in conn.execute(
            "SELECT id, metadata, is_deleted FROM memopedia_pages "
            "WHERE category = ? AND is_trunk = 0",
            (_CATEGORY_CHRONICLE,),
        ):
            target_ids.add(page_id)
            if is_deleted:
                continue
            try:
                meta = json.loads(meta_json) if meta_json else None
            except (TypeError, ValueError):
                continue
            if not isinstance(meta, dict) or _as_int(meta.get("level")) != 1:
                continue
            sources = meta.get("source_ids")
            if isinstance(sources, list):
                target_lv1_sources.update(str(s) for s in sources)

        # ZIP 側の chronicle 行 (取り込み先に同じ id が無いもの)
        candidates: Dict[str, Tuple[int, List[str], Any]] = {}
        zip_ids: Set[str] = set()
        unreadable = deleted = 0
        for page in pages:
            if page.get("category") != _CATEGORY_CHRONICLE or page.get("is_trunk"):
                continue
            page_id = page["id"]
            if page_id in zip_ids:
                continue
            zip_ids.add(page_id)
            if page_id in target_rows:
                continue
            is_raw, meta = _decode_json_text(page.get("metadata"))
            level = _as_int(meta.get("level")) if not is_raw and isinstance(meta, dict) else None
            sources = meta.get("source_ids") if level is not None else None
            if (
                level is None
                or not isinstance(sources, list)
                or not all(isinstance(s, str) for s in sources)
            ):
                unreadable += 1
                continue
            if decode_value(page.get("is_deleted")):
                deleted += 1
                continue
            candidates[page_id] = (level, sources, decode_value(page.get("parent_id")))

        # level の昇順に、同じ level の中は ZIP での並び順 (sorted は安定) で判定する。
        # 受理した Lv1 の source_ids は被覆の集合へ足し、後に来る ZIP の Lv1 も
        # それと交差判定する (ZIP の中で同じメッセージを覆う Lv1 は最初の一本だけ入る)。
        overlapping = on_shared = 0
        for page_id, (level, sources, _parent) in sorted(
            candidates.items(), key=lambda item: item[1][0]
        ):
            if level <= 1:
                if target_lv1_sources.intersection(sources):
                    overlapping += 1
                    continue
                plan.accepted.add(page_id)
                target_lv1_sources.update(sources)
                continue
            live = 0
            ok = True
            for child in sources:
                if child in plan.accepted:
                    live += 1
                elif child in target_ids or child in zip_ids:
                    ok = False
                    break
            if ok and live:
                plan.accepted.add(page_id)
            else:
                on_shared += 1

        for page_id in plan.accepted:
            parent = candidates[page_id][2]
            if parent in (None, _ROOT_CHRONICLE) or parent in plan.accepted:
                continue
            plan.to_root.add(page_id)

        if overlapping:
            self.warn(
                f"chronicle: {overlapping} level-1 page(s) not imported; their source "
                "messages overlap the target's existing level-1 coverage"
            )
        if on_shared:
            self.warn(
                f"chronicle: {on_shared} upper page(s) not imported; they stand on pages "
                "shared with the target or not imported"
            )
        if deleted:
            self.warn(f"chronicle: {deleted} deleted page(s) not imported")
        if unreadable:
            self.warn(
                f"chronicle: {unreadable} page(s) not imported; level or source_ids in "
                "their metadata is unreadable"
            )
        return plan

    def record_original(self, meta_cell: List[Any], key: str, original: Any, page_id: str) -> None:
        """振り直した行の元の番号を metadata に残す。meta_cell は [is_raw, 値]。"""
        if meta_cell[0]:
            self.warn(f"memopedia page {page_id}: metadata is not a JSON object or array; {key} not recorded")
            return
        if meta_cell[1] is None:
            meta_cell[1] = {}
        if not isinstance(meta_cell[1], dict):
            self.warn(f"memopedia page {page_id}: metadata is not an object; {key} not recorded")
            return
        meta_cell[1][key] = original

    def update_page(
        self,
        conn: sqlite3.Connection,
        page: Dict[str, Any],
        decoded: Dict[str, Any],
        existing: Tuple[Any, ...],
    ) -> None:
        """ZIP 側が新しいページで取り込み先の行を上書きする。

        metadata は取り込み先の値を基底にして ZIP 側のキーを重ねる (取り込み先だけが
        持つキーは残る。ZIP 側で消えたキーも取り込み先の値が残る)。番号 (列の
        short_id、metadata の core_id / Chronicle の short_id と original_*) は
        取り込み先の値を保つ — 取り込み先で既に参照されている番号だから。
        ZIP 側の metadata が JSON の object でも NULL でもない (array や、object /
        array として解釈できない文字列) ときは、その値で置き換える。
        """
        category = existing[6]
        try:
            target_meta = json.loads(existing[5]) if existing[5] else None
        except (TypeError, ValueError):
            target_meta = None
        is_raw, zip_meta = _decode_json_text(page.get("metadata"))
        keep_keys = ["original_short_id"]
        if category == _CATEGORY_CORE:
            keep_keys += ["core_id", "original_core_id"]
        if category == _CATEGORY_CHRONICLE:
            keep_keys += ["short_id", "original_chronicle_short_id"]
        decoded = dict(decoded)
        if not is_raw and (zip_meta is None or isinstance(zip_meta, dict)):
            base = dict(target_meta) if isinstance(target_meta, dict) else {}
            merged = {**base, **(zip_meta or {})}
            for key in keep_keys:
                if key in base:
                    merged[key] = base[key]
                else:
                    merged.pop(key, None)
            keep_null = zip_meta is None and not merged
            decoded["metadata"] = None if keep_null else json.dumps(merged, ensure_ascii=False)
        cols = set(self.columns(conn, "memopedia_pages"))
        assignments = [
            (k, v) for k, v in decoded.items()
            if k in cols and k not in ("id", "short_id", "created_at")
        ]
        conn.execute(
            f"UPDATE memopedia_pages SET {', '.join(f'{_quote(k)} = ?' for k, _ in assignments)} "
            "WHERE id = ?",
            [v for _, v in assignments] + [decoded["id"]],
        )

    # --- journal/ (memory.db 側) ------------------------------------------------

    def stage_journal_memory(self, conn: sqlite3.Connection) -> None:
        self.stage_activities_and_memos(conn)
        self.stage_clips(conn)
        self.stage_purpose_tags(conn)

    def stage_activities_and_memos(self, conn: sqlite3.Connection) -> None:
        pack = self.pack
        id_map: Dict[int, int] = {}
        if pack.activities:
            counts = self.result.count("activities")
            by_uuid = dict(conn.execute(
                "SELECT uuid, id FROM activities WHERE uuid IS NOT NULL"
            ).fetchall())
            open_by_name = dict(conn.execute(
                "SELECT name, id FROM activities WHERE status = 'open'"
            ).fetchall())
            for row in sorted(pack.activities, key=lambda r: _as_int(r.get("id")) or 0):
                zip_id = _as_int(row.get("id"))
                u = row.get("uuid")
                if u and u in by_uuid:
                    id_map[zip_id] = by_uuid[u]
                    counts.skipped += 1
                    continue
                if row.get("status") == "open" and row.get("name") in open_by_name:
                    # open な同名は一つだけ (idx_activities_open_name)。同じアクティビティとして扱う
                    id_map[zip_id] = open_by_name[row["name"]]
                    counts.skipped += 1
                    self.warn(
                        f"activity {row.get('name')!r} (uuid {u}) joined the open activity of the "
                        "same name in the target"
                    )
                    continue
                decoded = _decode_row(row, "activities")
                decoded.pop("id", None)
                decoded["uuid"] = u or str(uuid.uuid4())
                cur = self.insert(conn, "activities", decoded)
                id_map[zip_id] = cur.lastrowid
                by_uuid[decoded["uuid"]] = cur.lastrowid
                if decoded.get("status") == "open":
                    open_by_name[decoded.get("name")] = cur.lastrowid
                counts.added += 1
        if pack.memos:
            counts = self.result.count("memos")
            present_uuids = self.existing_keys(conn, "memos", "uuid", (m.get("uuid") for m in pack.memos))
            present_idem = self.existing_keys(conn, "memos", "idem_key", (m.get("idem_key") for m in pack.memos))
            for row in sorted(pack.memos, key=lambda r: _as_int(r.get("id")) or 0):
                u = row.get("uuid")
                idem = row.get("idem_key")
                if (u and u in present_uuids) or (idem and idem in present_idem):
                    counts.skipped += 1
                    continue
                activity_id = id_map.get(_as_int(row.get("activity_id")))
                if activity_id is None:
                    counts.skipped += 1
                    self.warn(f"memo {u}: its activity is not in the pack; skipped")
                    continue
                decoded = _decode_row(row, "memos")
                decoded.pop("id", None)
                decoded["activity_id"] = activity_id
                decoded["uuid"] = u or str(uuid.uuid4())
                self.insert(conn, "memos", decoded)
                present_uuids.add(decoded["uuid"])
                if idem:
                    present_idem.add(idem)
                counts.added += 1

    def stage_clips(self, conn: sqlite3.Connection) -> None:
        clips = self.pack.clips
        if not clips:
            return
        counts = self.result.count("clips")
        present = self.existing_keys(conn, "clips", "clip_id", (c["clip_id"] for c in clips))
        to_insert = []
        seen: Set[str] = set()
        for row in clips:
            if row["clip_id"] in present or row["clip_id"] in seen:
                counts.skipped += 1
                continue
            seen.add(row["clip_id"])
            to_insert.append(_decode_row(row, "clips"))
        if not to_insert:
            return
        (col_max,) = conn.execute("SELECT COALESCE(MAX(short_id), 0) FROM clips").fetchone()
        originals = [_as_int(d.get("short_id")) for d in to_insert]
        planned = _plan_numbers(originals, int(col_max))
        for d, orig, new in zip(to_insert, originals, planned):
            if new != orig:
                d["short_id"] = new
                # clips は metadata 列を持たないので元の番号は残せない
                self.result.renumbered["clips.short_id"] = self.result.renumbered.get("clips.short_id", 0) + 1
            self.insert(conn, "clips", d)
            counts.added += 1

    def stage_purpose_tags(self, conn: sqlite3.Connection) -> None:
        tags = self.pack.purpose_tags
        if not tags:
            return
        counts = self.result.count("purpose_tags")
        present = self.existing_keys(conn, "purpose_tags", "tag_id", (t["tag_id"] for t in tags))
        pairs = set(conn.execute("SELECT target_ref, purpose_ref FROM purpose_tags").fetchall())
        for row in tags:
            # tag_id の一致、または (target_ref, purpose_ref) の一致 (UNIQUE) は同じ付与として扱う
            pair = (row.get("target_ref"), row.get("purpose_ref"))
            if row["tag_id"] in present or pair in pairs:
                counts.skipped += 1
                continue
            self.insert(conn, "purpose_tags", _decode_row(row, "purpose_tags"))
            present.add(row["tag_id"])
            pairs.add(pair)
            counts.added += 1

    # --- journal/ (saiverse.db 側とファイル) -----------------------------------

    def stage_task_book(self, world: sqlite3.Connection) -> None:
        rows = self.pack.task_book
        if not rows:
            return
        counts = self.result.count("task_book")
        if not self.table_exists(world, "task_book"):
            counts.skipped += len(rows)
            self.warn("saiverse.db has no task_book table; task_book skipped")
            return
        # TASK_ID は元の値を保つ (task_book を参照する表は無い — database/models.py)。
        # 元の TASK_ID の行が取り込み先にあるとき:
        # - 取り込み先のペルソナの行なら同じ約束としてスキップする (A → B → A の往復の
        #   戻りや、同じパックの再実行はここで照合される)。
        # - 別のペルソナの行なら derived_id("task_book", 元の値) に振り直して入れる
        #   (同じ世界に同じパックから二体目を迎えたとき)。振り直した値の行が既に
        #   あればスキップする (二体目への再実行)。
        # 取り込み先のペルソナに同じ IDEM_KEY があるもの (uq_task_book_idem は
        # ペルソナごとの一意) も同じ約束として扱う。
        # 振り直して入れた行は ORIGIN_TASK_ID に元の値 (ZIP の行が既に ORIGIN_TASK_ID を
        # 持っていればその値 = 最初の環境の TASK_ID) を残す。照合は ZIP の行の TASK_ID /
        # ORIGIN_TASK_ID のどちらかが、取り込み先のペルソナの行の TASK_ID /
        # ORIGIN_TASK_ID のどちらかと一致したら同じ約束とみなす — 振り直された約束を
        # 書き出して元の環境へ合流で戻したとき、元の行と照合できるようにするため。
        # 取り込み先の task_book に ORIGIN_TASK_ID 列が無い (起動時のマイグレーション
        # 前の DB) ときは TASK_ID だけで照合する (insert が列を警告して落とす)。
        has_origin = "ORIGIN_TASK_ID" in self.columns(world, "task_book")
        originals = [r["TASK_ID"] for r in rows]
        zip_origins = [r.get("ORIGIN_TASK_ID") for r in rows]
        keys = [k for k in (*originals, *zip_origins) if isinstance(k, str) and k]
        derived = {tid: self.derived_id("task_book", tid) for tid in originals}
        present = self.existing_keys(world, "task_book", "TASK_ID", [*originals, *derived.values()])
        own = self.existing_keys_of_target(world, "task_book", "TASK_ID", "PERSONA_ID", keys)
        if has_origin:
            own |= self.existing_keys_of_target(
                world, "task_book", "ORIGIN_TASK_ID", "PERSONA_ID", keys
            )
        present_idem = self.existing_keys_of_target(
            world, "task_book", "IDEM_KEY", "PERSONA_ID", (r.get("IDEM_KEY") for r in rows)
        )
        for row, zip_origin in zip(rows, zip_origins):
            idem = row.get("IDEM_KEY")
            original = row["TASK_ID"]
            if not isinstance(zip_origin, str) or not zip_origin:
                zip_origin = None
            if original in own or (zip_origin and zip_origin in own) or (idem and idem in present_idem):
                counts.skipped += 1
                continue
            new_id = derived[original] if original in present else original
            if new_id in present:
                counts.skipped += 1
                continue
            decoded = _decode_row(row, "task_book")
            decoded["TASK_ID"] = new_id
            decoded["PERSONA_ID"] = self.target
            if new_id != original:
                decoded["ORIGIN_TASK_ID"] = zip_origin or original
            self.insert(world, "task_book", decoded)
            present.add(new_id)
            own.update(k for k in (new_id, original, zip_origin) if k)
            if idem:
                present_idem.add(idem)
            counts.added += 1

    def stage_day_reports(self) -> None:
        reports = self.pack.day_reports
        if not reports:
            return
        counts = self.result.count("day_reports")
        base = self.persona_dir / _DAY_REPORTS_DIR
        label = re.sub(r"[^A-Za-z0-9_.-]", "_", self.source)
        for zip_name, rel in reports:
            data_hash = _sha256_bytes(self.reader.read(zip_name))
            dest = base.joinpath(*rel.parts)
            if not dest.exists() and not self.is_reserved(dest):
                self.plan_file(zip_name, dest)
                counts.added += 1
                continue
            if dest.is_file() and _sha256_file(dest) == data_hash:
                counts.skipped += 1
                continue
            # 同じ日付で内容が違う: 別々の環境で生きたどちらも本物の記録なので両方残す
            stem, ext = dest.stem, dest.suffix
            n = 0
            while True:
                tag = f".from_{label}" if n == 0 else f".from_{label}_{n}"
                candidate = dest.with_name(f"{stem}{tag}{ext}")
                n += 1
                if self.is_reserved(candidate):
                    continue
                if candidate.exists():
                    if candidate.is_file() and _sha256_file(candidate) == data_hash:
                        counts.skipped += 1
                        break
                    continue
                self.plan_file(zip_name, candidate)
                counts.added += 1
                break

    # --- profile/ ---------------------------------------------------------------

    def stage_profile(self, world: sqlite3.Connection) -> None:
        profile = self.pack.profile
        if profile is None:
            return
        counts = self.result.count("profile")
        if not self.overwrite_profile:
            counts.skipped += 1
            return
        ai = _decode_row(profile["ai"])
        files = profile.get("files") if isinstance(profile.get("files"), dict) else {}
        cols = self.columns(world, "ai")
        updates: Dict[str, Any] = {}
        for col in cols:
            if col in PROFILE_PROTECTED_COLUMNS or col in _PROFILE_FILE_COLUMNS or col not in ai:
                continue
            updates[col] = ai[col]
        for col in _PROFILE_FILE_COLUMNS:
            if col not in cols or col not in ai:
                continue
            if not ai[col]:
                updates[col] = None
                continue
            zip_name = files.get(col)
            if not isinstance(zip_name, str) or zip_name not in self.reader.names:
                self.warn(f"profile: the file for ai.{col} is not in the pack; column left unchanged")
                continue
            name = PurePosixPath(zip_name).name
            _safe_relpath(name, "profile/profile.json")
            data_hash = _sha256_bytes(self.reader.read(zip_name))
            # 元の値が /api/media/images/<名前> (設定画面の ImageUpload が保存する形) なら、
            # その URL の配信元 (api/routes/media.py の serve_image = SAIVERSE_HOME/image/) へ
            # 置き、同じ形の URL で指す。それ以外は user_data/icons へ置き、絶対パスで指す
            # (api/deps.py の avatar_path_to_url がどちらも表示の URL に変える)
            as_api_url = isinstance(ai[col], str) and ai[col].strip().startswith(API_MEDIA_IMAGES_PREFIX)
            directory = self.home / "image" if as_api_url else self.home / "user_data" / "icons"
            dest, reused = self.choose_media_dest(directory, name, data_hash, False)
            if not reused:
                self.plan_file(zip_name, dest)
            updates[col] = API_MEDIA_IMAGES_PREFIX + dest.name if as_api_url else str(dest)
        for col in ai:
            if col not in cols and (("ai", col) not in self._dropped_warned):
                self._dropped_warned.add(("ai", col))
                self.warn(f"ai.{col} does not exist in the target database; value dropped")
        if updates:
            world.execute(
                f"UPDATE ai SET {', '.join(f'{_quote(c)} = ?' for c in updates)} WHERE AIID = ?",
                [*updates.values(), self.target],
            )
        self.result.profile_columns_overwritten = sorted(updates)
        counts.updated += 1

    # --- extensions/ と items/ (mode="new" のみ) ------------------------------------

    def report_refused_ext_memory_tables(self) -> None:
        """extensions/saiverse/memory_db/ の受けない表 (_PackData.ext_memory_refused) を警告してスキップ件数に載せる。"""
        for table, n_rows in sorted(self.pack.ext_memory_refused.items()):
            self.result.count(f"extensions/memory_db/{table}").skipped += n_rows
            self.warn(
                f"extensions/saiverse/memory_db/{table}.json is not a table a persona pack "
                f"writes to that folder; {n_rows} row(s) not imported"
            )

    def stage_ext_memory(self, conn: sqlite3.Connection) -> None:
        """extensions/saiverse/memory_db/ を取り込む (export.is_extension_memory_table の表だけ)。"""
        self.report_refused_ext_memory_tables()
        room_digests = self.room_state_digest_map()
        for table, rows in sorted(self.pack.ext_memory_tables.items()):
            counts = self.result.count(f"extensions/memory_db/{table}")
            if not rows:
                continue
            if not self.table_exists(conn, table):
                counts.skipped += len(rows)
                self.warn(f"memory.db has no table {table!r}; {len(rows)} row(s) skipped")
                continue
            if table == "desk_items":
                (n,) = conn.execute("SELECT COUNT(*) FROM desk_items").fetchone()
                if n:
                    counts.skipped += len(rows)
                    continue
            for row in rows:
                decoded = {k: self.remap_exact(decode_value(v)) for k, v in row.items()}
                if table in PERCEPTION_MEDIA_TABLES and "media" in decoded:
                    # 添付と同じ書き換え (復元先を指す / 復元できなければ path を落とす)。
                    # 参照するファイルの復元は plan_attachments が計画済み
                    decoded["media"] = self.relocate_media_column(decoded["media"])
                room_column = ROOM_STATE_COLUMNS.get(table)
                if room_column is not None and room_column in decoded:
                    # 部屋の様子の束の media も同じ規則で書き換え、差分の土台の指紋を付け直す
                    decoded[room_column] = self.relocate_room_state_column(
                        table, decoded[room_column], room_digests,
                    )
                cur = self.insert(conn, table, decoded, or_ignore=True)
                if cur.rowcount:
                    counts.added += 1
                else:
                    counts.skipped += 1

    def stage_ext_saiverse(self, world: sqlite3.Connection) -> None:
        """extensions/saiverse/saiverse_db/ を取り込む (SAIVERSE_DB_TABLE_OWNERS の表だけ)。

        表は SAIVERSE_DB_TABLE_OWNERS の並び (親が子より先) で処理する。持ち主の列は
        ZIP の値によらず取り込み先のペルソナ ID にする。子表 ("parent") は、親
        テーブルの行のうち ZIP にあって、取り込み先でこのペルソナの行になっているもの
        (今回入れた行と、前の取り込みで入った行) を指す行だけ。addon_message_metadata
        ("message") は self.message_ids 宛ての行だけ。文字列の主キーと参照の列は
        WORLD_STRING_KEY_TABLES の写像で振り直し (decode_world_row)、単一列の整数の
        主キーは取り込み先の連番で振り直す。
        """
        pack_tables = self.pack.ext_saiverse_tables
        for table in sorted(set(pack_tables) - set(SAIVERSE_DB_TABLE_OWNERS)):
            rows = pack_tables[table]
            self.result.count(f"extensions/saiverse_db/{table}").skipped += len(rows)
            self.warn(
                f"extensions/saiverse/saiverse_db/{table}.json is not a table a persona pack "
                f"carries; {len(rows)} row(s) not imported"
            )
        for table, rule in SAIVERSE_DB_TABLE_OWNERS.items():
            if table not in pack_tables:
                continue
            rows = pack_tables[table]
            counts = self.result.count(f"extensions/saiverse_db/{table}")
            if not rows:
                continue
            if not self.table_exists(world, table):
                counts.skipped += len(rows)
                self.warn(f"saiverse.db has no table {table!r}; {len(rows)} row(s) skipped")
                continue
            column = rule[1]
            if column not in self.columns(world, table):
                counts.skipped += len(rows)
                self.warn(f"saiverse.db table {table!r} has no column {column!r}; {len(rows)} row(s) skipped")
                continue
            allowed: Optional[Set[Any]] = None
            if rule[0] == "parent":
                parent_table, parent_key = rule[2], rule[3]
                parent_owner = SAIVERSE_DB_TABLE_OWNERS[parent_table][1]
                # 親の主キーが振り直す列なら、取り込み先の値で照合する (子の参照の列も
                # 同じ写像で付け替えてから比べる)
                parent_rekeyed = WORLD_STRING_KEY_TABLES.get(parent_table, ("",))[0] == parent_key
                zip_parents = {
                    self.world_key(parent_table, decode_value(r.get(parent_key))) if parent_rekeyed
                    else decode_value(r.get(parent_key))
                    for r in pack_tables.get(parent_table, [])
                }
                allowed = set()
                if zip_parents and self.table_exists(world, parent_table):
                    keys = [k for k in zip_parents if k is not None]
                    for i in range(0, len(keys), _CHUNK):
                        part = keys[i:i + _CHUNK]
                        allowed.update(r[0] for r in world.execute(
                            f"SELECT {_quote(parent_key)} FROM {_quote(parent_table)} "
                            f"WHERE {_quote(parent_owner)} = ? "
                            f"AND {_quote(parent_key)} IN ({', '.join('?' for _ in part)})",
                            [self.target, *part],
                        ))
            elif rule[0] == "message":
                allowed = self.message_ids
            refused = 0
            # 単一列の整数の主キーは世界全体の連番なので、取り込み先で振り直す。
            # 整数かどうかは INTEGER affinity (型名に "INT" を含む — SQLite の型
            # affinity の規則) で判定する。型名がちょうど INTEGER の列は rowid の別名
            # なので値を渡さず SQLite に振らせる。それ以外 (BIGINT など) は rowid の
            # 別名ではなく、値を渡さないと NULL が入るので、取り込み先の MAX+1 から振る。
            info = world.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
            pk_cols = [r for r in info if r[5]]
            pk_col: Optional[str] = None
            next_pk: Optional[int] = None
            if len(pk_cols) == 1 and "INT" in str(pk_cols[0][2]).upper():
                pk_col = pk_cols[0][1]
                if str(pk_cols[0][2]).strip().upper() != "INTEGER":
                    (top,) = world.execute(
                        f"SELECT COALESCE(MAX({_quote(pk_col)}), 0) FROM {_quote(table)}"
                    ).fetchone()
                    next_pk = int(top) + 1
            for row in rows:
                decoded = self.decode_world_row(table, row, skip=pk_col)
                if rule[0] == "owner":
                    decoded[column] = self.target
                elif decoded.get(column) not in allowed:
                    refused += 1
                    counts.skipped += 1
                    continue
                if next_pk is not None:
                    decoded[pk_col] = next_pk
                    next_pk += 1
                cur = self.insert(world, table, decoded, or_ignore=True)
                if cur.rowcount:
                    counts.added += 1
                else:
                    counts.skipped += 1
            if refused:
                self.warn(
                    f"extensions/saiverse_db/{table}: {refused} row(s) not imported; their "
                    f"{column} does not point to this persona's imported rows"
                )

    def decode_world_row(
        self, table: str, row: Dict[str, Any], *, skip: Optional[str] = None
    ) -> Dict[str, Any]:
        """extensions/saiverse/saiverse_db/ の 1 行を取り込み先の値にする (列 skip は落とす)。

        WORLD_STRING_KEY_TABLES の主キーと参照の列は world_key で振り直し、それ以外の
        列は remap_exact (元のペルソナ ID・既知のスレッド ID の置き換え) を通す。
        """
        key_cols: Dict[str, str] = {}
        spec = WORLD_STRING_KEY_TABLES.get(table)
        if spec is not None:
            key_cols[spec[0]] = table
            key_cols.update(dict(spec[1]))
        out: Dict[str, Any] = {}
        for key, value in row.items():
            if key == skip:
                continue
            value = decode_value(value)
            out[key] = self.world_key(key_cols[key], value) if key in key_cols else self.remap_exact(value)
        return out

    def stage_persona_dir_files(self) -> None:
        files = self.pack.ext_persona_files
        if not files:
            return
        counts = self.result.count("extensions/persona_dir")
        for zip_name, rel in files:
            if (
                (len(rel.parts) == 1 and (rel.name in _MEMORY_DB_FILES or rel.name == WORLD_PENDING_MARKER))
                or rel.parts[0] == _DAY_REPORTS_DIR
            ):
                counts.skipped += 1
                self.warn(f"{zip_name}: not restored (reserved name in the persona folder)")
                continue
            self.plan_keep_existing(zip_name, self.persona_dir.joinpath(*rel.parts), counts)

    # --- items/ (世界への登録) ----------------------------------------------------

    def stage_items(self, world: sqlite3.Connection) -> None:
        """私室の見た目と設定・fixture・所持品を世界へ登録する (モジュール docstring)。

        内訳は ``items`` 一つにまとめる: added = 登録した item と fixture の行数、
        updated = 私室の設定を写したら 1、skipped = 登録しなかった item と fixture の行数
        (同じパックで登録済み・置き場所を対応づけられない・私室が無い)。
        """
        pack = self.pack
        if pack.private_room is None and pack.items is None:
            return
        counts = self.result.count("items")
        room_id: Optional[str] = None
        row = world.execute("SELECT PRIVATE_ROOM_ID FROM ai WHERE AIID = ?", (self.target,)).fetchone()
        if row and row[0] and world.execute(
            "SELECT 1 FROM building WHERE BUILDINGID = ?", (row[0],)
        ).fetchone():
            room_id = row[0]
        zip_room_id: Optional[str] = None
        if pack.private_room is not None:
            zip_room_id = decode_value(pack.private_room["building"].get("BUILDINGID"))
            if room_id is None:
                counts.skipped += len(pack.private_room.get("fixtures") or [])
                self.warn(f"{self.target} has no private room in saiverse.db; the private room was not restored")
            else:
                self.restore_private_room(world, room_id, counts)
        if pack.items is not None:
            self.restore_items(world, room_id, zip_room_id, counts)

    def plan_media_restore(self, zip_name: str, media_dir: str) -> str:
        """ZIP のファイルを共有メディアフォルダ media_dir へ置く計画を立て、
        SAIVERSE_HOME からの相対パス (manager/items.py が FILE_PATH に書く形) を返す。

        同名で中身の同じファイルがあれば再利用する。``<名前>.summary.txt`` が ZIP の
        隣にあれば一緒に置く。
        """
        name = PurePosixPath(zip_name).name
        _safe_relpath(name, zip_name)
        data_hash = _sha256_bytes(self.reader.read(zip_name))
        summary_zip: Optional[str] = zip_name + _SUMMARY_SUFFIX
        if summary_zip not in self.reader.names:
            summary_zip = None
        dest, reused = self.choose_media_dest(self.home / media_dir, name, data_hash, summary_zip is not None)
        if not reused:
            self.plan_file(zip_name, dest)
        if summary_zip is not None:
            summary_dest = dest.with_name(dest.name + _SUMMARY_SUFFIX)
            if not summary_dest.exists() and not self.is_reserved(summary_dest):
                self.plan_file(summary_zip, summary_dest)
        return f"{media_dir}/{dest.name}"

    def derived_id(self, kind: str, source_id: str) -> str:
        """取り込み先のペルソナと元の ID から決まる ID (uuid5)。

        kind は ``"item"`` / ``"fixture"`` / ``"task_book"`` か、WORLD_STRING_KEY_TABLES の表の名前。
        """
        return str(uuid.uuid5(_ID_NAMESPACE, f"{self.target}/{kind}/{source_id}"))

    def restore_private_room(self, world: sqlite3.Connection, room_id: str, counts: ImportCounts) -> None:
        room_doc = self.pack.private_room or {}
        room = _decode_row(room_doc["building"])
        files = room_doc.get("files") or {}
        cols = set(self.columns(world, "building"))
        updates: Dict[str, Any] = {
            col: room[col] for col in ROOM_COPIED_COLUMNS
            if col in cols and col in room and room[col] is not None
        }
        name = updates.get("BUILDINGNAME")
        if name is not None and world.execute(
            "SELECT 1 FROM building WHERE BUILDINGNAME = ? AND BUILDINGID <> ? "
            "AND CITYID = (SELECT CITYID FROM building WHERE BUILDINGID = ?)",
            (name, room_id, room_id),
        ).fetchone():
            del updates["BUILDINGNAME"]
            self.warn(f"private room: another building in this city is named {name!r}; the room name was kept")
        if "IMAGE_PATH" in cols:
            image_zip = files.get("IMAGE_PATH")
            if image_zip:
                updates["IMAGE_PATH"] = self.plan_media_restore(image_zip, "image")
            elif room.get("IMAGE_PATH"):
                self.warn("private room: the image file is not in the pack; IMAGE_PATH left unchanged")
        if updates:
            world.execute(
                f"UPDATE building SET {', '.join(f'{_quote(c)} = ?' for c in updates)} WHERE BUILDINGID = ?",
                [*updates.values(), room_id],
            )
        counts.updated += 1

        fixtures = room_doc.get("fixtures") or []
        if not fixtures:
            return
        if not self.table_exists(world, "fixture"):
            counts.skipped += len(fixtures)
            self.warn("saiverse.db has no fixture table; fixtures skipped")
            return
        fixture_files = files.get("fixtures") or {}
        for raw in fixtures:
            fx = _decode_row(raw)
            source_fid = fx["FIXTURE_ID"]
            fid = source_fid
            existing = world.execute(
                "SELECT BUILDING_ID FROM fixture WHERE FIXTURE_ID = ?", (fid,)
            ).fetchone()
            if existing is not None and existing[0] != room_id:
                # 別の部屋の fixture と ID が衝突する: 振り直す
                fid = self.derived_id("fixture", source_fid)
                existing = world.execute(
                    "SELECT BUILDING_ID FROM fixture WHERE FIXTURE_ID = ?", (fid,)
                ).fetchone()
            if existing is not None:
                counts.skipped += 1  # 同じパックで登録済み
                continue
            fx["FIXTURE_ID"] = fid
            fx["BUILDING_ID"] = room_id
            fx["CREATOR_ID"] = self.remap_exact(fx.get("CREATOR_ID"))
            zip_file = fixture_files.get(source_fid)
            if zip_file:
                fx["FILE_PATH"] = self.plan_media_restore(
                    zip_file, _media_dir_for(fx.get("FILE_PATH"), fx.get("TYPE"))
                )
            elif fx.get("FILE_PATH"):
                self.warn(f"fixture {fx.get('NAME')!r}: its file is not in the pack; FILE_PATH cleared")
                fx["FILE_PATH"] = None
            self.insert(world, "fixture", fx)
            counts.added += 1

    def restore_items(
        self,
        world: sqlite3.Connection,
        room_id: Optional[str],
        zip_room_id: Optional[str],
        counts: ImportCounts,
    ) -> None:
        doc = self.pack.items or {}
        items: Dict[str, Dict[str, Any]] = {}
        for raw in doc["items"]:
            items.setdefault(raw["ITEM_ID"], _decode_row(raw))
        if not items:
            return
        if not (self.table_exists(world, "item") and self.table_exists(world, "item_location")):
            counts.skipped += len(items)
            self.warn("saiverse.db has no item / item_location table; items skipped")
            return
        new_ids = {iid: self.derived_id("item", iid) for iid in items}

        # 置き場所 (1 item 1 行: UNIQUE(ITEM_ID)) を取り込み先の持ち主へ対応づける。
        # 入れ物 (OWNER_KIND='bag') の中身は、入れ物が登録できるときだけ登録する
        locations: Dict[str, Dict[str, Any]] = {}
        for raw in doc["locations"]:
            locations.setdefault(raw["ITEM_ID"], _decode_row(raw))
        placed: Dict[str, Tuple[str, str]] = {}
        unplaced: Set[str] = set()
        pending = {iid for iid in items if iid in locations}
        changed = True
        while changed:
            changed = False
            for iid in sorted(pending):
                loc = locations[iid]
                kind, owner = loc["OWNER_KIND"], loc["OWNER_ID"]
                target_owner: Optional[str] = None
                if kind == "persona" and owner == self.source:
                    target_owner = self.target
                elif kind == "building" and owner == zip_room_id and room_id is not None:
                    target_owner = room_id
                elif kind == "bag" and owner in items:
                    if owner in placed:
                        target_owner = new_ids[owner]
                    elif owner not in unplaced and owner in pending:
                        continue  # 入れ物の判定待ち
                if target_owner is None:
                    unplaced.add(iid)
                else:
                    placed[iid] = (kind, target_owner)
                pending.discard(iid)
                changed = True
        unplaced.update(pending)  # 入れ物の連鎖が輪になっているもの
        unplaced.update(iid for iid in items if iid not in locations)
        if unplaced:
            counts.skipped += len(unplaced)
            self.warn(
                f"items: {len(unplaced)} item(s) not imported; their place (persona, private room "
                "or container) could not be matched in this world"
            )

        present = self.existing_keys(world, "item", "ITEM_ID", (new_ids[i] for i in placed))
        counts.skipped += sum(1 for i in placed if new_ids[i] in present)  # 同じパックで登録済み
        # 入れ物を中身より先に入れる (placed は判定の順 = 入れ物が先)
        to_insert = [(iid, kind_owner) for iid, kind_owner in placed.items() if new_ids[iid] not in present]
        # SHORT_ID (item:N) は世界全体の連番 (database/models.py の _assign_item_short_id が
        # MAX+1 で振る)。intent §5 の短縮番号の規則で、元の番号が取り込み先の MAX より
        # 大きければそのまま、それ以外と番号の無い行は MAX+1 から振る
        (col_max,) = world.execute("SELECT COALESCE(MAX(SHORT_ID), 0) FROM item").fetchone()
        originals = [_as_int(items[iid].get("SHORT_ID")) for iid, _ in to_insert]
        planned = _plan_numbers(originals, int(col_max or 0))
        top = max([int(col_max or 0), *(n for n in planned if n is not None)])
        for i, n in enumerate(planned):
            if n is None:
                top += 1
                planned[i] = top
        renumbered = sum(1 for o, n in zip(originals, planned) if o is not None and o != n)
        occupied: Dict[Tuple[str, str], Set[int]] = {}
        files = doc.get("files") or {}
        for (iid, (kind, owner)), short_id in zip(to_insert, planned):
            new_id = new_ids[iid]
            item = dict(items[iid])
            item["ITEM_ID"] = new_id
            item["SHORT_ID"] = short_id
            item["CREATOR_ID"] = self.remap_exact(item.get("CREATOR_ID"))
            zip_file = files.get(iid)
            if zip_file:
                item["FILE_PATH"] = self.plan_media_restore(
                    zip_file, _media_dir_for(item.get("FILE_PATH"), item.get("TYPE"))
                )
            elif item.get("FILE_PATH"):
                self.warn(f"item {item.get('NAME')!r}: its file is not in the pack; FILE_PATH cleared")
                item["FILE_PATH"] = None
            self.insert(world, "item", item)

            loc = dict(locations[iid])
            loc.pop("LOCATION_ID", None)
            key = (kind, owner)
            if key not in occupied:
                occupied[key] = {
                    r[0] for r in world.execute(
                        "SELECT SLOT_NUMBER FROM item_location WHERE OWNER_KIND = ? AND OWNER_ID = ? "
                        "AND SLOT_NUMBER IS NOT NULL",
                        key,
                    )
                }
            slot = _as_int(loc.get("SLOT_NUMBER"))
            if slot is None or slot in occupied[key]:
                slot = 1
                while slot in occupied[key]:
                    slot += 1
            occupied[key].add(slot)
            loc.update({"ITEM_ID": new_id, "OWNER_KIND": kind, "OWNER_ID": owner, "SLOT_NUMBER": slot})
            self.insert(world, "item_location", loc)
            counts.added += 1
        if renumbered:
            self.result.renumbered["items.short_id"] = self.result.renumbered.get("items.short_id", 0) + renumbered

    def plan_keep_existing(self, zip_name: str, dest: Path, counts: ImportCounts) -> None:
        """既存のファイルは上書きしない (同じ中身ならスキップ、違えば警告してスキップ)。"""
        if self.is_reserved(dest):
            counts.skipped += 1
            return
        if dest.exists():
            counts.skipped += 1
            if not (dest.is_file() and _sha256_file(dest) == _sha256_bytes(self.reader.read(zip_name))):
                self.warn(f"{zip_name}: {dest} already exists with different content; not overwritten")
            return
        self.plan_file(zip_name, dest)
        counts.added += 1

    def stage_merge_recall_edges(self, conn: sqlite3.Connection) -> None:
        """合流で取り込んだ chronicle ページの chunk_page_edges を入れる。

        extensions/ は合流では取り込まない (intent §5) が、この表は取り込んだ
        chronicle ページから実体ページへの想起用タグの辺 (sai_memory/memory/
        recall_edges.py) で、取り込んだページと一緒でないと意味を持たない。
        両端 (chronicle_page_id が今回取り込んだページ、entity_page_id が取り込み先に
        あるページ) が揃う行だけを INSERT OR IGNORE で入れる。
        """
        table = "chunk_page_edges"
        rows = self.pack.ext_memory_tables.get(table)
        if not rows:
            return
        self._merged_ext_tables.add(table)
        counts = self.result.count(f"extensions/memory_db/{table}")
        if not self.imported_chronicle_ids or not self.table_exists(conn, table):
            counts.skipped += len(rows)
            return
        decoded = [{k: decode_value(v) for k, v in row.items()} for row in rows]
        entity_ids = self.existing_keys(
            conn, "memopedia_pages", "id",
            (r.get("entity_page_id") for r in decoded
             if r.get("chronicle_page_id") in self.imported_chronicle_ids),
        )
        for row in decoded:
            chronicle_id = row.get("chronicle_page_id")
            entity_id = row.get("entity_page_id")
            created_at = row.get("created_at")
            if (
                chronicle_id not in self.imported_chronicle_ids
                or entity_id not in entity_ids
                or isinstance(created_at, bool)
                or not isinstance(created_at, int)
            ):
                counts.skipped += 1
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO chunk_page_edges("
                "chronicle_page_id, entity_page_id, created_at) VALUES (?, ?, ?)",
                (chronicle_id, entity_id, created_at),
            )
            if cur.rowcount:
                counts.added += 1
            else:
                counts.skipped += 1

    def count_merge_skips(self) -> None:
        """合流では items/ と extensions/ を取り込まない (intent §5)。件数だけ載せる。

        stage_merge_recall_edges が扱った表と、印による再開で取り込んだもの
        (resume_world の extensions/saiverse/saiverse_db/・items/、resume_memory の
        extensions/saiverse/memory_db/・extensions/saiverse/persona_dir/) は、そちらが
        件数を載せている。
        """
        pack = self.pack
        if not self.resume_world:
            n_items = len((pack.items or {}).get("items") or [])
            n_items += len((pack.private_room or {}).get("fixtures") or [])
            if n_items:
                self.result.count("items").skipped += n_items
            for table, rows in pack.ext_saiverse_tables.items():
                self.result.count(f"extensions/saiverse_db/{table}").skipped += len(rows)
        if self.resume_memory:
            return
        self.report_refused_ext_memory_tables()
        for table, rows in pack.ext_memory_tables.items():
            if table in self._merged_ext_tables:
                continue
            self.result.count(f"extensions/memory_db/{table}").skipped += len(rows)
        if pack.ext_persona_files:
            self.result.count("extensions/persona_dir").skipped += len(pack.ext_persona_files)
