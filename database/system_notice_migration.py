"""システム通知の旧形式データを新形式へ正規化する一回限りの移行。

docs/intent/system_notice_delivery.md「設計 > 移行」。供給側 (第 1 束) と提示
(第 3 束) は、システム通知を role=system・タグ無し本文 + 識別用の metadata
``__system_notice__`` (``sai_memory.memory.storage.SYSTEM_NOTICE_KEY``) で扱う。
それより前に保存された行には二つの旧形式が残っている:

1. 本文に ``<system>`` タグを埋め込んだ role=user (一部 role=assistant) の行。
   **本文が ``<system>`` で始まる** かつ **その系統の識別用 metadata を持つ**
   行だけを、タグを剥がして role=system + ``__system_notice__`` にする
   (AND 判定、不変条件 2・4)。識別用 metadata の無いタグ始まりの行は触らない
   (外部由来の偽物は role=user のまま残れば、合言葉の規則のもとで「ただの
   文章」として読まれる)。
2. 旧世界で role=system・タグ無しで保存された行 (スペルの結果・TOOL の結果・
   Handy Tool・権限拒否など)。``__system_notice__`` が無いので
   ``is_system_notice`` が偽になり、自動想起の種などから外れる。tags から系統を
   推定できるものに印を付ける。推定できない行は触らない。

ほかに、同じ本文の形を持つ列・表を最終形へ揃える: messages の
``paired_action_text`` 列、``perception_batches`` (kind=``realtime_and_recall``)
の ``rendered_text``、本体 DB の ``building_messages`` (音声通話の痕跡が
role=user + タグで載った世代)、``execution_ledger`` / ``execution_outbox`` に
凍結された本文 (まだ送り直し・記録し直しがありうる行だけ)。

「本文が ``<system>`` で始まるか」を意味の判定に使うのはこの移行の一回だけ
(不変条件 2 の例外)。常設のコードからこのモジュールを呼ばない。

起動時の入口は :func:`run_startup_system_notice_migration` (main.py が
ペルソナの読み込みより前に呼ぶ)。ペルソナごとの記憶 DB は ``embed_metadata``
の :data:`MIGRATION_MARKER_KEY` で一回限りにし、書き換える前にその DB の
写しを ``<SAIVERSE_HOME>/backups/system_notice_migration/`` に取る (通常の
起動時バックアップは数が上限を超えると古いものから消えるので、この写しは
別の置き場に置く)。移行の結果は同じ置き場にレポートとして書き出す — 本番へ
当てたあとに人の目で確かめるためのもの。レポートの材料は DB ごとの結果の
JSON (``results/<時刻>_<pid>_<乱数>/``) で、書き換えのコミットの後に書くので、
レポートの書き出しが失敗しても次の起動で作り直せる。

コピーで試すときは CLI を使う::

    python -m database.system_notice_migration --home <SAIVERSE_HOME の写し> --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

LOGGER = logging.getLogger(__name__)

SYSTEM_OPEN = "<system>"
SYSTEM_CLOSE = "</system>"

#: ペルソナの記憶 DB の ``embed_metadata`` に書く「この移行は済んだ」の印。
#: 値は完了時刻 (ISO)。置き場と判定 (キーがあれば済み) は saiverse/
#: v3_shape_migration.py の ``MARKER_KEY`` と同じ流儀 — 書き込み先と同じ DB に
#: 置くので、memory.db を差し替え・復元しても「移行したか」と中身がずれない。
MIGRATION_MARKER_KEY = "system_notice_migration_done"

#: 写しとレポートの置き場 (``<SAIVERSE_HOME>/backups/`` の下)。
OUTPUT_DIR_NAME = "system_notice_migration"

#: レポートに載せる系統ごとのサンプル数・本文の冒頭の長さ。
SAMPLES_PER_KIND = 3
HEAD_CHARS = 160
#: 推定できなかった role=system 行をレポートに並べる上限 (件数は全数を出す)。
UNKNOWN_SYSTEM_LIST_LIMIT = 50

#: 作業セッションのダイジェストのタグ (sea/work_session.py の ``DIGEST_TAG``)。
#: 旧データの判定なので、保存された当時の値をここに固定する。
_DIGEST_TAG = "session_digest"

#: 子ラインの結果報告の旧本文の書き出し (sea/runtime_nodes.py の
#: ``lg_subplay_node`` が組んでいた ``サブ Playbook '{name}' の実行結果:``)。
_SUBLINE_REPORT_PREFIX = "サブ Playbook '"


# ---------------------------------------------------------------------------
# 本文のタグを剥がす
# ---------------------------------------------------------------------------


def starts_with_system_tag(text: Any) -> bool:
    return isinstance(text, str) and text.startswith(SYSTEM_OPEN)


def strip_system_tags(text: str, *, pairs: Union[int, Tuple[int, ...]] = 1) -> Optional[str]:
    """外側の ``<system>…</system>`` の包みを外した本文。包みの形が曖昧なら None。

    剥がすのは、本文全体が「完全な ``<system>…</system>`` の組を空白だけを
    挟んで、``pairs`` のどれかの個数だけ並べたもの」になっている行だけ
    (``pairs`` は個数一つか、受け付ける個数の組)。各組の中身の前後の空白を
    落として空行 (``\\n\\n``) で繋ぐ。

    組の数は置き場ごとに呼び出し側が縛る。一つの包みの中に ``</system><system>``
    を含む引用が入った行 (旧外部イベントの通知が引用を包んだ形) と、正当に組を
    二つ並べた行は、文字の並びでは区別できない — 期待と違う数の組に見える行は
    決め打ちで剥がさず、曖昧としてレポートへ回す。

    - messages・paired_action_text・実行台帳・送信トレイ・building_messages は
      一つの通知を一つの包みで保存していたので ``pairs=1``。
    - ``perception_batches`` の ``rendered_text`` (kind=realtime_and_recall) は
      リアルタイム情報と想起の組を ``\\n`` で繋いだ旧形式で、旧
      ``build_turn_realtime_and_recall_message`` は片方しか無いターンでも
      その一つの組だけで保存した。だから ``pairs=(1, 2)``。剥がした結果は
      第 3 束の提示の最終形 (sea/runtime_context.
      compose_turn_realtime_and_recall_content — ある本文だけを ``\\n\\n`` で
      繋ぐ) と同じ形になる。

    組の中で開きと閉じが同数そろって入れ子になっているタグ (中断の再開文が元の
    発火文を引用している世代など) は本文の一部としてそのまま残す — 送るときに
    クライアント層が無害化する (llm_clients/system_notice.py)。

    次の形は None を返す (呼び出し側は変換せずレポートへ回す)。どれも、外部
    由来の本文 (建物のイベント文・メンションなど) に ``</system>`` や
    ``<system>`` が混ざっていた可能性があり、どこまでが包みかを機械的に決め
    られない — 決め打ちで剥がすと本文が変わる:

    - 対の無い閉じタグ (``<system>A </system> B</system>``)
    - 閉じられていない開きタグ (途中で切れた形、``<system>A <system>B</system>``)
    - 組の外に空白以外の文字がある (``<system>A</system> B``)
    - 完全な組の数が ``pairs`` と違う
    """
    segments: List[str] = []
    current: List[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        if text.startswith(SYSTEM_OPEN, i):
            if depth > 0:
                current.append(SYSTEM_OPEN)
            depth += 1
            i += len(SYSTEM_OPEN)
            continue
        if text.startswith(SYSTEM_CLOSE, i):
            if depth == 0:
                return None  # 対の無い閉じタグ
            depth -= 1
            if depth == 0:
                segments.append("".join(current))
                current = []
            else:
                current.append(SYSTEM_CLOSE)
            i += len(SYSTEM_CLOSE)
            continue
        if depth == 0:
            if not text[i].isspace():
                return None  # 組の外の地の文
        else:
            current.append(text[i])
        i += 1
    if depth != 0 or not segments:
        return None  # 閉じられていない開きタグ
    allowed = (pairs,) if isinstance(pairs, int) else tuple(pairs)
    if len(segments) not in allowed:
        return None  # 置き場が期待する組の数と違う (引用か正当な複数の組か決められない)
    return "\n\n".join(s.strip() for s in segments if s.strip())


# ---------------------------------------------------------------------------
# 系統の判定
# ---------------------------------------------------------------------------


def _tags_of(metadata: Dict[str, Any]) -> set:
    raw = metadata.get("tags")
    if not isinstance(raw, list):
        return set()
    return {t for t in raw if isinstance(t, str)}


def _semantic_tags(tags: set) -> set:
    """判定に使うタグ (全行に付く ``conversation`` と ``pulse:`` / ``playbook:`` を除く)。"""
    return {
        t for t in tags
        if t != "conversation" and not t.startswith("pulse:") and not t.startswith("playbook:")
    }


def _has_playbook_tag(tags: set) -> bool:
    return any(t.startswith("playbook:") for t in tags)


def classify_tagged_message(
    role: Any, content: Any, metadata: Dict[str, Any],
) -> Optional[str]:
    """タグ入り本文の行の系統名 (``__system_notice__`` の値)。対象外は None。

    判定は「本文が ``<system>`` で始まる」かつ「その系統の識別用 metadata」の
    AND (docs/handoff/2026-10-27_system_notice_survey.md「別枠: 13 系統」の表と、
    git 履歴に残る旧世代の供給側)。上から順に最初に当たった規則で決める。
    """
    if not starts_with_system_tag(content):
        return None
    role_s = role.lower() if isinstance(role, str) else ""
    tags = _tags_of(metadata)
    semantic = _semantic_tags(tags)

    if role_s in ("assistant", "model"):
        # 中断の記録 (2026-08 に撤去された機構) は role=assistant で
        # ``<system>(中断: …)</system>`` を書いていた。機構の文を本人の発言として
        # 残さない (発話の尊厳)。
        if "interrupted" in tags:
            return "interrupted"
        return None
    if role_s != "user":
        return None

    if "event_message" in tags:
        if "sluice" in tags:
            return "sluice_capture"
        if "day_plan" in tags:
            return "day_plan"
        if "curation" in tags:
            return "curation"
        if "perception" in tags:
            # W14 (知覚レンダリング) より前の知覚の行。今の知覚のまとめと同じ系統名。
            return "perception"
        if "version_upgrade" in tags:
            return "version_upgrade"
        if metadata.get("voice_call") is True:
            return "voice_call"
        if semantic == {"internal", "event_message"}:
            return "building_event"
        # event_message を持つが上のどれでもない世代。系統名は印のタグそのもの。
        return "event_message"
    if "sluice" in tags and _DIGEST_TAG in tags:
        return "sluice_digest"
    if "track_context" in tags:
        # Track 切替通知 (Track ランタイムの退役 2026-08 より前。event_message を
        # 持たない世代)。
        return "track_context"
    if "track_chronicle_insert" in tags:
        return "track_chronicle_insert"
    if "situation_change" in tags:
        return "situation_change"
    if metadata.get("with") == ["system"]:
        return "schedule"
    if _has_playbook_tag(tags):
        if (strip_system_tags(content) or "").startswith(_SUBLINE_REPORT_PREFIX):
            return "subline_report"
        return "playbook_memorize"
    return None


def classify_unmarked_system_message(metadata: Dict[str, Any]) -> Optional[str]:
    """印 (``__system_notice__``) の無い role=system 行の系統名。推定できなければ None。"""
    tags = _tags_of(metadata)
    if "spell" in tags or "pre_spell" in tags:
        return "spell_result"
    if "handy_tool" in tags:
        return "handy_tool"
    if {"permission", "denied"} <= tags:
        return "playbook_permission"
    if {"error", "exec"} <= tags:
        return "playbook_exec_error"
    if "tool" in tags:
        return "tool_result"
    if _has_playbook_tag(tags):
        return "playbook_memorize"
    return None


# ---------------------------------------------------------------------------
# 結果の器
# ---------------------------------------------------------------------------


@dataclass
class Change:
    """一行の変換 (レポートのサンプル用)。"""

    table: str
    row_id: str
    kind: str
    before: str
    after: str


@dataclass
class Skipped:
    """変換しなかったタグ始まりの行 (レポートに全件出す)。"""

    table: str
    row_id: str
    role: str
    tags: List[str]
    head: str
    #: 変換しなかった理由 (空 = 系統を判定できなかった)。
    reason: str = ""


#: :func:`strip_system_tags` が包みの形を決められなかった行の理由。
AMBIGUOUS_TAGS_REASON = "タグの対応が曖昧"
#: 確認待ちの印 (``SYSTEM_NOTICE_REVIEW_KEY``) の値: タグ入り本文だが系統を判定
#: できなかった行。
UNCLASSIFIED_REASON = "系統を判定できなかった"
#: 確認待ちの印の値: 印の無い role=system 行で系統を推定できなかった行。
UNKNOWN_SYSTEM_REASON = "系統を推定できなかった role=system"


@dataclass
class StoreResult:
    """一つの DB の移行結果。"""

    label: str
    path: str
    #: ペルソナの記憶 DB なら True (本体 DB は False)。
    persona: bool = False
    backup_path: Optional[str] = None
    changes: List[Change] = field(default_factory=list)
    skipped: List[Skipped] = field(default_factory=list)
    unknown_system_count: int = 0
    unknown_system_samples: List[Skipped] = field(default_factory=list)
    #: タグ無し本文の role=user で ``playbook:`` タグを持ち ``conversation`` タグの
    #: 無い行 (旧世界の MEMORIZE の結果の候補)。変換はせず、人の目で確かめる
    #: ためにレポートへ出す。
    untagged_memorize_count: int = 0
    untagged_memorize_samples: List[Skipped] = field(default_factory=list)
    #: 当てる UPDATE 文 (SQL, 引数)。走査は読むだけで、書くのは :func:`_apply`。
    updates: List[Tuple[str, tuple]] = field(default_factory=list)
    already_done: bool = False
    error: Optional[str] = None
    #: 結果の JSON から読み戻したときの系統別件数 (``changes`` はサンプルだけになる)。
    restored_counts: Optional[Dict[Tuple[str, str], int]] = None
    #: この起動でこの結果をレポートの材料 (結果の JSON) に書いたか。JSON には載せない。
    result_saved: bool = False
    #: この起動でこの結果を材料に書こうとして失敗したか (レポートから詳細が欠ける)。
    #: JSON には載せない。
    result_save_failed: bool = False

    def counts(self) -> Dict[Tuple[str, str], int]:
        if self.restored_counts is not None:
            return dict(self.restored_counts)
        out: Dict[Tuple[str, str], int] = {}
        for c in self.changes:
            out[(c.table, c.kind)] = out.get((c.table, c.kind), 0) + 1
        return out

    @property
    def has_findings(self) -> bool:
        return bool(
            self.changes or self.skipped or self.unknown_system_count
            or self.untagged_memorize_count or self.error
        )

    @property
    def has_leftovers(self) -> bool:
        """変換しなかった行・推定できなかった行・旧 MEMORIZE の候補があるか (人の確認が要る行)。"""
        return bool(self.skipped or self.unknown_system_count or self.untagged_memorize_count)

    def leftover_counts(self) -> List[int]:
        return [len(self.skipped), self.unknown_system_count, self.untagged_memorize_count]

    def leftover_keys(self) -> List[str]:
        """残した行の同定 (表・行 id・role・残した理由) の並び。前回との比較に使う。

        件数だけで比べると、一行が片づいて別の一行が増えた入れ替わりを見逃す。
        推定できなかった行と旧 MEMORIZE の候補は先頭の一覧に載る分だけを行で
        同定し、件数も並びに含める。
        """
        keys = [
            f"skipped|{s.table}|{s.row_id}|{s.role}|{s.reason}" for s in self.skipped
        ]
        keys += [f"unknown_system|{s.table}|{s.row_id}" for s in self.unknown_system_samples]
        keys += [f"memorize|{s.table}|{s.row_id}" for s in self.untagged_memorize_samples]
        keys += [
            f"count|unknown_system|{self.unknown_system_count}",
            f"count|memorize|{self.untagged_memorize_count}",
        ]
        return sorted(keys)

    def to_json(self) -> Dict[str, Any]:
        """レポートを組み直せるだけの中身 (件数・サンプル・残した行) を JSON にできる形で。"""
        from dataclasses import asdict

        return {
            "label": self.label,
            "path": self.path,
            "persona": self.persona,
            "backup_path": self.backup_path,
            "already_done": self.already_done,
            "error": self.error,
            "counts": [[t, k, n] for (t, k), n in sorted(self.counts().items())],
            "samples": [asdict(c) for c in _sample_changes(self.changes)],
            "skipped": [asdict(s) for s in self.skipped],
            "unknown_system_count": self.unknown_system_count,
            "unknown_system_samples": [asdict(s) for s in self.unknown_system_samples],
            "untagged_memorize_count": self.untagged_memorize_count,
            "untagged_memorize_samples": [asdict(s) for s in self.untagged_memorize_samples],
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "StoreResult":
        return cls(
            label=data["label"],
            path=data["path"],
            persona=bool(data.get("persona")),
            backup_path=data.get("backup_path"),
            already_done=bool(data.get("already_done")),
            error=data.get("error"),
            restored_counts={(t, k): n for t, k, n in data.get("counts") or []},
            changes=[Change(**c) for c in data.get("samples") or []],
            skipped=[Skipped(**s) for s in data.get("skipped") or []],
            unknown_system_count=int(data.get("unknown_system_count") or 0),
            unknown_system_samples=[Skipped(**s) for s in data.get("unknown_system_samples") or []],
            untagged_memorize_count=int(data.get("untagged_memorize_count") or 0),
            untagged_memorize_samples=[
                Skipped(**s) for s in data.get("untagged_memorize_samples") or []
            ],
        )


def _sample_changes(changes: Sequence[Change]) -> List[Change]:
    """レポートに載せるサンプル (表と系統の組ごとに先頭 :data:`SAMPLES_PER_KIND` 件)。"""
    seen: Dict[Tuple[str, str], int] = {}
    out: List[Change] = []
    for c in changes:
        key = (c.table, c.kind)
        if seen.get(key, 0) >= SAMPLES_PER_KIND:
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(c)
    return out


def _head(text: Any) -> str:
    s = str(text or "").replace("\r", "")
    s = s.replace("\n", "\\n")
    return s if len(s) <= HEAD_CHARS else s[:HEAD_CHARS] + "…"


def _load_json(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _col_or_null(cols: set, name: str) -> str:
    """SELECT に書く列の式。古いスキーマで列が無ければ NULL として読む。

    移行の判定は読み取り専用で init_db (列の追加) より前に走るので、列の足りない
    古い記憶 DB でも走査が例外にならないようにする。
    """
    return name if name in cols else f"NULL AS {name}"


# ---------------------------------------------------------------------------
# ペルソナの記憶 DB
# ---------------------------------------------------------------------------


def _mark_for_review(
    result: StoreResult, row_id: Any, raw_meta: Any, metadata: Dict[str, Any], reason: str,
) -> None:
    """レポート送りにした messages の行へ、確認待ちの印を付ける UPDATE を積む。

    印 (``SYSTEM_NOTICE_REVIEW_KEY``) は本文と role を変えず、その行を「実会話」
    (sai_memory/memory/storage.py の ``_conversation_exclusion``) から外す。既に
    印がある行には積まない (印を消して流し直しても二度書かない)。metadata が
    辞書として読めない行 (壊れた JSON・配列) には付けない — 印だけの metadata に
    置き換えると元の metadata が失われる。metadata の列が無い古いスキーマでは
    書けないので、呼び出し側が呼ばない。
    """
    from sai_memory.memory.storage import SYSTEM_NOTICE_REVIEW_KEY

    if raw_meta and _parse_json_object(raw_meta) is None:
        return
    if metadata.get(SYSTEM_NOTICE_REVIEW_KEY):
        return
    new_meta = dict(metadata)
    new_meta[SYSTEM_NOTICE_REVIEW_KEY] = reason
    result.updates.append((
        "UPDATE messages SET metadata = ? WHERE id = ?",
        (_dump_json(new_meta), row_id),
    ))


def _parse_json_object(raw: Any) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def scan_persona_conn(conn: sqlite3.Connection, result: StoreResult) -> None:
    """ペルソナの記憶 DB を読んで、変換を ``result`` に積む (書かない)。

    - messages: タグ入り本文の行 (判定は :func:`classify_tagged_message`) と、
      印の無い role=system 行 (:func:`classify_unmarked_system_message`)。
    - messages.paired_action_text: 列そのものが機構だけが書く指示文の控えなので、
      タグで始まる値は剥がす。
    - perception_batches (kind=realtime_and_recall).rendered_text。
    """
    from sai_memory.memory.storage import SYSTEM_NOTICE_KEY

    if not _table_exists(conn, "messages"):
        return
    cols = _columns(conn, "messages")
    if not {"id", "role", "content"} <= cols:
        # 判定に要る列が無い DB は対象が無い (変換もしない)
        return
    meta_col = _col_or_null(cols, "metadata")
    # 確認待ちの印は metadata の列に書く (列の無い古いスキーマでは付けない)
    can_mark = "metadata" in cols
    order = "created_at, rowid" if "created_at" in cols else "rowid"
    rows = conn.execute(
        f"SELECT id, role, content, {meta_col} FROM messages "
        "WHERE substr(content, 1, ?) = ? OR role = 'system' "
        f"ORDER BY {order}",
        (len(SYSTEM_OPEN), SYSTEM_OPEN),
    ).fetchall()
    for row_id, role, content, raw_meta in rows:
        metadata = _load_json(raw_meta)
        role_s = role.lower() if isinstance(role, str) else ""
        if starts_with_system_tag(content):
            kind = classify_tagged_message(role, content, metadata)
            body = strip_system_tags(content) if kind is not None else None
            if kind is None or body is None:
                result.skipped.append(Skipped(
                    "messages", str(row_id), str(role),
                    sorted(_tags_of(metadata)), _head(content),
                    reason="" if kind is None else AMBIGUOUS_TAGS_REASON,
                ))
                if can_mark:
                    _mark_for_review(
                        result, row_id, raw_meta, metadata,
                        UNCLASSIFIED_REASON if kind is None else AMBIGUOUS_TAGS_REASON,
                    )
                continue
            new_meta = dict(metadata)
            new_meta[SYSTEM_NOTICE_KEY] = kind
            result.updates.append((
                "UPDATE messages SET role = 'system', content = ?, metadata = ? "
                "WHERE id = ?",
                (body, _dump_json(new_meta), row_id),
            ))
            result.changes.append(Change("messages", str(row_id), kind, content, body))
            continue
        if role_s != "system" or metadata.get(SYSTEM_NOTICE_KEY):
            continue
        kind = classify_unmarked_system_message(metadata)
        if kind is None:
            result.unknown_system_count += 1
            if len(result.unknown_system_samples) < UNKNOWN_SYSTEM_LIST_LIMIT:
                result.unknown_system_samples.append(Skipped(
                    "messages", str(row_id), str(role),
                    sorted(_tags_of(metadata)), _head(content),
                ))
            if can_mark:
                _mark_for_review(result, row_id, raw_meta, metadata, UNKNOWN_SYSTEM_REASON)
            continue
        new_meta = dict(metadata)
        new_meta[SYSTEM_NOTICE_KEY] = kind
        result.updates.append((
            "UPDATE messages SET metadata = ? WHERE id = ?",
            (_dump_json(new_meta), row_id),
        ))
        result.changes.append(Change(
            "messages(印の付与)", str(row_id), kind, str(content or ""), str(content or ""),
        ))

    # 旧世界の MEMORIZE の結果 (building_move / document_create / document_search /
    # generate_image など) は role=user・タグ無しの本文で保存された世代がある。
    # 発話でないことの来歴を機械的に確定できないので変換はせず、候補を数えて
    # レポートに出す。
    memorize_rows = conn.execute(
        "SELECT id, content, metadata FROM messages "
        "WHERE role = 'user' AND metadata LIKE '%playbook:%' "
        f"ORDER BY {order}",
    ).fetchall() if "metadata" in cols else []
    for row_id, content, raw_meta in memorize_rows:
        if starts_with_system_tag(content):
            continue
        tags = _tags_of(_load_json(raw_meta))
        if not _has_playbook_tag(tags) or "conversation" in tags:
            continue
        result.untagged_memorize_count += 1
        if len(result.untagged_memorize_samples) < UNKNOWN_SYSTEM_LIST_LIMIT:
            result.untagged_memorize_samples.append(Skipped(
                "messages", str(row_id), "user", sorted(tags), _head(content),
            ))

    if "paired_action_text" in cols:
        for row_id, text in conn.execute(
            "SELECT id, paired_action_text FROM messages "
            "WHERE substr(paired_action_text, 1, ?) = ?",
            (len(SYSTEM_OPEN), SYSTEM_OPEN),
        ).fetchall():
            body = strip_system_tags(text)
            if body is None:
                result.skipped.append(Skipped(
                    "messages.paired_action_text", str(row_id), "", [], _head(text),
                    reason=AMBIGUOUS_TAGS_REASON,
                ))
                continue
            result.updates.append((
                "UPDATE messages SET paired_action_text = ? WHERE id = ?",
                (body, row_id),
            ))
            result.changes.append(Change(
                "messages.paired_action_text", str(row_id), "paired_action", text, body,
            ))

    if _table_exists(conn, "perception_batches") and {"id", "rendered_text"} <= _columns(
        conn, "perception_batches",
    ):
        batch_cols = _columns(conn, "perception_batches")
        kind_expr = "kind" if "kind" in batch_cols else "'perception'"
        for batch_id, batch_kind, text in conn.execute(
            f"SELECT id, {kind_expr}, rendered_text FROM perception_batches "
            "WHERE substr(rendered_text, 1, ?) = ?",
            (len(SYSTEM_OPEN), SYSTEM_OPEN),
        ).fetchall():
            if batch_kind != "realtime_and_recall":
                result.skipped.append(Skipped(
                    "perception_batches", str(batch_id), str(batch_kind), [], _head(text),
                ))
                continue
            # リアルタイム情報と想起の組を並べた形 (片方だけのターンは組が一つ) を剥がす
            body = strip_system_tags(text, pairs=(1, 2))
            if body is None:
                result.skipped.append(Skipped(
                    "perception_batches", str(batch_id), str(batch_kind), [], _head(text),
                    reason=AMBIGUOUS_TAGS_REASON,
                ))
                continue
            result.updates.append((
                "UPDATE perception_batches SET rendered_text = ? WHERE id = ?",
                (body, batch_id),
            ))
            result.changes.append(Change(
                "perception_batches", str(batch_id), "realtime_and_recall", text, body,
            ))


def _persona_marker_set(conn: sqlite3.Connection) -> bool:
    from sai_memory.memory.storage import get_embed_metadata

    return get_embed_metadata(conn, MIGRATION_MARKER_KEY) is not None


def _set_persona_marker(conn: sqlite3.Connection) -> None:
    """取引の中で完了の印を書く (コミットは呼び出し側)。

    ``embed_metadata`` の無い古い記憶 DB では、表を同じ形で作る (sai_memory/
    memory/storage.py の init_db の ``CREATE TABLE IF NOT EXISTS embed_metadata``
    と同じ定義)。init_db は呼ばない — init_db は列の追加と既存行の埋め直しを
    自分でコミットするので、書き込みのロックの中では呼べず、ロックの外で呼ぶと
    写しが「触る前」の状態でなくなる。
    """
    from sai_memory.memory.storage import set_embed_metadata

    conn.execute(
        "CREATE TABLE IF NOT EXISTS embed_metadata ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    set_embed_metadata(
        conn, MIGRATION_MARKER_KEY, datetime.now(timezone.utc).isoformat(), commit=False,
    )


def _apply(conn: sqlite3.Connection, updates: Sequence[Tuple[str, tuple]]) -> None:
    for sql, params in updates:
        conn.execute(sql, params)


def _snapshot(src: Path, dest: Path) -> None:
    """SQLite のバックアップ API で ``src`` の写しを ``dest`` に作る。

    ``dest`` は新しく作る (既にあれば FileExistsError) — 同じ秒に起動した別の
    プロセスの写しを上書きしない。

    呼び出し側は ``src`` の書き込みの取引 (``BEGIN IMMEDIATE``) をまだ何も
    書かないうちに握ったまま呼ぶ。写しは別の接続で読む — 書き込みのロックは
    他の接続の読み取りを止めない (rollback journal でも WAL でも。
    tests/test_system_notice_migration.py で両方を確かめている) ので、写しは
    「このロックを取った時点のコミット済みの中身」になり、写しを取った後に
    別のプロセスが書いた行が、写しに無いまま移行されることがない。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "xb"):
        pass
    with closing(sqlite3.connect(str(src))) as s, closing(sqlite3.connect(str(dest))) as d:
        s.backup(d)


def _unique_suffix() -> str:
    """秒の時刻に足して、同時に起動したプロセス同士・同じ秒の二回の実行で名前が重ならない印。"""
    return f"{os.getpid()}_{uuid.uuid4().hex[:6]}"


def _snapshot_name(prefix: str, stamp: str) -> str:
    """写しのファイル名 (秒の時刻だけだと同時に起動した二つのプロセスで重なる)。"""
    return f"{prefix}.pre_migration_{stamp}_{_unique_suffix()}.bak"


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


#: 起動ごとのフォルダに、最初の結果の JSON と一緒に置く「この起動」の素性
#: (pid とプロセスの開始時刻)。:func:`regenerate_pending_reports` が、まだ走って
#: いる起動のフォルダを作り直しの対象から外すのに使う。
RUN_INFO_NAME = "RUN"


def _process_created_at(pid: int) -> Optional[float]:
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001 — 取れなければ照合しない
        return None


def _write_run_info(results_dir: Path) -> None:
    path = results_dir / RUN_INFO_NAME
    if path.exists():
        return
    _write_json_atomic(path, {
        "pid": os.getpid(),
        "process_created_at": _process_created_at(os.getpid()),
        "started_at": datetime.now(timezone.utc).isoformat(),
    })


def _run_in_progress(run_dir: Path) -> bool:
    """その起動のフォルダを書いたプロセスがまだ走っているか (素性の無い古いフォルダは偽)。

    pid が生きていて開始時刻も一致すれば走っている。開始時刻を照合できない
    (psutil で取れない) ときは、pid が生きていれば走っている側に倒す —
    走っている起動の途中の結果からレポートを作ると、確定していない移行を確定
    結果として報告することになる。プロセスが終わった後の起動で作り直される。
    """
    try:
        info = json.loads((run_dir / RUN_INFO_NAME).read_text(encoding="utf-8"))
        pid = int(info["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if pid == os.getpid():
        # このプロセス自身の起動 — 移行は順に走るので、ここを読む時点では返り終えている
        return False
    try:
        import psutil

        if not psutil.pid_exists(pid):
            return False
    except Exception:  # noqa: BLE001 — 生死を確かめられなければ走っている側に倒す
        return True
    expected = info.get("process_created_at")
    actual = _process_created_at(pid)
    if isinstance(expected, (int, float)) and actual is not None:
        return abs(float(expected) - actual) <= 0.01
    return True


def _write_result_json(results_dir: Optional[Path], key: str, result: StoreResult) -> None:
    """一つの DB の結果を ``results_dir/<key>.json`` に書く (レポートの材料)。

    書き換えのコミットの後に書く — コミットより前に書くと、コミットの前に
    止まった起動の JSON が「変換した」という確定していない結果として、次の起動の
    :func:`regenerate_pending_reports` に報告される。レポートの書き出しが
    失敗しても、次の起動でこの JSON 群からレポートだけを作り直せる。
    """
    if results_dir is None:
        return
    _write_run_info(results_dir)
    _write_json_atomic(results_dir / f"{key}.json", result.to_json())
    result.result_saved = True


def migrate_persona_db(
    db_path: Path,
    *,
    persona_id: str,
    output_dir: Path,
    dry_run: bool = False,
    stamp: str,
    results_dir: Optional[Path] = None,
) -> StoreResult:
    """一つのペルソナの記憶 DB を移行する。

    印 (:data:`MIGRATION_MARKER_KEY`) が立っていれば何もしない。そうでなければ
    書き込みの取引 (``BEGIN IMMEDIATE`` — 他のプロセスの書き込みを止める) を
    取り、その中で印をもう一度確かめ (別のプロセスが先に終えていれば何も
    せずに終える)、変換する行を走査し、変換する行があれば写しを
    ``output_dir/<persona_id>/`` へ取ってから (写しが取れなければ書き換えない)
    書き換えと印をコミットする。写しを取ってから書き換えるまでの間に別の
    プロセスが行を書けないので、写しに無い行が移行されることはない。
    ``init_db`` は呼ばない (:func:`_set_persona_marker`) — 写しは DB に何も
    触る前の中身になる。

    ``results_dir`` を渡すと、見るべきものがあるとき結果を JSON でコミットの
    後に書く (:func:`_write_result_json`)。``dry_run`` は読み取り専用で開き、
    何も書かない。
    """
    result = StoreResult(label=f"ペルソナ {persona_id}", path=str(db_path), persona=True)
    with closing(_open_readonly(db_path)) as conn:
        if _persona_marker_set(conn):
            result.already_done = True
            return result
        if dry_run:
            scan_persona_conn(conn, result)
            return result

    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _persona_marker_set(conn):
                # 同時に起動した別のプロセスが、こちらの読み取りの後に移行を
                # 終えた。何も書かずに終える。
                conn.rollback()
                result.already_done = True
                return result
            scan_persona_conn(conn, result)
            if result.updates:
                backup = output_dir / persona_id / _snapshot_name("memory.db", stamp)
                try:
                    _snapshot(db_path, backup)
                except Exception as exc:  # noqa: BLE001 — 写しが取れなければ書き換えない
                    LOGGER.error(
                        "[system-notice-migration] could not back up %s; skipping its migration",
                        db_path, exc_info=True,
                    )
                    conn.rollback()
                    failed = StoreResult(
                        label=result.label, path=result.path, persona=True,
                        error=f"移行前の写しを取れなかったので、この DB は移行していません: {exc}",
                    )
                    _try_write_result_json(results_dir, f"persona_{persona_id}", failed)
                    return failed
                result.backup_path = str(backup)
            _apply(conn, result.updates)
            _set_persona_marker(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if result.has_findings or result.backup_path:
        # コミットの後に結果を残す (レポートが書けなくても作り直せるように)。
        # 書けなくても移行は確定しているので、失敗扱いにはしない。
        _try_write_result_json(results_dir, f"persona_{persona_id}", result)
    return result


# ---------------------------------------------------------------------------
# 本体 DB (saiverse.db)
# ---------------------------------------------------------------------------

#: 回収で応対が送り直される判断 (on_event) の台帳の kind (saiverse/
#: autonomy_wiring.py の ``f"judgment.{kind}"``、判断点の on_event)。
_ON_EVENT_LEDGER_KIND = "judgment.on_event"
#: スルースの台帳の kind (sea/sluice.py の ``_LEDGER_KIND``)。記録済みの結果の
#: ``prompt`` は再適用のときに記録の paired_action_text になる。
_SLUICE_LEDGER_KIND = "sluice.pan"
#: 記憶への追記を運ぶ送信トレイの宛先 (saiverse/execution_ledger_wiring.py)。
_SAIMEMORY_APPEND_TARGETS = ("saimemory.append", "saimemory.append_digest")


def scan_main_conn(conn: sqlite3.Connection, result: StoreResult) -> None:
    """本体 DB を読んで、変換を ``result`` に積む (書かない)。

    凍結された本文は「まだ送り直し・記録し直しがありうる行」だけを直す — 送り
    終えた行の凍結は崩さない (送信トレイの不変条件 6 の例外を最小にする):

    - building_messages: 音声通話の痕跡 (role=user + タグ + event_message +
      ``voice_call``) を、今の形 (role=host・タグ無し、saiverse/voice_call.py の
      ``_building_entry``) にする。
    - execution_ledger: on_event の prepared 行の ``dispatch_envelope.user_input``
      (回収で応対の入力として送り直される) と、スルースの applied / completed
      行の結果の ``prompt`` (再適用で記録の paired_action_text になる)。
    - execution_outbox: 未配送 (pending / dead) の記憶への追記の ``message``。
    """
    from sai_memory.memory.storage import SYSTEM_NOTICE_KEY

    if _table_exists(conn, "building_messages"):
        for row_id, role, content, raw_meta in conn.execute(
            "SELECT id, role, content, metadata_json FROM building_messages "
            "WHERE substr(content, 1, ?) = ? ORDER BY id",
            (len(SYSTEM_OPEN), SYSTEM_OPEN),
        ).fetchall():
            metadata = _load_json(raw_meta)
            tags = _tags_of(metadata)
            is_voice_call = (
                role == "user"
                and "event_message" in tags
                and metadata.get("voice_call") is True
            )
            body = strip_system_tags(content) if is_voice_call else None
            if body is not None:
                result.updates.append((
                    "UPDATE building_messages SET role = 'host', content = ? WHERE id = ?",
                    (body, row_id),
                ))
                result.changes.append(Change(
                    "building_messages", str(row_id), "voice_call", content, body,
                ))
                continue
            result.skipped.append(Skipped(
                "building_messages", str(row_id), str(role), sorted(tags), _head(content),
                reason=AMBIGUOUS_TAGS_REASON if is_voice_call else "",
            ))

    if _table_exists(conn, "execution_ledger"):
        for exec_id, raw_payload in conn.execute(
            "SELECT EXECUTION_ID, PAYLOAD_JSON FROM execution_ledger "
            "WHERE KIND = ? AND STATUS = 'prepared'",
            (_ON_EVENT_LEDGER_KIND,),
        ).fetchall():
            payload = _load_json(raw_payload)
            envelope = payload.get("dispatch_envelope")
            if not isinstance(envelope, dict):
                continue
            text = envelope.get("user_input")
            if not starts_with_system_tag(text):
                continue
            body = strip_system_tags(text)
            if body is None:
                result.skipped.append(Skipped(
                    "execution_ledger(on_event)", str(exec_id), "", [], _head(text),
                    reason=AMBIGUOUS_TAGS_REASON,
                ))
                continue
            envelope = dict(envelope, user_input=body)
            payload = dict(payload, dispatch_envelope=envelope)
            result.updates.append((
                "UPDATE execution_ledger SET PAYLOAD_JSON = ? WHERE EXECUTION_ID = ?",
                (_dump_json(payload), exec_id),
            ))
            result.changes.append(Change(
                "execution_ledger(on_event)", str(exec_id), "schedule", text, body,
            ))
        for exec_id, raw_result in conn.execute(
            "SELECT EXECUTION_ID, RESULT_JSON FROM execution_ledger "
            "WHERE KIND = ? AND STATUS IN ('applied', 'completed')",
            (_SLUICE_LEDGER_KIND,),
        ).fetchall():
            recorded = _load_json(raw_result)
            text = recorded.get("prompt")
            if not starts_with_system_tag(text):
                continue
            body = strip_system_tags(text)
            if body is None:
                result.skipped.append(Skipped(
                    "execution_ledger(sluice)", str(exec_id), "", [], _head(text),
                    reason=AMBIGUOUS_TAGS_REASON,
                ))
                continue
            recorded = dict(recorded, prompt=body)
            result.updates.append((
                "UPDATE execution_ledger SET RESULT_JSON = ? WHERE EXECUTION_ID = ?",
                (_dump_json(recorded), exec_id),
            ))
            result.changes.append(Change(
                "execution_ledger(sluice)", str(exec_id), "paired_action", text, body,
            ))

    if _table_exists(conn, "execution_outbox"):
        placeholders = ",".join("?" for _ in _SAIMEMORY_APPEND_TARGETS)
        for outbox_id, raw_payload in conn.execute(
            "SELECT OUTBOX_ID, PAYLOAD_JSON FROM execution_outbox "
            f"WHERE TARGET IN ({placeholders}) AND STATUS IN ('pending', 'dead')",
            _SAIMEMORY_APPEND_TARGETS,
        ).fetchall():
            payload = _load_json(raw_payload)
            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not starts_with_system_tag(content):
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            kind = classify_tagged_message(message.get("role"), content, metadata)
            body = strip_system_tags(content) if kind is not None else None
            if kind is None or body is None:
                result.skipped.append(Skipped(
                    "execution_outbox", str(outbox_id), str(message.get("role")),
                    sorted(_tags_of(metadata)), _head(content),
                    reason="" if kind is None else AMBIGUOUS_TAGS_REASON,
                ))
                continue
            new_meta = dict(metadata)
            new_meta[SYSTEM_NOTICE_KEY] = kind
            message = dict(message, role="system", content=body, metadata=new_meta)
            payload = dict(payload, message=message)
            result.updates.append((
                "UPDATE execution_outbox SET PAYLOAD_JSON = ? WHERE OUTBOX_ID = ?",
                (_dump_json(payload), outbox_id),
            ))
            result.changes.append(Change(
                "execution_outbox", str(outbox_id), kind, content, body,
            ))


def migrate_main_db(
    db_path: Path, *, output_dir: Path, dry_run: bool = False, stamp: str,
    results_dir: Optional[Path] = None,
    last_leftovers: Optional[List[str]] = None,
) -> StoreResult:
    """本体 DB を移行する。

    印は持たない — 変換の条件 (タグで始まる・未配送・prepared) を満たす行は
    変換すると条件から外れるので、毎起動で呼んでも二度目以降は何も書かない。
    書き換える行があるときは、書き込みの取引 (``BEGIN IMMEDIATE``) の中で
    走査し直し、書き換える前に写しを ``output_dir`` に取る (起動時の
    pre_upgrade バックアップは数が上限を超えると消えるので別に取る。写しと
    書き換えの間に別のプロセスが書けない — :func:`migrate_persona_db` と同じ順序)。

    ``results_dir`` を渡すと、変換があるとき (書き換えのコミットの後) と、
    残した行が前回 (``last_leftovers`` — :meth:`StoreResult.leftover_keys`) と
    違うときに結果を JSON で書く — 本体 DB は毎起動で走査され、残した行は
    毎回同じものが見えるので、同じ行のままならレポートの材料にしない。
    """
    result = StoreResult(label="本体 DB", path=str(db_path))
    if not db_path.exists():
        return result
    with closing(_open_readonly(db_path)) as conn:
        scan_main_conn(conn, result)
    if dry_run:
        return result
    if not result.updates:
        if result.has_leftovers and result.leftover_keys() != last_leftovers:
            _try_write_result_json(results_dir, "main", result)
        return result

    applied = StoreResult(label=result.label, path=result.path)
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            scan_main_conn(conn, applied)
            if applied.updates:
                backup = output_dir / _snapshot_name("saiverse.db", stamp)
                try:
                    _snapshot(db_path, backup)
                except Exception as exc:  # noqa: BLE001 — 写しが取れなければ書き換えない
                    LOGGER.error(
                        "[system-notice-migration] could not back up %s; skipping its migration",
                        db_path, exc_info=True,
                    )
                    conn.rollback()
                    failed = StoreResult(
                        label=result.label, path=result.path,
                        error=f"移行前の写しを取れなかったので、本体 DB は移行していません: {exc}",
                    )
                    _try_write_result_json(results_dir, "main", failed)
                    return failed
                applied.backup_path = str(backup)
            # 変換が無ければ (同時に起動した別のプロセスが先に終えた) 何も書かない
            _apply(conn, applied.updates)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if applied.changes or (
        applied.has_leftovers and applied.leftover_keys() != last_leftovers
    ):
        # コミットの後に結果を残す。書けなくても移行は確定しているので失敗扱いにしない。
        _try_write_result_json(results_dir, "main", applied)
    return applied


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------


def render_report(results: Sequence[StoreResult], *, dry_run: bool, stamp: str) -> str:
    from sai_memory.memory.storage import SYSTEM_NOTICE_REVIEW_KEY

    lines: List[str] = [
        "# システム通知の移行レポート",
        "",
        f"- 実行: {stamp}" + (" (dry-run: 何も書き換えていない)" if dry_run else ""),
        "- 設計: docs/intent/system_notice_delivery.md「移行」",
        "",
    ]
    for res in results:
        if not res.has_findings and not res.backup_path:
            continue
        lines.append(f"## {res.label}")
        lines.append(f"- DB: {res.path}")
        if res.backup_path:
            lines.append(f"- 移行前の写し: {res.backup_path}")
        if res.error:
            lines.append(f"- エラー: {res.error}")
        counts = res.counts()
        if counts:
            lines.append("")
            lines.append("### 変換した件数 (表 / 系統)")
            for (table, kind), count in sorted(counts.items()):
                lines.append(f"- {table} / {kind}: {count}")
            lines.append("")
            lines.append(f"### サンプル (系統ごとに最大 {SAMPLES_PER_KIND} 件)")
            for c in _sample_changes(res.changes):
                lines.append(f"- [{c.table} / {c.kind}] id={c.row_id}")
                lines.append(f"  - 前: {_head(c.before)}")
                if c.before != c.after:
                    lines.append(f"  - 後: {_head(c.after)}")
        if res.skipped:
            lines.append("")
            lines.append(
                f"### 変換しなかった <system> 始まりの行 (全 {len(res.skipped)} 件)"
            )
            if res.persona and any(s.table == "messages" for s in res.skipped):
                lines.append(
                    f"(messages の行には確認待ちの印 `{SYSTEM_NOTICE_REVIEW_KEY}` を metadata に付け、"
                    "会話の手本・想起・あらすじの発話の判定から外した。本文と role は"
                    "そのまま。metadata が辞書として読めない行には付けていない。行を直したら"
                    "この印を消す)"
                )
            for s in res.skipped:
                reason = f" 理由={s.reason}" if s.reason else ""
                lines.append(
                    f"- [{s.table}] id={s.row_id} role={s.role} tags={s.tags}{reason}: {s.head}"
                )
        if res.unknown_system_count:
            lines.append("")
            lines.append(
                f"### 系統を推定できなかった role=system の行 (全 {res.unknown_system_count} 件、"
                f"先頭 {len(res.unknown_system_samples)} 件を表示。通知の印は付けず、"
                f"確認待ちの印 `{SYSTEM_NOTICE_REVIEW_KEY}` を付けた。metadata が読めない行を除く)"
            )
            for s in res.unknown_system_samples:
                lines.append(f"- id={s.row_id} tags={s.tags}: {s.head}")
        if res.untagged_memorize_count:
            lines.append("")
            lines.append(
                "### タグ無しの role=user で playbook: タグがあり conversation タグの無い行 "
                f"(旧 MEMORIZE の結果の候補。全 {res.untagged_memorize_count} 件、"
                f"先頭 {len(res.untagged_memorize_samples)} 件を表示。変換はしていない)"
            )
            for s in res.untagged_memorize_samples:
                lines.append(f"- id={s.row_id} tags={s.tags}: {s.head}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _iter_persona_dbs(saiverse_home: Path) -> Iterable[Tuple[str, Path]]:
    personas_dir = saiverse_home / "personas"
    if not personas_dir.is_dir():
        return
    for child in sorted(personas_dir.iterdir()):
        db = child / "memory.db"
        if child.is_dir() and db.is_file():
            yield child.name, db


#: DB ごとの結果の JSON の置き場 (``output_dir`` の下に、起動ごとのフォルダ
#: ``<時刻>_<pid>_<乱数>`` を作る)。
RESULTS_DIR_NAME = "results"
#: レポートを書き終えた起動のフォルダに置く印のファイル (中身はレポートのパス)。
REPORTED_MARKER_NAME = "REPORTED"
#: DB ごとの、前回の走査で残した行の同定 (``{DB のパス: StoreResult.leftover_keys()}``)。
#: 本体 DB は毎起動で走査されるので、同じ行が残っているだけでは毎回レポートを
#: 書かない (件数ではなく行で比べる — 件数が同じままの入れ替わりも報告する)。
LAST_LEFTOVERS_NAME = "last_leftovers.json"


def _load_last_leftovers(output_dir: Path) -> Dict[str, List[str]]:
    try:
        data = json.loads((output_dir / LAST_LEFTOVERS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_last_leftovers(
    output_dir: Path, results: Sequence[StoreResult], previous: Dict[str, List[str]],
) -> None:
    state = dict(previous)
    for r in results:
        if r.error or r.already_done or r.result_save_failed:
            # 材料を保存できなかった回は前回比較を進めない — 進めると、同じ行が
            # 次の起動で「前回と同じ」として報告されずに終わる。
            continue
        if not r.has_leftovers and r.path not in state:
            continue
        state[r.path] = r.leftover_keys()
    if state != previous:
        _write_json_atomic(output_dir / LAST_LEFTOVERS_NAME, state)


def _try_write_result_json(results_dir: Optional[Path], key: str, result: StoreResult) -> None:
    """結果の JSON を書く。書けなくても他の DB も移行の確定も止めない。

    書けなかったら ``result_save_failed`` を立てる — 起動の入口
    (:func:`run_startup_system_notice_migration`) がそれを画面の警告にし、本体 DB の
    前回比較 (:func:`_save_last_leftovers`) を更新しない。レポートから欠ける詳細の
    代わりに、残した行の同定 (表と行 id。本文は書かない) をログに残す。
    """
    try:
        _write_result_json(results_dir, key, result)
    except Exception:  # noqa: BLE001
        result.result_save_failed = True
        LOGGER.error(
            "[system-notice-migration] could not write the result of %s "
            "(converted=%d, rows needing review: %s)",
            result.label, len(result.changes), result.leftover_keys(),
            exc_info=True,
        )


def _load_run_results(run_dir: Path) -> List[StoreResult]:
    out: List[StoreResult] = []
    for path in sorted(run_dir.glob("*.json")):
        try:
            out.append(StoreResult.from_json(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, KeyError, TypeError):
            LOGGER.warning(
                "[system-notice-migration] unreadable result file %s", path, exc_info=True,
            )
    # 本体 DB を先に、ペルソナは名前の順に
    out.sort(key=lambda r: (r.persona, r.label))
    return out


def _write_run_report(run_dir: Path, report_path: Path, *, stamp: str) -> Optional[Path]:
    """一回の起動の結果の JSON 群からレポートを書き、その起動のフォルダに書き終えた印を置く。"""
    results = _load_run_results(run_dir)
    if not results:
        return None
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(results, dry_run=False, stamp=stamp), encoding="utf-8")
    (run_dir / REPORTED_MARKER_NAME).write_text(str(report_path), encoding="utf-8")
    return report_path


def regenerate_pending_reports(saiverse_home: Path) -> List[Path]:
    """前の起動で結果の JSON を書いたのにレポートを書けなかった分を、JSON 群から作り直す。

    DB には触らない (完了の印はそのまま尊重する)。作り直したレポートのパスを返す。
    まだ走っている起動のフォルダ (:func:`_run_in_progress` — 同時に起動した別の
    プロセスが移行の途中) は対象にしない。その起動が自分でレポートを書くか、
    書けずに終われば次の起動で作り直される。
    """
    output_dir = saiverse_home / "backups" / OUTPUT_DIR_NAME
    results_root = output_dir / RESULTS_DIR_NAME
    if not results_root.is_dir():
        return []
    written: List[Path] = []
    for run_dir in sorted(results_root.iterdir()):
        if not run_dir.is_dir() or (run_dir / REPORTED_MARKER_NAME).exists():
            continue
        if not any(run_dir.glob("*.json")):
            continue
        if _run_in_progress(run_dir):
            continue
        try:
            path = _write_run_report(
                run_dir, output_dir / f"report_{run_dir.name}.md", stamp=run_dir.name,
            )
        except Exception:  # noqa: BLE001 — 作り直せなければ次の起動でまた試す
            LOGGER.error(
                "[system-notice-migration] could not regenerate the report for %s", run_dir,
                exc_info=True,
            )
            continue
        if path is not None:
            LOGGER.info("[system-notice-migration] report regenerated: %s", path)
            written.append(path)
    return written


def run_migration(
    *,
    db_path: Optional[Path],
    saiverse_home: Path,
    dry_run: bool = False,
    report_path: Optional[Path] = None,
) -> Tuple[List[StoreResult], Optional[Path]]:
    """本体 DB と全ペルソナの記憶 DB を移行し、見るべきものがあればレポートを書く。

    返り値は (DB ごとの結果, レポートのパス)。各 DB の結果は、見るべきもの
    (変換した行・変換しなかったタグ始まりの行・推定できなかった role=system 行・
    旧 MEMORIZE の結果の候補・エラー) があるとき、DB を一つ処理するたびに
    ``results/<時刻>_<pid>_<乱数>/`` へ JSON で書き、レポートはその JSON 群から組む
    (JSON は書き換えのコミットの後に書く。レポートが書けなくても次の起動で
    :func:`regenerate_pending_reports` が作り直せる)。本体 DB の残した行は、
    前回と同じ行なら材料にしない。``dry_run`` は JSON を書かず、見るべきものが
    あればその場の結果からレポートを書く。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"{stamp}_{_unique_suffix()}"
    output_dir = saiverse_home / "backups" / OUTPUT_DIR_NAME
    results_dir = None if dry_run else output_dir / RESULTS_DIR_NAME / run_id
    last_leftovers = {} if dry_run else _load_last_leftovers(output_dir)
    results: List[StoreResult] = []

    if db_path is not None:
        try:
            results.append(migrate_main_db(
                db_path, output_dir=output_dir, dry_run=dry_run, stamp=stamp,
                results_dir=results_dir, last_leftovers=last_leftovers.get(str(db_path)),
            ))
        except Exception as exc:  # noqa: BLE001 — 一つの DB の失敗で他を止めない
            LOGGER.error("[system-notice-migration] main DB migration failed", exc_info=True)
            failed = StoreResult(
                label="本体 DB", path=str(db_path), error=f"移行に失敗しました (変更は巻き戻し済み): {exc}",
            )
            results.append(failed)
            _try_write_result_json(results_dir, "main", failed)

    for persona_id, persona_db in _iter_persona_dbs(saiverse_home):
        try:
            results.append(migrate_persona_db(
                persona_db, persona_id=persona_id, output_dir=output_dir,
                dry_run=dry_run, stamp=stamp, results_dir=results_dir,
            ))
        except Exception as exc:  # noqa: BLE001
            LOGGER.error(
                "[system-notice-migration] persona %s migration failed", persona_id,
                exc_info=True,
            )
            failed = StoreResult(
                label=f"ペルソナ {persona_id}", path=str(persona_db), persona=True,
                error=f"移行に失敗しました (変更は巻き戻し済み): {exc}",
            )
            results.append(failed)
            _try_write_result_json(results_dir, f"persona_{persona_id}", failed)

    written: Optional[Path] = None
    if results_dir is None:
        if any(r.has_findings for r in results):
            written = report_path or output_dir / f"report_{stamp}_dry_run.md"
            written.parent.mkdir(parents=True, exist_ok=True)
            written.write_text(render_report(results, dry_run=True, stamp=stamp), encoding="utf-8")
    else:
        report_ok = True
        if results_dir.is_dir() and any(results_dir.glob("*.json")):
            try:
                written = _write_run_report(
                    results_dir, report_path or output_dir / f"report_{run_id}.md", stamp=stamp,
                )
            except Exception:  # noqa: BLE001 — 次の起動で JSON 群から作り直す
                LOGGER.error(
                    "[system-notice-migration] could not write the report; it will be "
                    "regenerated from %s on the next start", results_dir, exc_info=True,
                )
                written = None
                report_ok = False
        if report_ok:
            try:
                _save_last_leftovers(output_dir, results, last_leftovers)
            except Exception:  # noqa: BLE001
                LOGGER.error(
                    "[system-notice-migration] could not save the leftover counts", exc_info=True,
                )
    for res in results:
        if res.changes or res.error:
            LOGGER.info(
                "[system-notice-migration] %s: converted=%d skipped=%d unknown_system=%d error=%s",
                res.label, len(res.changes), len(res.skipped),
                res.unknown_system_count, res.error,
            )
    if written is not None:
        LOGGER.info("[system-notice-migration] report written: %s", written)
    return results, written


def run_startup_system_notice_migration(db_path: str) -> List[Dict[str, Any]]:
    """起動時の入口 (main.py がペルソナの読み込みより前に呼ぶ)。

    返り値は画面のバナーへ積む警告・知らせ (``manager.startup_alerts`` の形)。
    移行の失敗はここで握って起動は止めない — 失敗した DB は印が立たないので
    次の起動で再試行される。
    """
    from saiverse.data_paths import get_saiverse_home

    home = get_saiverse_home()
    alerts: List[Dict[str, Any]] = []
    try:
        regenerated = regenerate_pending_reports(home)
    except Exception:  # noqa: BLE001
        LOGGER.error("[system-notice-migration] report regeneration failed", exc_info=True)
        regenerated = []
    if regenerated:
        alerts.append({
            "id": "system_notice_migration_report_regenerated",
            "level": "info",
            "title": "前回書けなかったシステム通知の移行のレポートを作り直しました",
            "message": "レポート: " + ", ".join(str(p) for p in regenerated),
            "details": {},
        })
    try:
        results, report = run_migration(db_path=Path(db_path), saiverse_home=home)
    except Exception:  # noqa: BLE001
        LOGGER.error("[system-notice-migration] migration aborted", exc_info=True)
        return alerts + [{
            "id": "system_notice_migration_failed",
            "level": "warning",
            "title": "システム通知の記録の移行に失敗しました",
            "message": "起動時の移行が途中で止まりました。次の起動で再試行します。ログを確認してください。",
            "details": {},
        }]
    failed = [r for r in results if r.error]
    # 人の確認が要る行 (変換しなかった行・推定できなかった行・旧 MEMORIZE の候補)。
    # レポートの材料 (結果の JSON) に書いた DB の分だけ数える — 本体 DB の残りは
    # 前回と同じ行ならレポートの材料にしないので、毎起動で知らせない。
    saved = [r for r in results if r.result_saved]
    # 材料に書けなかった DB の分も、この起動の手元の結果から数える
    unsaved = [r for r in results if r.result_save_failed]
    review_rows = sum(sum(r.leftover_counts()) for r in saved + unsaved)
    if unsaved:
        alerts.append({
            "id": "system_notice_migration_result_unsaved",
            "level": "warning",
            "title": "システム通知の記録の移行結果を一部保存できず、レポートに詳細が欠けています",
            "message": (
                "移行の結果をレポートの材料に保存できなかった記録があります: "
                + "、".join(r.label for r in unsaved) + "。"
                + (
                    "ペルソナの記憶は移行の完了の印が立っているので、次の起動で走査し"
                    "直されず、この記録の詳細はレポートに載りません (変換した件数と確認が"
                    "要る行の id はこの起動のログにあります)。"
                    if any(r.persona for r in unsaved) else ""
                )
                + (
                    "本体 DB の確認が要る行は次の起動でもう一度報告します。"
                    if any(not r.persona for r in unsaved) else ""
                )
                + (f"確認が要る行: {review_rows} 件。" if review_rows else "")
            ),
            "details": {"unsaved": [r.label for r in unsaved]},
        })
    if saved and report is None:
        # 材料は書けたのにレポートが書けなかった。移行の成否と確認が要る行の
        # 件数は分かっているので、レポートを待たずに知らせる (レポートは次の
        # 起動で材料から作り直す — regenerate_pending_reports)。
        converted = any(r.changes for r in saved)
        alerts.append({
            "id": "system_notice_migration_report_failed",
            "level": "warning",
            "title": "システム通知の記録の移行のレポートを書き出せませんでした",
            "message": (
                ("移行は完了しましたが、" if converted and not failed else "")
                + "結果のレポートの書き出しに失敗しました (次の起動で作り直します)。"
                + f"確認が要る行: {review_rows} 件。"
            ),
            "details": {},
        })
    if failed:
        alerts.append({
            "id": "system_notice_migration_failed",
            "level": "warning",
            "title": "システム通知の記録の移行が一部の記録でできませんでした",
            "message": (
                "移行できなかった記録は元のまま残っていて、次の起動で再試行します。"
                + (f" 詳細: {report}" if report else "")
            ),
            "details": {"failed": [r.label for r in failed]},
        })
    elif report is not None and any(r.changes for r in results):
        alerts.append({
            "id": "system_notice_migration_done",
            "level": "info",
            "title": "システム通知の記録を新しい形式へ移行しました",
            "message": f"移行前の写しと結果のレポートを保存しました: {report}" + (
                f" 変換せずに残した、確認が要る行が {review_rows} 件あります (レポートに一覧があります)。"
                if review_rows else ""
            ),
            "details": {},
        })
    elif report is not None and review_rows:
        alerts.append({
            "id": "system_notice_migration_review",
            "level": "warning",
            "title": "システム通知の記録の移行で、確認が要る行があります",
            "message": (
                f"変換せずに残した行・種類を推定できなかった行が {review_rows} 件あります。"
                f"一覧はレポートにあります: {report}"
            ),
            "details": {},
        })
    return alerts


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="システム通知の旧形式データを新形式へ正規化する (コピーで試すこと)",
    )
    parser.add_argument("--home", required=True, help="SAIVERSE_HOME (personas/ を含むフォルダ)")
    parser.add_argument(
        "--db", default=None,
        help="本体 DB のパス (省略時は <home>/user_data/database/saiverse.db、無ければ本体 DB は飛ばす)",
    )
    parser.add_argument("--dry-run", action="store_true", help="読み取り専用で開き、レポートだけ書く")
    parser.add_argument("--report", default=None, help="レポートの書き出し先")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    home = Path(args.home)
    db = Path(args.db) if args.db else home / "user_data" / "database" / "saiverse.db"
    if not args.dry_run:
        for regenerated in regenerate_pending_reports(home):
            print(f"regenerated report: {regenerated}")
    results, report = run_migration(
        db_path=db if db.exists() else None,
        saiverse_home=home,
        dry_run=args.dry_run,
        report_path=Path(args.report) if args.report else None,
    )
    for res in results:
        print(f"{res.label}: converted={len(res.changes)} skipped={len(res.skipped)} "
              f"unknown_system={res.unknown_system_count} done_before={res.already_done} "
              f"error={res.error}")
    print(f"report: {report}")
    return 0 if not any(r.error for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
