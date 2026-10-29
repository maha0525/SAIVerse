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
   文章」として読まれる)。例外は、本番の写しの dry-run レポートで系統を確定した
   旧世代のスケジュール発火文・外部イベント通知で、書き出しと発火の記録の
   metadata と、ユーザーの発言ではないことの来歴の個別条件で拾う
   (:func:`legacy_notice`)。
2. 旧世界で role=system・タグ無しで保存された行 (スペルの結果・TOOL の結果・
   Handy Tool・権限拒否など)。``__system_notice__`` が無いので
   ``is_system_notice`` が偽になり、自動想起の種などから外れる。tags から系統を
   推定できるものに印を付ける。推定できない行は触らない。

規則の版 3 (docs/intent/system_notice_delivery.md「追補」の移行の規則の第二版) で、
1 の印を持たない行のうち、作った側が機構だと言い切れる型 (tool / internal タグ・
実在する Playbook 名のタグ・退役した子ラインの結果の書き出し・音声入力の案内文) と、
2 の perception タグ・感情パラメータ変動の知らせを足した。音声入力の案内文の行は、
通知の行と本文の無い音声の行の二行に分ける (:func:`_queue_voice_split`)。ただし
元の行を確かめられない音声入力の行は分けずに確認待ちにする。

規則の版 4 で、2 に、``switch_active_thread`` (builtin_data/tools/thread_switch.py) が
書いた行と、ChatGPT の会話の取り込み (scripts/import_chatgpt_conversations.py) の
見出しの行を足した。どちらも v0.3.27 で書き手を直したときに漏れていて、印の無い
role=system のまま書かれていた行。同じ版で、``lg_stelis_start_node`` (sea/runtime_nodes.py)
が書いた Stelis の開始の行と、削除済みの二つの書き手 (別の City への移動の失敗の案内、
``get_since_last_user_conversation`` の要約の行) が書いた行も足した。

元の発言の行 (本体 DB の ``building_messages`` — :class:`BuildingSourceRows`) が画面
からの送信の印 ``client_message_id`` を持つ行は、どの版のどの規則に当たっても変換せず
確認待ちにする (:data:`SENT_FROM_SCREEN_REASON`)。所有者が確認待ちの行を確定した記録
(``SYSTEM_NOTICE_REVIEWED_KEY``) を持つ行には触れない。

規則で変換するのは、この環境で新しい形式に切り替わった時刻 (:func:`resolve_cutover`
— 環境ごとに一度だけ決めて ``cutover.json`` に記録する) より前に作られた行だけ。
それより後は機構が旧形式の本文を書かないので、旧形式の形をした行は、ユーザーが
案内文などを真似た発言でありうる。その行は、どの規則に当たっても変換せず、確認
待ちにして所有者が決める (:data:`AFTER_CUTOVER_REASON`)。完了の印の無い新しい
記憶の全体の走査・規則の版を上げた走査し直し・完了の印を見送った後の走査し直し、
本体 DB の毎起動の走査のどれでも同じ。時刻を決められない・記録できない起動では、
何も変換しない (:class:`CutoverUnavailable`)。

ほかに、同じ本文の形を持つ列・表を最終形へ揃える: messages の
``paired_action_text`` 列、``perception_batches`` (kind=``realtime_and_recall``)
の ``rendered_text``、本体 DB の ``building_messages`` (音声通話の痕跡が
role=user + タグで載った世代)、``execution_ledger`` / ``execution_outbox`` に
凍結された本文 (まだ送り直し・記録し直しがありうる行だけ)。

「本文が ``<system>`` で始まるか」を意味の判定に使うのは、起動時のこの移行と、
所有者が確認待ちの行を確定する画面 (saiverse/system_notice_review.py) だけ
(不変条件 2 の例外 — 後者は所有者の手による管理の操作として認めたもの)。画面が
使うのは確認待ちの行のタグの剥がし方 (:func:`strip_system_tags` /
:func:`starts_with_system_tag`) だけで、それ以外の常設のコードからこのモジュールを
呼ばない。

起動時の入口は :func:`run_startup_system_notice_migration` (main.py が
ペルソナの読み込みより前に呼ぶ)。ペルソナごとの記憶 DB は ``embed_metadata``
の :data:`MIGRATION_MARKER_KEY` で一回限りにし、書き換える前にその DB の
写しを ``<SAIVERSE_HOME>/backups/system_notice_migration/`` に取る (通常の
起動時バックアップは数が上限を超えると古いものから消えるので、この写しは
別の置き場に置く)。移行の結果は同じ置き場にレポートとして書き出す — 本番へ
当てたあとに人の目で確かめるためのもの。レポートの材料は DB ごとの結果の
JSON (``results/<時刻>_<pid>_<乱数>/``)。書き換えを伴う DB の JSON は、
コミットの前に仮の名前で書き (書けなければその DB を巻き戻して完了の印を
立てない)、コミットの後に正式な名前へ付け替える。レポートの書き出しが失敗
しても次の起動で JSON 群から作り直せる。

コピーで試すときは CLI を使う::

    python -m database.system_notice_migration --home <SAIVERSE_HOME の写し> --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AbstractSet, Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple, Union

LOGGER = logging.getLogger(__name__)

SYSTEM_OPEN = "<system>"
SYSTEM_CLOSE = "</system>"

#: ペルソナの記憶 DB の ``embed_metadata`` に書く「この移行は済んだ」の印。
#: 値は ``"v<規則の版> <完了時刻 (ISO)>"`` (:func:`_marker_value`)。版 1 の印は
#: 版を書く前の形で、値が完了時刻だけ。置き場は saiverse/v3_shape_migration.py の
#: ``MARKER_KEY`` と同じ流儀 — 書き込み先と同じ DB に置くので、memory.db を
#: 差し替え・復元しても「移行したか」と中身がずれない。
MIGRATION_MARKER_KEY = "system_notice_migration_done"

#: 移行の規則の版。済んだ DB にも当て直す必要のある規則を足したときに上げる。
#: 印の版がこれより古い DB は、足した規則だけで走査し直す (:func:`migrate_persona_db`)。
#:
#: - 版 1: 印の AND 判定・印の無い role=system の推定・列と表の剥がし
#: - 版 2: 旧世代のスケジュール発火文・外部イベント通知の個別条件
#:   (:func:`legacy_notice`)
#: - 版 3: docs/intent/system_notice_delivery.md「追補」の移行の規則の第二版
#:   (:func:`classify_tagged_message_v3` と、:func:`classify_unmarked_system_message`
#:   の ``perception`` タグ・感情パラメータ変動の行)
#: - 版 4: 印の無い role=system 行のうち、``switch_active_thread`` (builtin_data/
#:   tools/thread_switch.py) が書いた行と、scripts/import_chatgpt_conversations.py が
#:   取り込みの先頭に書いた見出しの行、``lg_stelis_start_node`` (sea/runtime_nodes.py)
#:   が書いた Stelis の開始の行、旧 ``_handle_movement`` が書いた別の City への移動の
#:   失敗の案内、旧 ``get_since_last_user_conversation`` が書いた要約の行
#:   (:func:`classify_unmarked_system_message`)
#: - 版 5: v0.3.27 より前の ``PulseController._build_resumption_prompt`` が ``<system>``
#:   で包んで書いた中断の再開文 (:func:`resumption_notice`)。版 4 の走査の後に本番の
#:   確認の画面で見つかった
#:
#: 印の版が N の DB は、版 N より後に足した規則だけで走査し直す (各規則が自分の
#: 版を持つ — :func:`_classify_persona_row`)。
MIGRATION_RULES_VERSION = 5


def _marker_value() -> str:
    return f"v{MIGRATION_RULES_VERSION} {datetime.now(timezone.utc).isoformat()}"


def marker_rules_version(value: Optional[str]) -> Optional[int]:
    """完了の印の値が表す規則の版。印が無ければ None。

    版を書いていない値 (完了時刻だけ — 版 1 の形) と、読めない値は版 1 として
    扱う — 古い側に倒せば、走査し直しは変換済みの行を素通りするだけで済む。
    """
    if value is None:
        return None
    head = str(value).split(" ", 1)[0]
    if head.startswith("v") and head[1:].isdigit():
        return int(head[1:])
    return 1

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
    - 剥がした結果がまだ ``<system>`` で始まる (``<system><system>A</system></system>``)。
      二重の包みか、本文の先頭の引用かを決められない。加えて、剥がした結果が
      また変換の条件 (``<system>`` で始まる) を満たすと、次の走査でもう一度
      剥がされる — 完了の印を持たない本体 DB と、印を見送ったペルソナの記憶 DB
      (:func:`migrate_persona_db`) は毎起動で走査し直すので、変換の結果が変換の
      条件から外れることが冪等の前提になる。
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
    body = "\n\n".join(s.strip() for s in segments if s.strip())
    if starts_with_system_tag(body):
        return None  # 剥がしてもまだ包みで始まる (二重の包みか引用か決められない)
    return body


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


#: 系統の印 (``with:["system"]`` / ``event_message`` など) を持たない旧世代の
#: スケジュール発火文の書き出し。旧 schedule_manager は二つの文面を使った
#: (現行は前者 — saiverse/schedule_manager.py)。
_LEGACY_SCHEDULE_PREFIXES = ("スケジュールの実行時刻です", "スケジュールが実行されました")
#: 同じく、旧世代の外部イベント通知 (X のメンションなど) の書き出し。現行は
#: saiverse/autonomy_wiring.py が ``source="external_event"`` 付きで組む。
_LEGACY_EXTERNAL_EVENT_PREFIX = "[外部イベント通知]"
#: レポートの件数とサンプルで、印で拾った ``schedule`` と区別する表記。行に書く
#: ``__system_notice__`` の値はどちらも ``schedule`` (外部イベントの応対は
#: スケジュールと同じ入口から起動していた)。
LEGACY_SCHEDULE_LABEL = "schedule(旧世代)"
LEGACY_EXTERNAL_EVENT_LABEL = "external_event(旧世代)"
#: 旧世代の通知が記憶 DB に書かれたときの系統名 (``__system_notice__`` の値)。
LEGACY_NOTICE_KIND = "schedule"

#: 建物履歴から記憶への転記が行の metadata に刻む来歴のキー
#: (builtin_data/tools/get_building_messages.py の ``BUILDING_MSG_REF_KEY``)。
#: 旧データの判定なので、保存された当時の値をここに固定する。
_BUILDING_MSG_REF_KEY = "building_msg_ref"


def _has_user_utterance_provenance(metadata: Dict[str, Any]) -> bool:
    """記憶 DB の行が、ユーザーの発言の来歴の印を持つか (来歴の縛り)。

    建物履歴からの転記の印 (``building_msg_ref``) と、通話の文字起こしの印
    (``voice_call`` / ``voice_transcript``)。docs/intent/system_notice_delivery.md
    「移行」の来歴の縛り。
    """
    return bool(
        _BUILDING_MSG_REF_KEY in metadata
        or metadata.get("voice_call")
        or metadata.get("voice_transcript")
    )


def legacy_notice(
    role: Any, content: Any, metadata: Dict[str, Any],
) -> Optional[Tuple[str, str]]:
    """系統の印を持たない旧世代のスケジュール発火文・外部イベント通知なら (レポートの表記, 剥がした本文)。

    docs/intent/system_notice_delivery.md「移行」の旧世代の個別条件。本番の写しの
    dry-run レポートで系統を確定した二つだけを拾う。条件は全部の AND:

    - role=user、tags が ``conversation`` だけ (``pulse:`` を除く)
    - ユーザーの発言の来歴を持たない — 建物履歴からの転記の印
      (``building_msg_ref``) も、通話の文字起こしの印 (``voice_call`` /
      ``voice_transcript``) も無い
    - タグの剥がしが成立する (完全な組が一つ)
    - 剥がした本文の先頭が発火文の書き出しで、その発火の記録として供給側が付けた
      metadata を持つ — スケジュールは ``schedule_id``、外部イベントは
      ``source="external_event"``

    ``schedule_id`` と ``source`` は、送り手 (チャットの送信 API) も metadata に
    書ける。だから本文と metadata だけでは、ユーザーが発火文の形を打った発言と
    区別できない。来歴の条件はそれを縛る: 今のコードでユーザーの発言が記憶に
    入る経路は、建物履歴からの転記 (2026-07-21 から必ず ``building_msg_ref`` を
    刻む) と通話の文字起こしだけ (本番の写しでは正規の旧発火文 306 件のどれも
    これらの印を持たない)。建物履歴の行の来歴は列で縛る (:func:`scan_main_conn`
    — 呼び出し側の条件)。

    書き出しは剥がした本文の先頭でだけ見る (中断の再開文が元の発火文を引用して
    いる行は、先頭が再開文なので拾わない)。X の投稿指示・音声入力の通知など、
    出自の確度が足りない形はここで拾わない。:func:`classify_tagged_message` が
    系統を決められなかった行にだけ使う (印があるものはそちらが優先)。
    """
    if not isinstance(role, str) or role.lower() != "user":
        return None
    if not starts_with_system_tag(content):
        return None
    tags = {t for t in _tags_of(metadata) if not t.startswith("pulse:")}
    if tags != {"conversation"}:
        return None
    if _has_user_utterance_provenance(metadata):
        return None
    body = strip_system_tags(content)
    if body is None:
        return None
    if body.startswith(_LEGACY_SCHEDULE_PREFIXES):
        return (LEGACY_SCHEDULE_LABEL, body) if metadata.get("schedule_id") is not None else None
    if body.startswith(_LEGACY_EXTERNAL_EVENT_PREFIX):
        if metadata.get("source") == "external_event":
            return LEGACY_EXTERNAL_EVENT_LABEL, body
        return None
    return None


#: 規則 3 (tags に Playbook 名がそのまま入っている行) で Playbook 名に数えない
#: タグ。この移行の判定そのものに使うタグで、同名の Playbook があっても系統の
#: 根拠にしない。
_GENERAL_TAGS: FrozenSet[str] = frozenset({
    "conversation", "internal", "tool", "event_message", "sluice", "day_plan",
    "curation", "perception", "version_upgrade", _DIGEST_TAG, "track_context",
    "track_chronicle_insert", "situation_change", "interrupted", "spell", "pre_spell",
    "handy_tool", "permission", "denied", "error", "exec",
})

#: 退役した Playbook (builtin_data/playbooks/archive/ の memory_research など) の
#: MEMORIZE が書いた子ラインの結果の書き出し。:data:`_SUBLINE_REPORT_PREFIX` とは
#: 別の文面。
_ARCHIVED_SUBLINE_REPORT_PREFIX = "サブプレイブック実行結果"

#: 音声入力のときにアドオンが本文に添えた定型の案内文 (タグを剥がした本文の全体)。
#: 名前の部分は供給元ごとに違うので任意の短い文字列にする。
_VOICE_INPUT_NOTICE_RE = re.compile(
    r"\S.{0,40}?\s?から音声入力を受信しました。添付の音声を(?:聴いて応答して|聞いて)ください。"
)
#: 音声入力の案内文を通知に変えた行の系統名。
VOICE_INPUT_NOTICE_KIND = "voice_input"
#: 音声入力のアドオンが metadata の ``source`` に書く値の末尾。
_VOICE_SOURCE_SUFFIX = "_voice"


#: 発話者が付けた添付の置き場 (api/routes/chat.py の /chat/send が書く 3 つ。確認待ちの
#: 行を確定する画面の saiverse/system_notice_review.py の ``ATTACHMENT_FIELDS`` と同じ)。
_ATTACHMENT_FIELDS = ("media", "images", "documents")


def _speaker_has_attachments(role: Any, metadata: Dict[str, Any]) -> bool:
    """role=system でない行 (発話者の行) が、添付の項目を一つでも持つか。

    項目の数え方は確認の画面の ``attachment_items`` と同じ (各欄のリストの辞書の項目を
    一つずつ数え、ファイルを指す欄が無い項目も数える)。この行を行ごと role=system に
    すると発話者の添付が機構の通知の持ち物になるので、移行は規則 5 の二行分割以外では
    行ごと変換しない (:data:`SPEAKER_ATTACHMENT_REASON`)。
    """
    role_s = role.lower() if isinstance(role, str) else ""
    if role_s == "system":
        return False
    for field_name in _ATTACHMENT_FIELDS:
        values = metadata.get(field_name)
        if isinstance(values, list) and any(isinstance(item, dict) for item in values):
            return True
    return False


def _has_audio_media(metadata: Dict[str, Any]) -> bool:
    """metadata の ``media`` に音声の項目が一つでもあるか。

    項目が音声かの判定は、音声を読み取る側 (``saiverse.media_utils.iter_audio_media``) と
    同じ :func:`saiverse.media_utils.is_audio_media_item` — ``type`` があれば大文字小文字を
    区別せずに ``audio`` か、無ければ ``mime_type`` が ``audio/`` で始まるか。元の発言の
    行の引き当て (:class:`BuildingSourceRows`) も同じ関数を使う。
    """
    from saiverse.media_utils import is_audio_media_item

    media = metadata.get("media")
    if not isinstance(media, list):
        return False
    return any(is_audio_media_item(m) for m in media)


def is_voice_input_notice(body: Optional[str], metadata: Dict[str, Any]) -> bool:
    """タグを剥がした本文が音声入力の定型の案内文で、行が音声入力の発言の形か (規則 5)。

    来歴の縛りは掛けない — これはユーザーの音声の発言そのものの行なので、建物
    履歴からの転記の印を持っている。条件は本文が案内文の形・metadata の media に
    音声がある・``source`` が ``_voice`` で終わる、の AND。

    この形はチャットの送信 API の送り手も作れるので、変換するのは元の発言の行が
    画面からの送信の印を持たないと確かめられた行だけ (:class:`BuildingSourceRows`。
    確かめるのは変換の手前の :func:`_queue_conversion`)。
    """
    if not isinstance(body, str) or _VOICE_INPUT_NOTICE_RE.fullmatch(body) is None:
        return False
    source = metadata.get("source")
    return (
        _has_audio_media(metadata)
        and isinstance(source, str)
        and source.endswith(_VOICE_SOURCE_SUFFIX)
    )


#: v0.3.27 より前の ``PulseController._build_resumption_prompt`` (sea/pulse_controller.py)
#: が書いた中断の再開文の形。優先度の高い要求で中断した Pulse をやり直すとき、元の
#: プロンプトを「前回のプロンプト」として引用し、その後ろに元のプロンプトをもう一度
#: 並べる。当時は元のプロンプトも再開文の頭も ``<system>`` で包んでいたので、包みの組が
#: 二つ並び、``strip_system_tags`` では剥がせなかった。
_RESUMPTION_HEAD = (
    "[前回の処理が中断されました]\n中断理由: 優先度の高いリクエストを処理しました\n前回のプロンプト: "
)
_RESUMPTION_RE = re.compile(
    "<system>\n" + re.escape(_RESUMPTION_HEAD) + r"(?P<quoted>.*)\n</system>\n\n(?P<prompt>.*)",
    re.DOTALL,
)
#: レポートの表記
RESUMPTION_LABEL = "schedule(中断の再開文)"


def resumption_notice(role: Any, content: Any, metadata: Dict[str, Any]) -> Optional[str]:
    """旧形式の中断の再開文の行なら、今の書き手が書く形の本文。違えば None (規則の版 5)。

    条件は全部の AND:

    - role=user で、ユーザーの発言の来歴 (:func:`_has_user_utterance_provenance`) を持たない
    - 書き手の来歴を持つ: metadata に ``schedule_id`` (スケジュールの発火) か ``slot_id``
      (一日の予定のコマの開始) がある。再開する Pulse の要求の metadata を、旧
      ``run_meta_user`` がそのまま記録していた。会話の取り込みが本人の発言として
      保存した行 (同じ文面を貼り付けた発言でありうる) はこれを持たないので、確認待ちに残る
    - 本文の全体が :data:`_RESUMPTION_RE` の形で、引用した「前回のプロンプト」と末尾に
      並べたプロンプトが一字も違わない
    - そのプロンプトが ``<system>`` の包み一つで全体を囲んでいる (元のプロンプトが機構の
      文 — スケジュールの発火文やコマの開始の文 — だったこと)

    本文は、今の ``_build_resumption_prompt`` がタグ無しのプロンプトから組む形と同じに
    する (頭の文 + 剥がしたプロンプト、空行、剥がしたプロンプト)。今の書き手はこの文を
    スケジュールの Pulse の入力として、系統 ``schedule`` の通知で記録する
    (sea/runtime.py の ``run_meta_user``)。
    """
    if not isinstance(role, str) or role.lower() != "user":
        return None
    if not isinstance(content, str) or _has_user_utterance_provenance(metadata):
        return None
    if metadata.get("schedule_id") is None and not metadata.get("slot_id"):
        return None
    m = _RESUMPTION_RE.fullmatch(content)
    if m is None or m.group("quoted") != m.group("prompt"):
        return None
    prompt = strip_system_tags(m.group("prompt"))
    if prompt is None:
        return None
    return f"{_RESUMPTION_HEAD}{prompt}\n\n{prompt}"


def classify_tagged_message_v3(
    role: Any,
    content: Any,
    metadata: Dict[str, Any],
    *,
    playbook_names: AbstractSet[str] = frozenset(),
) -> Optional[str]:
    """版 3 で足した、タグ入り本文の role=user 行の規則。系統名を返す。対象外は None。

    docs/intent/system_notice_delivery.md「追補」の移行の規則の第二版の 1〜5 と 7。
    :func:`classify_tagged_message` と :func:`legacy_notice` が系統を決められなかった
    行にだけ使う (:func:`_classify_persona_row`)。上から最初に当たったもの一つ:

    1. tags に ``tool`` → ``tool_result``
    2. tags に ``internal`` → ``playbook_memorize``
    4. 剥がした本文が :data:`_ARCHIVED_SUBLINE_REPORT_PREFIX` で始まる →
       ``subline_report`` (退役した Playbook の結果報告の行は tags に Playbook 名を
       持つので、3 より先に見る — 既存の判定が ``playbook:`` タグの行で書き出しを
       先に見るのと同じ順)
    3. tags に実在する Playbook 名 (``playbook_names``、:data:`_GENERAL_TAGS` を
       除く) → ``playbook_memorize``
    5. 音声入力の案内文 (:func:`is_voice_input_notice`) → :data:`VOICE_INPUT_NOTICE_KIND`
       (呼び出し側が二行に分割する)

    1〜4 には来歴の縛り (:func:`_has_user_utterance_provenance`) を AND で掛ける。
    5 には掛けない (代わりに、変換の手前で元の発言の行が画面からの送信でないことを
    確かめる — :class:`BuildingSourceRows`)。規則 7 (定型の機構テキスト) は、本番の写しで出どころのコードを
    特定できた文面が無かったので、今は持たない。
    """
    if not starts_with_system_tag(content):
        return None
    if not isinstance(role, str) or role.lower() != "user":
        return None
    tags = {t for t in _tags_of(metadata) if not t.startswith("pulse:")}
    if not _has_user_utterance_provenance(metadata):
        if "tool" in tags:
            return "tool_result"
        if "internal" in tags:
            return "playbook_memorize"
        if (strip_system_tags(content) or "").startswith(_ARCHIVED_SUBLINE_REPORT_PREFIX):
            return "subline_report"
        if (tags - _GENERAL_TAGS) & set(playbook_names):
            return "playbook_memorize"
    if is_voice_input_notice(strip_system_tags(content), metadata):
        return VOICE_INPUT_NOTICE_KIND
    return None


#: 旧 persona/mixins/emotion.py (2026-06 に撤去) の ``_format_emotion_summary`` が
#: 組んで、``add_to_persona_only`` で role=system のまま記憶に書いた感情パラメータの
#: 変動の知らせ。一行は ``<名前>: mean <±差> → <値>, var <±差> → <値>``。
_EMOTION_SUMMARY_RE = re.compile(
    r'<div class="note-box">感情パラメータ変動<br>'
    r"[^<>:]+: mean [+-]\d+\.\d → -?\d+\.\d, var [+-]\d+\.\d → -?\d+\.\d"
    r"(?:<br>[^<>:]+: mean [+-]\d+\.\d → -?\d+\.\d, var [+-]\d+\.\d → -?\d+\.\d)*"
    r"</div>"
)
#: その系統名 (出どころの関数名から)。
EMOTION_SUMMARY_KIND = "emotion_summary"

#: builtin_data/tools/thread_switch.py の ``switch_active_thread`` が書く行の系統名
#: (同じファイルの ``THREAD_SWITCH_NOTICE_KIND`` と同じ値)。その行は、移動元の
#: スレッドの行への参照 ``other_thread_messages`` を metadata に持つか (移動元に行が
#: あった場合)、``summary`` が渡されなかったときの本文
#: (:data:`_THREAD_SWITCH_PREFIX` で始まる) を持つ。``other_thread_messages`` を書く
#: のはこのツールだけ。
THREAD_SWITCH_KIND = "thread_switch"
_THREAD_SWITCH_PREFIX = "[thread switch] moved from "
#: scripts/import_chatgpt_conversations.py が取り込んだ会話の先頭に書く見出しの行の
#: 系統名 (同じファイルの ``CHATGPT_IMPORT_NOTICE_KIND`` と同じ値)。
CHATGPT_IMPORT_KIND = "chatgpt_import"
#: その見出しの本文の全体の形。題 (``record.title``) と会話の id (``conversation_id``
#: か、無ければ ``identifier`` — 題がそのまま入ることもある) は任意の文字列で、時刻は
#: ``format_datetime`` の形 (秒まで、UTC の ``Z``)。削除された旧 Gradio の管理画面
#: (コミット 41ee80d9 で削除) も同じ形で書いていた。
_CHATGPT_IMPORT_HEADER_RE = re.compile(
    r'\[Imported ChatGPT conversation ".*" \(.*\) created \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]',
    re.DOTALL,
)
#: sea/runtime_nodes.py の ``lg_stelis_start_node`` が親のスレッドに書く Stelis の開始の
#: 行の系統名 (同じファイルの ``STELIS_ANCHOR_NOTICE_KIND`` と同じ値)。その行は本文が
#: 空で、metadata の ``type`` が ``stelis_anchor``、``stelis_thread_id`` に Stelis の
#: スレッドの id を持つ。v0.3.27 の後もこの書き手は印の無いまま書いていた。
STELIS_ANCHOR_KIND = "stelis_anchor"
#: 旧 persona/mixins/movement.py の ``_handle_movement`` (コミット f915bf22 で削除) が、
#: 別の City への移動 (``dispatch_callback``) に失敗したときに role=system で書いた案内
#: の系統名。本文は :data:`_CITY_DISPATCH_FAILURE_PREFIX` に失敗の理由を続けたものか、
#: ``dispatch_callback`` が無いときの :data:`_CITY_DISPATCH_UNAVAILABLE` そのもの。
CITY_DISPATCH_FAILURE_KIND = "city_dispatch_failure"
_CITY_DISPATCH_FAILURE_PREFIX = "別のCityへの移動に失敗しました。"
_CITY_DISPATCH_UNAVAILABLE = "別のCityへ移動する機能が設定されていません。"
#: 旧 builtin_data/tools/get_since_last_user_conversation.py の
#: ``_save_summary_to_memory`` (コミット 1b9ede25 で削除) が書いた要約の行の系統名。
#: その行は metadata に ``summary_uuid`` (uuid4 の文字列) と、tags の
#: ``since_user_conversation`` を持ち、本文は ``[要約 <summary_uuid の先頭 8 文字>] `` で
#: 始まる。
SINCE_USER_CONVERSATION_SUMMARY_KIND = "since_user_conversation_summary"
_SINCE_USER_CONVERSATION_TAG = "since_user_conversation"


def _is_stelis_anchor_row(metadata: Dict[str, Any], content: Any) -> bool:
    """``lg_stelis_start_node`` が書いた Stelis の開始の行の形か (規則の版 4)。"""
    thread_id = metadata.get("stelis_thread_id")
    return (
        metadata.get("type") == "stelis_anchor"
        and isinstance(thread_id, str) and bool(thread_id)
        and (content is None or (isinstance(content, str) and not content.strip()))
    )


def _is_since_user_conversation_summary_row(
    metadata: Dict[str, Any], tags: AbstractSet[str], content: Any,
) -> bool:
    """``_save_summary_to_memory`` が書いた要約の行の形か (規則の版 4)。"""
    summary_uuid = metadata.get("summary_uuid")
    return (
        isinstance(summary_uuid, str) and len(summary_uuid) >= 8
        and _SINCE_USER_CONVERSATION_TAG in tags
        and isinstance(content, str)
        and content.startswith(f"[要約 {summary_uuid[:8]}] ")
    )


def _unmarked_system_rule(
    metadata: Dict[str, Any], content: Any = None,
) -> Optional[Tuple[str, int]]:
    """印の無い role=system 行の (系統名, その規則を足した版)。推定できなければ None。"""
    tags = _tags_of(metadata)
    if "spell" in tags or "pre_spell" in tags:
        return "spell_result", 1
    if "handy_tool" in tags:
        return "handy_tool", 1
    if {"permission", "denied"} <= tags:
        return "playbook_permission", 1
    if {"error", "exec"} <= tags:
        return "playbook_exec_error", 1
    if "tool" in tags:
        return "tool_result", 1
    if _has_playbook_tag(tags):
        return "playbook_memorize", 1
    # 版 3 (規則 6)
    if "perception" in tags:
        return "perception", 3
    if isinstance(content, str) and _EMOTION_SUMMARY_RE.fullmatch(content):
        return EMOTION_SUMMARY_KIND, 3
    # 版 4
    if "other_thread_messages" in metadata or (
        isinstance(content, str) and content.startswith(_THREAD_SWITCH_PREFIX)
    ):
        return THREAD_SWITCH_KIND, 4
    if isinstance(content, str) and _CHATGPT_IMPORT_HEADER_RE.fullmatch(content):
        return CHATGPT_IMPORT_KIND, 4
    if _is_stelis_anchor_row(metadata, content):
        return STELIS_ANCHOR_KIND, 4
    if isinstance(content, str) and (
        content.startswith(_CITY_DISPATCH_FAILURE_PREFIX) or content == _CITY_DISPATCH_UNAVAILABLE
    ):
        return CITY_DISPATCH_FAILURE_KIND, 4
    if _is_since_user_conversation_summary_row(metadata, tags, content):
        return SINCE_USER_CONVERSATION_SUMMARY_KIND, 4
    return None


def classify_unmarked_system_message(
    metadata: Dict[str, Any], content: Any = None,
) -> Optional[str]:
    """印 (``__system_notice__``) の無い role=system 行の系統名。推定できなければ None。

    tags から推定する (版 1)。版 3 で、``perception`` タグの行 (知覚の旧世代) と、
    本文が感情パラメータの変動の知らせ (:data:`_EMOTION_SUMMARY_RE`) の行を足した。
    版 4 で、``switch_active_thread`` が書いた行 (:data:`THREAD_SWITCH_KIND`) と、
    ChatGPT の会話の取り込みの見出しの行 (:data:`CHATGPT_IMPORT_KIND`)、Stelis の開始の
    行 (:data:`STELIS_ANCHOR_KIND`)、別の City への移動の失敗の案内
    (:data:`CITY_DISPATCH_FAILURE_KIND`)、``get_since_last_user_conversation`` の要約の行
    (:data:`SINCE_USER_CONVERSATION_SUMMARY_KIND`) を足した。
    """
    hit = _unmarked_system_rule(metadata, content)
    return hit[0] if hit else None


def _builtin_playbooks_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "builtin_data" / "playbooks"


def known_playbook_names(main_db: Optional[Path] = None) -> Tuple[FrozenSet[str], bool]:
    """規則 3 の「実在する Playbook 名」と、それを欠けなく読めたか。

    builtin_data/playbooks/ (archive も含む) のファイルの ``name`` と、本体 DB の
    ``playbooks`` 表の ``name`` (読み取り専用で開く)。読めないファイル・本体 DB が
    あれば、読めた分の名前と ``False`` を返す — 欠けた名前の集合で走査すると、
    規則 3 に当たるはずの行が確認待ちに回る。呼び出し側はその起動で完了の印を
    立てず、次の起動で読み直す (:data:`MARKER_HELD_PLAYBOOK_NAMES`)。本体 DB が
    無い・``playbooks`` 表が無いのは欠けではない (世界にその名前が無い)。
    """
    names = set()
    complete = True
    root = _builtin_playbooks_dir()
    try:
        paths = sorted(root.rglob("*.json")) if root.is_dir() else None
    except OSError:
        paths = None
    if paths is None:
        # 同梱の Playbook の置き場はリポジトリに必ずある。見えないのは読めないとき
        LOGGER.warning("[system-notice-migration] could not list playbook files in %s", root)
        complete = False
        paths = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            LOGGER.warning(
                "[system-notice-migration] could not read playbook file %s", path,
                exc_info=True,
            )
            complete = False
            continue
        name = data.get("name") if isinstance(data, dict) else None
        if isinstance(name, str) and name:
            names.add(name)
    if main_db is not None and main_db.is_file():
        try:
            with closing(_open_readonly(main_db)) as conn:
                if _table_exists(conn, "playbooks"):
                    names.update(
                        r[0] for r in conn.execute("SELECT name FROM playbooks")
                        if isinstance(r[0], str) and r[0]
                    )
        except sqlite3.Error:
            LOGGER.warning(
                "[system-notice-migration] could not read playbook names from %s",
                main_db, exc_info=True,
            )
            complete = False
    return frozenset(names), complete


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
#: 確認待ちの印の値・レポートの理由: この環境で新しい形式に切り替わった時刻
#: (:func:`resolve_cutover`) より後に作られた行。どの規則に当たっても自動では
#: 変換しない — その時刻より後は、機構が旧形式の本文を書かないので、旧形式の
#: 形をした行は機構の書いたものと言い切れない (ユーザーが案内文などを真似た
#: 発言でありうる)。
AFTER_CUTOVER_REASON = "新しい形式に切り替わった後に作られた行"
#: 確認待ちの印の値・レポートの理由: 発話者 (ユーザー・ペルソナ) の行が添付
#: (``media`` / ``images`` / ``documents``) を持つので、規則に当たっても行ごとは変換
#: しなかった行。行ごと role=system にすると、発話者の添付が機構の通知の持ち物になる
#: (docs/intent/system_notice_delivery.md 不変条件 7)。例外は規則 5 の二行分割だけ
#: (添付は分けた音声の行に残る — :func:`_queue_voice_split`)。
SPEAKER_ATTACHMENT_REASON = "発話者の添付を持つ行"


class CutoverUnavailable(Exception):
    """新しい形式に切り替わった時刻を決められない・記録できない (その起動では何も変換しない)。"""


#: 新しい形式に切り替わった時刻の記録のファイル名 (``<SAIVERSE_HOME>/backups/
#: system_notice_migration/`` の下)。
CUTOVER_FILE_NAME = "cutover.json"

#: 記憶 DB の完了の印を見送った理由 (:attr:`StoreResult.marker_held`)。完了の印は
#: 「今の版の規則を、欠けの無い材料で、この DB に届きうる旧形式の行の全部へ当てた」
#: ことの記録なので、その前提が欠けた起動では立てない (次の起動で走査し直す)。
#:
#: - 規則 3 の材料 (実在する Playbook 名) を完全に読めなかった (:func:`known_playbook_names`)
MARKER_HELD_PLAYBOOK_NAMES = "playbook_names_incomplete"
#: - 本体 DB の移行が失敗した。本体 DB の送信トレイ・台帳には、これから記憶へ
#:   書かれる旧形式の本文が残りうる (:func:`scan_main_conn`)
MARKER_HELD_MAIN_DB_FAILED = "main_db_failed"
#: - 前の起動の、この DB の仮の結果の JSON の採否がまだ決まっていない。印を書き換える
#:   と、その JSON に書いた印の値と一致しなくなり、コミット済みの結果が捨てられる
#:   (:func:`_settle_staged_results`)
MARKER_HELD_STAGED_RESULT = "staged_result_unresolved"
#: - 走査の対象 (タグ始まり・role=system) の行に、metadata が辞書として読めない行が
#:   ある (:attr:`StoreResult.unreadable_metadata_count`)。その行には確認待ちの印を
#:   付けられず (元の metadata を失う)、実会話として扱われ続ける。完了の印を立てると
#:   二度と走査されないので、行が残る間は立てずに毎起動で画面に出す
MARKER_HELD_UNREADABLE_METADATA = "unreadable_metadata"

#: 変換をコミットした回の記録 (:func:`_settle_staged_results` が仮の結果の JSON の採否を
#: 決めるのに使う)。記憶 DB は ``embed_metadata`` の鍵 ``<この接頭辞><起動のフォルダ名>``、
#: 本体 DB は ``execution_ledger`` の KIND :data:`COMMIT_LEDGER_KIND` の行
#: (IDEMPOTENCY_KEY が起動のフォルダ名、STATUS ``completed``)。
COMMIT_RECORD_KEY_PREFIX = "system_notice_migration_commit:"
COMMIT_LEDGER_KIND = "system_notice_migration.commit"
#: 本体 DB に ``execution_ledger`` が無いので、変換をコミットしなかったときの理由
#: (:func:`migrate_main_db`。レポートの「エラー」の行に載る)。
MAIN_DB_NO_COMMIT_RECORD_ERROR = (
    "本体 DB に execution_ledger の表が無く、この回の変換をコミットした記録を書けないので、"
    "本体 DB は移行していません (変換する行は元のまま残っています)。execution_ledger の表が"
    "作られた後の起動で再試行します"
)


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
    #: 走査の対象 (タグ始まり・role=system) なのに metadata が辞書として読めない
    #: (壊れた JSON・配列など) ので、変換も確認待ちの印も付けなかった行
    #: (:func:`_read_metadata`)。人の目で確かめるためにレポートへ出す。
    unreadable_metadata_count: int = 0
    unreadable_metadata_samples: List[Skipped] = field(default_factory=list)
    #: 当てる UPDATE 文 (SQL, 引数)。走査は読むだけで、書くのは :func:`_apply`。
    updates: List[Tuple[str, tuple]] = field(default_factory=list)
    already_done: bool = False
    error: Optional[str] = None
    #: 確認待ちの印を付けるべき行があるのに、metadata の列が無い古いスキーマなので
    #: 付けられなかったか。真なら完了の印を立てない (:func:`migrate_persona_db`)。
    review_marks_deferred: bool = False
    #: 完了の印を立てる前提が欠けていたので、変換だけをコミットして完了の印を
    #: 見送った理由 (``MARKER_HELD_*``)。空なら見送っていない。次の起動で走査し直す。
    marker_held: List[str] = field(default_factory=list)
    #: 古い版の規則で移行を済ませた記憶 DB を、新しい版で足した規則だけで走査し
    #: 直したとき、その印の版 (:data:`MIGRATION_RULES_VERSION`)。全体を走査したら None。
    rescan_from_version: Optional[int] = None
    #: この結果のコミットで立てた完了の印の値 (立てていなければ None)。孤児の
    #: 仮の結果の JSON を採用してよいかを、DB の印と突き合わせて決めるのに使う
    #: (:func:`regenerate_pending_reports`)。
    marker_value: Optional[str] = None
    #: この結果の変換をコミットした取引が DB に書いた、その回の記録の名前 (起動の
    #: フォルダ名 — :data:`COMMIT_RECORD_KEY_PREFIX` / :data:`COMMIT_LEDGER_KIND`)。
    #: 変換をコミットしなかった回は None。完了の印を見送った回・本体 DB でも、仮の
    #: 結果の JSON をこの記録と突き合わせて採用できる (:func:`_settle_staged_results`)。
    commit_record: Optional[str] = None
    #: ``changes`` が変換した全行か。全行を JSON に保存する前の版の結果の JSON から
    #: 読み戻すと、``changes`` は系統ごとのサンプルだけなので偽になる。
    changes_complete: bool = True
    #: 結果の JSON から読み戻したときの系統別件数。
    restored_counts: Optional[Dict[Tuple[str, str], int]] = None
    #: この起動でこの結果をレポートの材料 (結果の JSON) に書いたか。JSON には載せない。
    result_saved: bool = False
    #: この起動でこの結果を材料に書こうとして失敗したか (レポートから詳細が欠ける)。
    #: JSON には載せない。
    result_save_failed: bool = False
    #: 本体 DB に ``execution_ledger`` が無く、その回の変換の記録を書けないので、変換を
    #: コミットしなかったか (:data:`MAIN_DB_NO_COMMIT_RECORD_ERROR`)。画面の警告を分ける
    #: のに使う。JSON には載せない (理由は ``error`` に載る)。
    commit_record_unavailable: bool = False

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
            or self.untagged_memorize_count or self.unreadable_metadata_count
            or self.marker_held or self.error
        )

    @property
    def has_leftovers(self) -> bool:
        """変換しなかった行・推定できなかった行・旧 MEMORIZE の候補・metadata の読めない行があるか (人の確認が要る行)。"""
        return bool(
            self.skipped or self.unknown_system_count or self.untagged_memorize_count
            or self.unreadable_metadata_count
        )

    def leftover_counts(self) -> List[int]:
        return [
            len(self.skipped), self.unknown_system_count, self.untagged_memorize_count,
            self.unreadable_metadata_count,
        ]

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
        if self.unreadable_metadata_count:
            # 記憶 DB だけに出る行。無いときは並びに足さない (本体 DB の前回の
            # 並びと、この欄の無かった版の並びが一致したままになる)
            keys += [
                f"unreadable_metadata|{s.table}|{s.row_id}"
                for s in self.unreadable_metadata_samples
            ]
            keys.append(f"count|unreadable_metadata|{self.unreadable_metadata_count}")
        return sorted(keys)

    def to_json(self) -> Dict[str, Any]:
        """レポートを組み直せる中身 (件数・変換した全行・残した行) を JSON にできる形で。

        ``changes`` は変換した全行 (表・行 id・系統・変更前後の全文)。変換を復元・目視
        できる記録はこれだけなので、サンプルに切り詰めない。``samples`` は全行を
        持たない前の版の読み手のために残す。
        """
        from dataclasses import asdict

        return {
            "label": self.label,
            "path": self.path,
            "persona": self.persona,
            "backup_path": self.backup_path,
            "already_done": self.already_done,
            "error": self.error,
            "review_marks_deferred": self.review_marks_deferred,
            "marker_held": list(self.marker_held),
            "rescan_from_version": self.rescan_from_version,
            "marker_value": self.marker_value,
            "commit_record": self.commit_record,
            "counts": [[t, k, n] for (t, k), n in sorted(self.counts().items())],
            "changes": [asdict(c) for c in self.changes] if self.changes_complete else None,
            "samples": [asdict(c) for c in _sample_changes(self.changes)],
            "skipped": [asdict(s) for s in self.skipped],
            "unknown_system_count": self.unknown_system_count,
            "unknown_system_samples": [asdict(s) for s in self.unknown_system_samples],
            "untagged_memorize_count": self.untagged_memorize_count,
            "untagged_memorize_samples": [asdict(s) for s in self.untagged_memorize_samples],
            "unreadable_metadata_count": self.unreadable_metadata_count,
            "unreadable_metadata_samples": [
                asdict(s) for s in self.unreadable_metadata_samples
            ],
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "StoreResult":
        full = data.get("changes")
        complete = isinstance(full, list)
        return cls(
            label=data["label"],
            path=data["path"],
            persona=bool(data.get("persona")),
            backup_path=data.get("backup_path"),
            already_done=bool(data.get("already_done")),
            error=data.get("error"),
            review_marks_deferred=bool(data.get("review_marks_deferred")),
            marker_held=[str(r) for r in data.get("marker_held") or []],
            rescan_from_version=data.get("rescan_from_version"),
            marker_value=data.get("marker_value"),
            commit_record=data.get("commit_record"),
            changes_complete=complete,
            restored_counts={(t, k): n for t, k, n in data.get("counts") or []},
            changes=[Change(**c) for c in (full if complete else data.get("samples") or [])],
            skipped=[Skipped(**s) for s in data.get("skipped") or []],
            unknown_system_count=int(data.get("unknown_system_count") or 0),
            unknown_system_samples=[Skipped(**s) for s in data.get("unknown_system_samples") or []],
            untagged_memorize_count=int(data.get("untagged_memorize_count") or 0),
            untagged_memorize_samples=[
                Skipped(**s) for s in data.get("untagged_memorize_samples") or []
            ],
            unreadable_metadata_count=int(data.get("unreadable_metadata_count") or 0),
            unreadable_metadata_samples=[
                Skipped(**s) for s in data.get("unreadable_metadata_samples") or []
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


def _created_after_cutover(created_at: Any, cutover: Optional[int]) -> bool:
    """行の作成時刻が、新しい形式に切り替わった時刻 (秒に切り捨てた epoch 秒) と同じ秒か、それより後か。

    ``cutover`` が None なら境を当てない (偽)。起動の入口 (:func:`run_migration`) は
    必ず渡す — None で呼ぶのは、境と関係の無い規則を確かめる呼び出しだけ。

    - 数 (epoch 秒) と数の文字列は秒に切り捨てて比べる。本体 DB の
      ``building_messages.created_at`` の形 (SQLite の ``CURRENT_TIMESTAMP``、UTC の
      ``YYYY-MM-DD HH:MM:SS``) と ISO の文字列は UTC として読む (時差の無い値は UTC)。
    - 切り替わりの時刻と同じ秒の行は後に数える (自動では変換せず確認待ちにする)。
      保存先ごとに時刻の精度が違い (本体 DB の ``created_at`` は秒まで)、同じ秒の中の
      前後を決められないので、確認待ちの側に倒す。
    - 作成時刻の無い行 (NULL・列の無い古いスキーマ) は前に数える。今の書き手
      (記憶の ``add_message``・建物の履歴・実行台帳・送信トレイ・知覚のまとめ) は
      必ず作成時刻を書くので、無い行は古いスキーマか外部からの取り込みの行。
    - 読めない値 (形の崩れた文字列など) は後に数える — 自動では変換せず確認待ちに倒す。
    """
    if cutover is None or created_at is None:
        return False
    if isinstance(created_at, bool):
        return True
    if isinstance(created_at, (int, float)):
        try:
            return math.floor(created_at) >= cutover
        except (ValueError, OverflowError):  # NaN・無限大
            return True
    if isinstance(created_at, str):
        text = created_at.strip()
        try:
            return math.floor(float(text)) >= cutover
        except (ValueError, OverflowError):
            pass
        at = _parse_utc(text)
        return True if at is None else math.floor(at.timestamp()) >= cutover
    return True


def _parse_utc(text: Any) -> Optional[datetime]:
    """ISO の時刻 (``YYYY-MM-DD HH:MM:SS`` も含む) を UTC の aware な時刻として読む。読めなければ None。"""
    try:
        at = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# ペルソナの記憶 DB
# ---------------------------------------------------------------------------


def _mark_for_review(
    result: StoreResult, row_id: Any, metadata: Dict[str, Any], reason: str,
    *, replace: bool = False,
) -> None:
    """レポート送りにした messages の行へ、確認待ちの印を付ける UPDATE を積む。

    印 (``SYSTEM_NOTICE_REVIEW_KEY``) は本文と role を変えず、その行を「実会話」
    (sai_memory/memory/storage.py の ``_conversation_exclusion``) から外す。既に
    印がある行には積まない (印を消して流し直しても二度書かない)。ただし
    ``replace`` なら、印の理由が ``reason`` と違うときだけ書き換える — 規則に
    当たったのに変換できなかった行 (分けられない・タグの対応が曖昧) は、前の版が
    付けた「系統を判定できなかった」より、その理由のほうが行の正しい説明になる。
    ``metadata`` は辞書として読めた行のものだけを渡す (:func:`_read_metadata` —
    読めない行は印だけの metadata に置き換えると元の metadata が失われるので、
    呼び出し側が渡さない)。metadata の列が無い古いスキーマでは書けないので、
    呼び出し側が呼ばない。
    """
    from sai_memory.memory.storage import SYSTEM_NOTICE_REVIEW_KEY

    current = metadata.get(SYSTEM_NOTICE_REVIEW_KEY)
    if current and (not replace or current == reason):
        return
    new_meta = dict(metadata)
    new_meta[SYSTEM_NOTICE_REVIEW_KEY] = reason
    result.updates.append((
        "UPDATE messages SET metadata = ? WHERE id = ?",
        (_dump_json(new_meta), row_id),
    ))


def _read_metadata(raw: Any) -> Optional[Dict[str, Any]]:
    """記憶 DB の行の metadata の列を、書き換えてよい辞書として読む。

    空 (NULL・空文字) は空の辞書 — 書いても失うものが無い。JSON として壊れて
    いる・辞書でない (配列・文字列・``null`` など) 値は None。None の行の metadata
    は、変換でも確認待ちの印でも上書きしない (上書きすると元の値が失われる)。
    """
    if raw is None or raw == "":
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _notice_metadata(metadata: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """通知に変換する行の新しい metadata (通知の印を足す)。

    確認待ちの印 (``SYSTEM_NOTICE_REVIEW_KEY``) は理由を問わず外す — 変換した行は
    もう確認を待っていない。印が残ると、確認待ちの行を確定する画面の入口が消え
    なくなり、「実会話」からの除外も続く。
    """
    from sai_memory.memory.storage import SYSTEM_NOTICE_KEY, SYSTEM_NOTICE_REVIEW_KEY

    new_meta = dict(metadata)
    new_meta[SYSTEM_NOTICE_KEY] = kind
    new_meta.pop(SYSTEM_NOTICE_REVIEW_KEY, None)
    return new_meta


@dataclass(frozen=True)
class _RowMatch:
    """記憶 DB の messages の一行に当たった規則。"""

    #: ``__system_notice__`` に書く系統名。
    kind: str
    #: レポートの件数とサンプルの表記。
    label: str
    #: 剥がした本文。印を付けるだけの role=system 行は元の本文。None はタグの対応が曖昧。
    body: Optional[str]
    #: その規則を足した版 (:data:`MIGRATION_RULES_VERSION`)。
    version: int
    #: role=system の行に印を付けるだけか (本文と role は変えない)。
    mark_only: bool = False


def _classify_persona_row(
    role: Any,
    content: Any,
    metadata: Dict[str, Any],
    *,
    playbook_names: AbstractSet[str],
) -> Optional[_RowMatch]:
    """記憶 DB の messages の一行に当たる規則 (無ければ None)。

    上から最初に当たったもの一つ: 印の AND 判定 (:func:`classify_tagged_message`、
    版 1) → 旧世代の個別条件 (:func:`legacy_notice`、版 2) → 版 3 の規則
    (:func:`classify_tagged_message_v3`)。印の無い role=system 行は
    :func:`_unmarked_system_rule`。走査し直しは、当たった規則の版が印の版より
    新しい行だけを変換する — 古い版の規則に当たる行は、その版の走査が変換したか
    確認待ちにしてある。

    旧形式の中断の再開文 (:func:`resumption_notice`、版 5) も旧規則より先に見る。

    発話者の添付を持つ role=user の行で、規則 5 (音声入力の案内文) の形のものは、
    旧規則より先に規則 5 にする — 旧規則のタグ (``event_message`` など) を持っていても、
    規則 5 の出どころの確認と二行分割を通す。それ以外の規則に当たった、発話者の添付を
    持つ行は、変換の手前で確認待ちにする (:func:`_queue_conversion`)。
    """
    from sai_memory.memory.storage import SYSTEM_NOTICE_KEY

    if starts_with_system_tag(content):
        if is_voice_split_row(role, content, metadata):
            return _RowMatch(
                VOICE_INPUT_NOTICE_KIND, VOICE_INPUT_NOTICE_KIND, strip_system_tags(content), 3,
            )
        # 旧形式の中断の再開文 (版 5)。包みの組が二つ並ぶので、印の AND 判定に当たっても
        # 本文を剥がせず「タグの対応が曖昧」になっていた形。形が狭いので先に見る
        resumed = resumption_notice(role, content, metadata)
        if resumed is not None:
            return _RowMatch(LEGACY_NOTICE_KIND, RESUMPTION_LABEL, resumed, 5)
        kind = classify_tagged_message(role, content, metadata)
        if kind is not None:
            return _RowMatch(kind, kind, strip_system_tags(content), 1)
        legacy = legacy_notice(role, content, metadata)
        if legacy is not None:
            label, body = legacy
            return _RowMatch(LEGACY_NOTICE_KIND, label, body, 2)
        kind = classify_tagged_message_v3(
            role, content, metadata, playbook_names=playbook_names,
        )
        if kind is not None:
            return _RowMatch(kind, kind, strip_system_tags(content), 3)
        return None
    role_s = role.lower() if isinstance(role, str) else ""
    if role_s != "system" or metadata.get(SYSTEM_NOTICE_KEY):
        return None
    hit = _unmarked_system_rule(metadata, content)
    if hit is None:
        return None
    kind, version = hit
    return _RowMatch(kind, kind, str(content or ""), version, mark_only=True)


#: 音声入力の行を二行に分けるとき、元の行から新しい行へ写す列 (行の並びと所属を
#: 決める列。sai_memory/memory/storage.py の ``add_message`` が書く列のうち、
#: 応答の行だけが持つ ``paired_action_text`` / ``thought_signature`` /
#: ``spell_origin_id`` / ``spell_seq`` は写さない)。
_SPLIT_COPIED_COLUMNS = (
    "thread_id", "resource_id", "created_at", "origin_track_id", "line_role", "line_id",
    "scope", "pulse_id", "origin_episode",
)
#: 音声入力の行を分けて足した行の、レポートの件数とサンプルの表の表記 (行の数は
#: 変換した行の数に数えない)。
SPLIT_ADDED_TABLE = "messages(音声の行を分けて足した行)"
#: 二行に分けた後の新しい行が、元の行の直後に並べられない (同じ時刻で後ろの
#: rowid の行が既にある) ので、変換せずに残した行の理由。
SPLIT_POSITION_REASON = "同じ時刻の後ろに別の行があり、音声の行を通知の直後に置けない"
#: 規則に当たったが、元の発言の行 (本体 DB の ``building_messages`` の行 —
#: :class:`BuildingSourceRows`) が画面からの送信の印 ``client_message_id`` を持つので、
#: 変換せずに残した行の理由。どの版のどの規則に当たった行でも同じ
#: (:func:`_queue_conversion`)。画面やチャットの送信 API から人が送った発言で、旧形式の
#: 本文や tags は送り手が真似て書けるので、機構の書いたものと言い切れない。本体 DB の
#: ``voice_call`` の行も、この印を持てば変換しない (:func:`scan_main_conn`)。
SENT_FROM_SCREEN_REASON = "元の発言の行が画面からの送信の印 (client_message_id) を持つ"
#: 規則 5 に当たったが、元の発言の行を本体 DB で一つに決められなかった (見つからない・
#: 複数ある・本体 DB を読めない) ので、変換せずに残した行の理由。
VOICE_SOURCE_UNKNOWN_REASON = "音声入力の案内文の形だが、元の発言の行を本体 DB で確かめられなかった"
#: 音声入力の行を分けるとき、通知の行に残す metadata の欄。記録の分類の ``tags`` /
#: ``with`` と、音声がどこを通って届いたかの欄 (中継の名前 ``source``・中継の接続
#: ``gateway_session_id``・機体 ``vessel_id``)。どれもユーザーの添付でも発言の来歴
#: でもない。本番の写しの音声入力の行 104 件が持つ欄は、これと ``media``・
#: ``building_msg_ref``・確認待ちの印だけだった (2026-10-29)。ここに無い欄は通知に
#: 残さない (出どころの分からない欄を機構の名義に置かない)。
_VOICE_NOTICE_KEPT_KEYS = ("tags", "with", "source", "gateway_session_id", "vessel_id")


def split_position_is_free(conn: sqlite3.Connection, cols: AbstractSet[str], rowid: Any, created_at: Any) -> bool:
    """新しく足す行 (元の行と同じ created_at、末尾の rowid) が元の行の直後に並ぶか。

    記憶の読み出しは created_at, rowid の順に並べる。同じ created_at で元の行より
    後ろの rowid の行が (どのスレッドにでも) あると、足した行はその後ろに並ぶ。
    ``cols`` は messages の列の名前。移行と確認の画面の二行分割
    (saiverse/system_notice_review.py) が同じ判定を使う。
    """
    if "created_at" in cols:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE created_at IS ? AND rowid > ? LIMIT 1",
            (created_at, rowid),
        ).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM messages WHERE rowid > ? LIMIT 1", (rowid,)).fetchone()
    return row is None


def is_voice_split_row(role: Any, content: Any, metadata: Optional[Dict[str, Any]]) -> bool:
    """規則 5 の二行分割の対象の形か (role=user・本文が包みで始まる・発話者の添付を持つ・
    剥がした本文と metadata が :func:`is_voice_input_notice`)。

    移行の判定 (:func:`_classify_persona_row`) と、確認の画面の「正式に変換」
    (saiverse/system_notice_review.py の ``allowed_actions``) が同じ関数を使う。
    元の発言の行が画面からの送信かは見ない (移行は変換の手前で別に確かめ、確認の
    画面では所有者が見て決める)。
    """
    if not isinstance(metadata, dict):
        return False
    return (
        isinstance(role, str) and role.lower() == "user"
        and starts_with_system_tag(content)
        and _speaker_has_attachments(role, metadata)
        and is_voice_input_notice(strip_system_tags(content), metadata)
    )


def voice_split_metadata(
    metadata: Dict[str, Any], line_role: Any, kind: str = VOICE_INPUT_NOTICE_KIND,
) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[str]]:
    """規則 5 の二行分割の、通知の行と音声の行の metadata と、音声の行の line_role。

    返り値は ``(notice_meta, user_meta, user_line_role)``。``user_line_role`` が None の
    ときは元の行の line_role をそのまま写す。移行 (:func:`_queue_voice_split`) と確認の
    画面の「正式に変換」(saiverse/system_notice_review.py) が同じ関数を使う。

    通知の metadata には、音声入力を中継した機構が書いた欄
    (:data:`_VOICE_NOTICE_KEPT_KEYS`) と系統の印 ``__system_notice__`` (``kind``) だけを
    置く — 添付 (``media``・``images``・``documents`` と、その項目の説明・アイテムの
    参照)、ユーザーの発言の来歴 (建物履歴からの転記の印 ``building_msg_ref`` など)、
    それに出どころの分からない欄は、ユーザーの持ち物として音声の行にだけ置く。添付が
    通知にも残ると、ユーザーの画像や文書が機構の通知の添付物として二重に届く
    (不変条件 7)。音声の行の metadata は元の行の metadata (確認待ちの印を除く全部)。

    音声の行は実会話 (storage.py の ``filter_real_conversation_ids``) に入る形にする:
    元の行の tags のうち ``MECHANISM_TAGS`` に入るものは写さず、元の行の line_role が
    ``CHRONICLE_EXCLUDED_LINE_ROLES`` に入るなら ``USER_LINE_ROLE`` にする。外したものは
    音声の行の ``REVIEW_CLEARED_KEY`` に残す (saiverse/system_notice_review.py の
    ``ACTION_USER`` と同じ形)。
    """
    from sai_memory.memory.storage import (
        CHRONICLE_EXCLUDED_LINE_ROLES,
        MECHANISM_TAGS,
        SYSTEM_NOTICE_KEY,
        SYSTEM_NOTICE_REVIEW_KEY,
    )
    from saiverse.system_notice_review import REVIEW_CLEARED_KEY, USER_LINE_ROLE

    notice_meta = {k: metadata[k] for k in _VOICE_NOTICE_KEPT_KEYS if k in metadata}
    notice_meta[SYSTEM_NOTICE_KEY] = kind
    user_meta = {k: v for k, v in metadata.items() if k != SYSTEM_NOTICE_REVIEW_KEY}
    cleared: Dict[str, Any] = {}
    tags = user_meta.get("tags")
    if isinstance(tags, list):
        removed = [t for t in tags if t in MECHANISM_TAGS]
        if removed:
            user_meta["tags"] = [t for t in tags if t not in MECHANISM_TAGS]
            cleared["tags"] = removed
    user_line_role: Optional[str] = None
    if line_role in CHRONICLE_EXCLUDED_LINE_ROLES:
        user_line_role = USER_LINE_ROLE
        cleared["line_role"] = line_role
    if cleared:
        user_meta[REVIEW_CLEARED_KEY] = cleared
    return notice_meta, user_meta, user_line_role


def split_insert_statement(
    cols: AbstractSet[str],
    new_id: str,
    source_id: Any,
    *,
    role: str,
    content: str,
    metadata_json: Optional[str],
    line_role: Optional[str] = None,
) -> Tuple[str, Tuple[Any, ...]]:
    """元の行 (``source_id``) の直後に並ぶ新しい行を足す SQL と引数。

    行の並びと所属を決める列 (:data:`_SPLIT_COPIED_COLUMNS` のうち ``cols`` にあるもの)
    は元の行から写す。``line_role`` を渡したときは写さずにその値を書く。移行の
    二行分割と、確認の画面の二行分割 (``SAIMemoryAdapter.rewrite_message`` の
    ``insert_after``) が同じ文を使う。並びが元の行の直後になるかは
    :func:`split_position_is_free` で先に確かめること。
    """
    copied = [c for c in _SPLIT_COPIED_COLUMNS if c in cols]
    extra_cols = extra_vals = ""
    extra_params: Tuple[Any, ...] = ()
    if line_role is not None and "line_role" in cols:
        copied.remove("line_role")
        extra_cols, extra_vals, extra_params = ", line_role", ", ?", (line_role,)
    copied_sql = "".join(f", {c}" for c in copied)
    return (
        f"INSERT INTO messages (id, role, content, metadata{extra_cols}{copied_sql}) "
        f"SELECT ?, ?, ?, ?{extra_vals}{copied_sql} FROM messages WHERE id = ?",
        (new_id, role, content, metadata_json, *extra_params, source_id),
    )


def _queue_voice_split(
    conn: sqlite3.Connection,
    result: StoreResult,
    cols: set,
    row_id: Any,
    content: str,
    metadata: Dict[str, Any],
    match: _RowMatch,
) -> None:
    """音声入力の行を、通知の行 (元の行) と本文の無い音声の行 (新しい行) に分ける。

    元の行はその場で role=system の通知にする (タグを剥がした案内文、
    ``__system_notice__``)。新しい行は role=user・本文 ``""``。両方の metadata と
    新しい行の line_role は :func:`voice_split_metadata`、新しい行を足す文は
    :func:`split_insert_statement` (確認の画面の「正式に変換」と共有)。通知の行
    (元の行) の tags と line_role はそのまま。id は ``add_message`` と同じく
    ``uuid4``。本文が空なので、``add_message`` を呼ぶ記憶の書き込み
    (saiverse_memory/adapter.py) と同じく埋め込みの行は作らない。
    """
    line_role = None
    if "line_role" in cols:
        row = conn.execute("SELECT line_role FROM messages WHERE id = ?", (row_id,)).fetchone()
        line_role = row[0] if row is not None else None
    notice_meta, user_meta, user_line_role = voice_split_metadata(metadata, line_role, match.kind)
    new_id = str(uuid.uuid4())
    result.updates.append((
        "UPDATE messages SET role = 'system', content = ?, metadata = ? WHERE id = ?",
        (match.body, _dump_json(notice_meta), row_id),
    ))
    result.updates.append(split_insert_statement(
        cols, new_id, row_id,
        role="user", content="", metadata_json=_dump_json(user_meta), line_role=user_line_role,
    ))
    result.changes.append(Change("messages", str(row_id), match.label, content, match.body or ""))
    result.changes.append(Change(SPLIT_ADDED_TABLE, new_id, match.label, content, ""))


#: :class:`BuildingSourceRows` が引くのに使う ``building_messages`` の列。
_SOURCE_ROW_COLUMNS = frozenset({
    "id", "building_id", "role", "message_id", "client_message_id", "metadata_json",
})


class BuildingSourceRows:
    """記憶の行の元になった発言の行 (本体 DB の ``building_messages``) を引く (出どころの条件)。

    旧形式の本文・tags・規則 5 の形 (案内文・音声の ``media``・``source`` が
    ``_voice`` で終わる) は、チャットの送信 API の送り手も作れる (``tags`` や
    ``source`` は送り手が書ける欄で、音声は添付として付けられる)。音声入力を中継する
    アドオン (stackchan・Godot ほか) は ``handle_user_input_stream`` を
    ``client_message_id`` なしで呼び、画面からの送信は必ず ``client_message_id`` を
    付ける。だから元の行がこの印を持つかで分ける (:meth:`sent_from_screen`)。
    ``building_msg_ref`` を持つ行はどの規則でも (:func:`_queue_conversion`)、持たない
    行は規則 5 でだけ (音声の ``uri`` で) 引く。

    元の行の引き方:

    - 記憶の行が ``building_msg_ref`` (``"<building_id>:<message_id>"`` —
      builtin_data/tools/get_building_messages.py の転記が 2026-07-21 から必ず刻む) を
      持てば、それと ``building_id`` / ``message_id`` が一致する行。``building_id`` に
      ``:`` が入りうるので、区切りの位置を全部試す。
    - 持たなければ (転記が印を刻む前の行)、音声の ``media`` の ``uri`` が同じ
      ``role='user'`` の行。音声のファイル名は受け取るたびに一意に作られる。比べるのは
      各行の ``metadata_json`` を JSON として解釈した ``media`` の、音声の項目
      (:func:`saiverse.media_utils.is_audio_media_item`) の ``uri`` (生の JSON の文字列では探さない — 同じ uri がエスケープ
      の違う表現で保存されていると、文字列の検索では候補から漏れて、一つに決まったと
      誤認する)。初めて uri で引くときに ``role='user'`` の行を一度だけ全部読んで、
      uri から行への対応を作る (:meth:`_audio_uri_index`)。読んだ行の中に
      ``metadata_json`` を読めない行 (JSON として解釈できない・辞書でない・``media`` が
      一覧でない) が一つでもあれば、その行が同じ音声を持つ元の発言だった可能性を除け
      ないので、その回の uri での引き当ては全部「決められない」(None) にする。
      ``building_msg_ref`` で引く行は、``building_id`` と ``message_id`` の列で引くので
      読めない行に左右されない。

    本体 DB は読み取り専用で、最初に引くときに開く。本体 DB が無い・開けない・
    ``building_messages`` の表か列が無い・引く途中で失敗した場合は、それ以降も
    含めて「決められない」(None) を返す。
    """

    def __init__(self, main_db: Optional[Path]) -> None:
        self._path = main_db
        self._conn: Optional[sqlite3.Connection] = None
        self._unavailable = main_db is None
        #: 音声の uri → その uri を音声として持つ role=user の行 (``id`` → ``client_message_id``)。
        self._uri_index: Optional[Dict[str, Dict[Any, Any]]] = None
        #: role=user の行の ``metadata_json`` を全部読めたか (読めない行があれば、uri での
        #: 引き当ては全部「決められない」— :meth:`_audio_uri_index`)。
        self._uri_index_complete = True

    def __enter__(self) -> "BuildingSourceRows":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _give_up(self, message: str) -> None:
        LOGGER.warning("[system-notice-migration] %s: %s", message, self._path, exc_info=True)
        self.close()
        self._unavailable = True

    def _connection(self) -> Optional[sqlite3.Connection]:
        if self._unavailable or self._path is None:
            return None
        if self._conn is not None:
            return self._conn
        try:
            if not self._path.is_file():
                self._give_up("main DB for the voice input source check is missing")
                return None
            conn = _open_readonly(self._path)
            usable = _table_exists(conn, "building_messages") and _SOURCE_ROW_COLUMNS <= _columns(
                conn, "building_messages",
            )
        except (OSError, sqlite3.Error):
            self._give_up("could not open the main DB for the voice input source check")
            return None
        if not usable:
            conn.close()
            self._give_up("main DB has no building_messages columns for the voice input source check")
            return None
        self._conn = conn
        return conn

    def sent_from_screen(self, metadata: Dict[str, Any]) -> Optional[bool]:
        """元の行が ``client_message_id`` を持てば True、持たなければ False。

        元の行を一つに決められない (見つからない・複数ある)・本体 DB を読めないときは None。
        """
        conn = self._connection()
        if conn is None:
            return None
        try:
            found = self._find(conn, metadata)
        except sqlite3.Error:
            self._give_up("could not read the main DB for the voice input source check")
            return None
        if found is None or len(found) != 1:
            return None
        (client_message_id,) = found.values()
        return bool(client_message_id)

    def _audio_uri_index(self, conn: sqlite3.Connection) -> Dict[str, Dict[Any, Any]]:
        """音声の uri → その uri を音声として持つ role=user の行。

        項目が音声かは :func:`saiverse.media_utils.is_audio_media_item` (音声を読み取る側と
        同じ判定) で決める。``metadata_json`` を読めない行は対応に載せられないので、
        ``_uri_index_complete`` を偽にする (:meth:`_find` が uri での引き当てを
        「決められない」にする)。
        """
        from saiverse.media_utils import is_audio_media_item

        if self._uri_index is not None:
            return self._uri_index
        index: Dict[str, Dict[Any, Any]] = {}
        complete = True
        for row_id, client_message_id, raw in conn.execute(
            "SELECT id, client_message_id, metadata_json FROM building_messages WHERE role = 'user'",
        ):
            if raw is None or raw == "" or raw == b"":
                continue
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                complete = False
                continue
            if value is None:
                continue
            if not isinstance(value, dict):
                complete = False
                continue
            media = value.get("media")
            if media is None:
                continue
            if not isinstance(media, list):
                complete = False
                continue
            for m in media:
                if not is_audio_media_item(m):
                    continue
                uri = m.get("uri")
                if isinstance(uri, str) and uri:
                    index.setdefault(uri, {})[row_id] = client_message_id
        if not complete:
            LOGGER.warning(
                "[system-notice-migration] main DB has role=user building rows whose "
                "metadata_json cannot be read; voice input rows without building_msg_ref "
                "are held for review: %s", self._path,
            )
        self._uri_index = index
        self._uri_index_complete = complete
        return index

    def _find(self, conn: sqlite3.Connection, metadata: Dict[str, Any]) -> Optional[Dict[Any, Any]]:
        """元の行の候補 (``id`` → ``client_message_id``)。

        uri で引くのに、本体 DB に ``metadata_json`` を読めない role=user の行があれば
        None (候補を全部は数えられない)。
        """
        found: Dict[Any, Any] = {}
        if _BUILDING_MSG_REF_KEY in metadata:
            ref = metadata.get(_BUILDING_MSG_REF_KEY)
            if not isinstance(ref, str):
                return found
            for i, ch in enumerate(ref):
                if ch != ":":
                    continue
                for row_id, client_message_id in conn.execute(
                    "SELECT id, client_message_id FROM building_messages "
                    "WHERE building_id = ? AND message_id = ?",
                    (ref[:i], ref[i + 1:]),
                ):
                    found[row_id] = client_message_id
            return found
        from saiverse.media_utils import is_audio_media_item

        media = metadata.get("media")
        uris = {
            m.get("uri") for m in media if is_audio_media_item(m)
        } if isinstance(media, list) else set()
        uris = {uri for uri in uris if isinstance(uri, str) and uri}
        if not uris:
            return found
        index = self._audio_uri_index(conn)
        if not self._uri_index_complete:
            return None
        for uri in uris:
            found.update(index.get(uri, {}))
        return found


def _voice_source_reason(
    source_rows: Optional[BuildingSourceRows], metadata: Dict[str, Any],
) -> Optional[str]:
    """規則 5 の行を変換してよいか。よければ None、だめなら残す理由。

    元の行が ``client_message_id`` を持たない (音声入力を中継するアドオンの発言) と
    分かったときだけ変換する。持つ・確かめられない (``source_rows`` が無い場合も) ときは
    確認待ちにする。
    """
    sent = source_rows.sent_from_screen(metadata) if source_rows is not None else None
    if sent is None:
        return VOICE_SOURCE_UNKNOWN_REASON
    return SENT_FROM_SCREEN_REASON if sent else None


def _queue_conversion(
    conn: sqlite3.Connection,
    result: StoreResult,
    cols: set,
    row: Tuple[Any, ...],
    metadata: Dict[str, Any],
    match: _RowMatch,
    *,
    source_rows: Optional[BuildingSourceRows] = None,
) -> Optional[str]:
    """当たった規則の変換を ``result`` に積む。変換できなければ残す理由を返す。

    ``building_msg_ref`` を持つ行は、元の発言の行 (:class:`BuildingSourceRows`) が画面
    からの送信の印 ``client_message_id`` を持てば、どの規則に当たっても変換しない
    (:data:`SENT_FROM_SCREEN_REASON`)。規則同士の優先順や版に関係なく、ここを通らない
    変換は無い。元の行を一つに決められないときは、規則 5 だけを確認待ちにし
    (:func:`_voice_source_reason`)、ほかの規則は今までどおり変換する — 判定の材料が
    無いのは、転記が印を刻む前の行と同じ。

    発話者の添付を持つ行は、規則 5 の二行分割のほかは変換しない
    (:data:`SPEAKER_ATTACHMENT_REASON`)。印を付けるだけの role=system の行は発話者の
    行ではないので対象外。role=user の行は、ユーザーの発言の来歴
    (:func:`_has_user_utterance_provenance`) を持つときだけ添付が発話者のもの —
    来歴の無い role=user の行の添付 (旧世代の知覚のまとまりの画像など) は機構が
    付けたもので、行ごと変換してよい。
    """
    row_id, role, content, _raw_meta, rowid, created_at = row
    role_s = role.lower() if isinstance(role, str) else ""
    if (
        match.kind != VOICE_INPUT_NOTICE_KIND and _BUILDING_MSG_REF_KEY in metadata
        and source_rows is not None and source_rows.sent_from_screen(metadata) is True
    ):
        return SENT_FROM_SCREEN_REASON
    if (
        not match.mark_only and match.kind != VOICE_INPUT_NOTICE_KIND
        and _speaker_has_attachments(role, metadata)
        and (role_s != "user" or _has_user_utterance_provenance(metadata))
    ):
        return SPEAKER_ATTACHMENT_REASON
    if match.mark_only:
        result.updates.append((
            "UPDATE messages SET metadata = ? WHERE id = ?",
            (_dump_json(_notice_metadata(metadata, match.kind)), row_id),
        ))
        result.changes.append(Change(
            "messages(印の付与)", str(row_id), match.label, str(content or ""), str(content or ""),
        ))
        return None
    if match.body is None:
        return AMBIGUOUS_TAGS_REASON
    if match.kind == VOICE_INPUT_NOTICE_KIND:
        held = _voice_source_reason(source_rows, metadata)
        if held is not None:
            return held
        if not split_position_is_free(conn, cols, rowid, created_at):
            return SPLIT_POSITION_REASON
        _queue_voice_split(conn, result, cols, row_id, content, metadata, match)
        return None
    result.updates.append((
        "UPDATE messages SET role = 'system', content = ?, metadata = ? WHERE id = ?",
        (match.body, _dump_json(_notice_metadata(metadata, match.kind)), row_id),
    ))
    result.changes.append(Change("messages", str(row_id), match.label, content, match.body))
    return None


def scan_persona_conn(
    conn: sqlite3.Connection,
    result: StoreResult,
    *,
    rescan_from_version: Optional[int] = None,
    playbook_names: Optional[AbstractSet[str]] = None,
    cutover: Optional[int] = None,
    source_rows: Optional[BuildingSourceRows] = None,
) -> None:
    """ペルソナの記憶 DB を読んで、変換を ``result`` に積む (書かない)。

    - messages: タグ入り本文の行と、印の無い role=system 行
      (判定は :func:`_classify_persona_row`)。
    - messages.paired_action_text: 列そのものが機構だけが書く指示文の控えなので、
      タグで始まる値は剥がす。
    - perception_batches (kind=realtime_and_recall).rendered_text。

    ``rescan_from_version`` は、その版で移行を済ませた DB の走査し直し
    (:func:`_scan_persona_messages`)。paired_action_text と perception_batches の
    規則は版 1 から変わっていないが、前の版の完了の後に増えた行 (取り込み・合流
    など) もありうるので走査し直しでも当てる (剥がした値はタグで始まらないので、
    前の版が剥がした行は素通りする)。旧 MEMORIZE の結果の候補は数えるだけの一覧で、
    前の版のレポートに載っているので数え直さない。

    所有者が確定した行 (``SYSTEM_NOTICE_REVIEWED_KEY`` を持つ行) は、どの列も
    書き換えず、確認待ちの印も付けない。metadata が辞書として読めない行は上書き
    せず、件数をレポートに出す (:func:`_read_metadata`)。

    ``cutover`` (新しい形式に切り替わった時刻、epoch 秒 — :func:`resolve_cutover`)
    より後に作られた行は、どの列も規則で書き換えない (:func:`_created_after_cutover`)。
    messages の行は確認待ちにし (:data:`AFTER_CUTOVER_REASON`)、paired_action_text と
    perception_batches は書き換えずにレポートへ出す。作成時刻は messages が
    ``created_at``、perception_batches が ``consumed_at`` (そのまとまりを書いた時刻)。

    ``playbook_names`` を渡さなければ、同梱の Playbook のファイルだけから読む
    (欠けなく読めたかは :func:`known_playbook_names`。呼び出し側の
    :func:`migrate_persona_db` は自分で読んで確かめる)。

    ``source_rows`` は規則 5 の行の元の発言の行を引く手段 (:class:`BuildingSourceRows`)。
    渡さなければ、規則 5 の行は元の行を確かめられないので確認待ちにする
    (:data:`VOICE_SOURCE_UNKNOWN_REASON`)。
    """
    if not _table_exists(conn, "messages"):
        return
    cols = _columns(conn, "messages")
    if not {"id", "role", "content"} <= cols:
        # 判定に要る列が無い DB は対象が無い (変換もしない)
        return
    if playbook_names is None:
        playbook_names, _complete = known_playbook_names()
    _scan_persona_messages(
        conn, result, cols,
        rescan_from_version=rescan_from_version, playbook_names=playbook_names,
        cutover=cutover, source_rows=source_rows,
    )
    _scan_persona_other_columns(
        conn, result, cols, count_memorize_candidates=rescan_from_version is None,
        cutover=cutover,
    )


def _scan_persona_messages(
    conn: sqlite3.Connection,
    result: StoreResult,
    cols: set,
    *,
    rescan_from_version: Optional[int],
    playbook_names: AbstractSet[str],
    cutover: Optional[int] = None,
    source_rows: Optional[BuildingSourceRows] = None,
) -> None:
    """messages の行を判定して、変換と確認待ちの印を ``result`` に積む。

    走査し直し (``rescan_from_version``) と全体の走査の違いは一つだけ: 前の版の
    走査が確認待ちの印を付けた行は、その版より後に足した規則に当たるときだけ
    変換し、当たらなければ前の版の判断 (と前の版のレポート) のまま残す。確認待ちの
    印の無い行は、前の版の完了の後に増えた行 (取り込み・合流など) かもしれない
    ので、全体の走査と同じく全部の規則で判定し、変換できなければ確認待ちにして
    レポートに出す — 印の無いまま完了の印を立てると、確認の画面にも出ず二度と
    走査されない。

    ``cutover`` より後に作られた行は、規則に当たっても当たらなくても変換せず、
    確認待ちの印を :data:`AFTER_CUTOVER_REASON` で付ける (前の版が付けた理由は
    書き換える)。全体の走査・走査し直し・完了の印を見送った後の走査のどれでも同じ。
    """
    from sai_memory.memory.storage import (
        SYSTEM_NOTICE_KEY,
        SYSTEM_NOTICE_REVIEW_KEY,
        SYSTEM_NOTICE_REVIEWED_KEY,
    )

    rescan = rescan_from_version is not None
    meta_col = _col_or_null(cols, "metadata")
    created_col = _col_or_null(cols, "created_at")
    # 確認待ちの印も変換も metadata の列に書く。列の無い古いスキーマでは書けないので、
    # 印を付けるべき行があれば ``review_marks_deferred`` を立てる — 完了の印を見送り、
    # 通常の初期化 (init_db) が列を足した後の起動で走査し直して印を付ける
    # (走査し直しでも同じ)。
    can_mark = "metadata" in cols
    order = "created_at, rowid" if "created_at" in cols else "rowid"
    rows = conn.execute(
        f"SELECT id, role, content, {meta_col}, rowid, {created_col} FROM messages "
        "WHERE substr(content, 1, ?) = ? OR role = 'system' "
        f"ORDER BY {order}",
        (len(SYSTEM_OPEN), SYSTEM_OPEN),
    ).fetchall()
    for row in rows:
        row_id, role, content, raw_meta = row[:4]
        role_s = role.lower() if isinstance(role, str) else ""
        tagged = starts_with_system_tag(content)
        if not tagged and role_s != "system":
            continue
        metadata = _read_metadata(raw_meta)
        if metadata is None:
            # 辞書として読めない metadata (壊れた JSON・配列など)。変換も確認待ちの印も
            # metadata を書き換えるので、元の値を失わないよう触らずレポートに出す
            result.unreadable_metadata_count += 1
            if len(result.unreadable_metadata_samples) < UNKNOWN_SYSTEM_LIST_LIMIT:
                result.unreadable_metadata_samples.append(Skipped(
                    "messages", str(row_id), str(role), [], _head(content),
                ))
            continue
        if metadata.get(SYSTEM_NOTICE_REVIEWED_KEY):
            continue  # 所有者が確定した行 (確認待ちの行の確定の画面)
        if role_s == "system" and metadata.get(SYSTEM_NOTICE_KEY):
            # 通知として済んでいる。本文の先頭が ``<system>`` (引用の書き出しなど) でも、
            # 系統の印を持つ通知を確認待ちにはしない
            continue
        match = _classify_persona_row(
            role, content, metadata, playbook_names=playbook_names,
        ) if can_mark else None
        held_system_after_cutover = (
            role_s == "system" and match is not None
            and metadata.get(SYSTEM_NOTICE_REVIEW_KEY) == AFTER_CUTOVER_REASON
        )
        if (
            rescan and metadata.get(SYSTEM_NOTICE_REVIEW_KEY)
            and (match is None or match.version <= rescan_from_version)
            and not held_system_after_cutover
        ):
            # 前の版の走査が確認待ちに残した行 (前の版のレポートにある)。ただし、前の版が
            # 時刻の境だけを理由に残した role=system の行は、今の版では規則で判定する
            # (下の境の判定を参照)
            continue
        if role_s != "system" and _created_after_cutover(row[5], cutover):
            # 新しい形式に切り替わった後の行。旧形式の形をしていても機構の書いた
            # ものと言い切れない (ユーザーが真似た発言でありうる) ので、所有者が決める。
            # role=system の行は境の後でも規則で判定する。移行が role=system の行に
            # するのは系統の印を足すことだけで、名義も本文も書き換えない (本文を剥がす
            # 規則は role=user / assistant の行にだけ当たる)。記憶ブラウザで所有者が
            # 足した role=system の行に印が付くことはありうるが、名義と本文は所有者が
            # 書いたまま残るので受け入れる (2026-10-30 まはー裁定)。v0.3.27 の後も印の
            # 無いまま書いていた Stelis の開始の行などを、所有者に委ねずに片づけるため
            result.skipped.append(Skipped(
                "messages", str(row_id), str(role),
                sorted(_tags_of(metadata)), _head(content), reason=AFTER_CUTOVER_REASON,
            ))
            if can_mark:
                _mark_for_review(result, row_id, metadata, AFTER_CUTOVER_REASON, replace=True)
            else:
                result.review_marks_deferred = True
            continue
        reason = (
            _queue_conversion(conn, result, cols, row, metadata, match, source_rows=source_rows)
            if match is not None else None
        )
        if match is not None and reason is None:
            continue
        if tagged or reason is not None:
            # 規則に当たったのに変換しなかった印の無い role=system 行 (元の発言の行が
            # 画面からの送信 — SENT_FROM_SCREEN_REASON) も、理由つきでここに載せる
            result.skipped.append(Skipped(
                "messages", str(row_id), str(role),
                sorted(_tags_of(metadata)), _head(content), reason=reason or "",
            ))
            review_reason = reason or UNCLASSIFIED_REASON
        else:
            result.unknown_system_count += 1
            if len(result.unknown_system_samples) < UNKNOWN_SYSTEM_LIST_LIMIT:
                result.unknown_system_samples.append(Skipped(
                    "messages", str(row_id), str(role),
                    sorted(_tags_of(metadata)), _head(content),
                ))
            review_reason = UNKNOWN_SYSTEM_REASON
        if can_mark:
            # 規則に当たったのに変換できなかった行は、その理由で印を付け直す
            _mark_for_review(result, row_id, metadata, review_reason, replace=reason is not None)
        else:
            result.review_marks_deferred = True


def _reviewed_by_owner(raw_meta: Any) -> bool:
    """所有者が確認待ちの行を確定した記録 (``SYSTEM_NOTICE_REVIEWED_KEY``) を持つ行か。"""
    from sai_memory.memory.storage import SYSTEM_NOTICE_REVIEWED_KEY

    metadata = _read_metadata(raw_meta)
    return bool(metadata and metadata.get(SYSTEM_NOTICE_REVIEWED_KEY))


def _scan_persona_other_columns(
    conn: sqlite3.Connection, result: StoreResult, cols: set, *,
    count_memorize_candidates: bool = True,
    cutover: Optional[int] = None,
) -> None:
    order = "created_at, rowid" if "created_at" in cols else "rowid"
    meta_col = _col_or_null(cols, "metadata")
    created_col = _col_or_null(cols, "created_at")

    # 旧世界の MEMORIZE の結果 (building_move / document_create / document_search /
    # generate_image など) は role=user・タグ無しの本文で保存された世代がある。
    # 発話でないことの来歴を機械的に確定できないので変換はせず、候補を数えて
    # レポートに出す。所有者が確定した行は候補に数えない。
    memorize_rows = conn.execute(
        "SELECT id, content, metadata FROM messages "
        "WHERE role = 'user' AND metadata LIKE '%playbook:%' "
        f"ORDER BY {order}",
    ).fetchall() if "metadata" in cols and count_memorize_candidates else []
    for row_id, content, raw_meta in memorize_rows:
        if starts_with_system_tag(content) or _reviewed_by_owner(raw_meta):
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
        for row_id, text, raw_meta, created_at in conn.execute(
            f"SELECT id, paired_action_text, {meta_col}, {created_col} FROM messages "
            "WHERE substr(paired_action_text, 1, ?) = ?",
            (len(SYSTEM_OPEN), SYSTEM_OPEN),
        ).fetchall():
            if _reviewed_by_owner(raw_meta):
                continue  # 所有者が確定した行はどの列も書き換えない
            if _created_after_cutover(created_at, cutover):
                # 今の書き手はタグを付けずに書く (指示文の展開そのまま)。切り替わりの
                # 後にタグで始まる値は、指示文に入った外からの本文でありうる。この列には
                # 確認待ちの印を付ける場所が無い (行の印は応答の本文を実会話から外す)
                # ので、書き換えずにレポートへ出す
                result.skipped.append(Skipped(
                    "messages.paired_action_text", str(row_id), "", [], _head(text),
                    reason=AFTER_CUTOVER_REASON,
                ))
                continue
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
        for batch_id, batch_kind, text, consumed_at in conn.execute(
            f"SELECT id, {kind_expr}, rendered_text, {_col_or_null(batch_cols, 'consumed_at')} "
            "FROM perception_batches WHERE substr(rendered_text, 1, ?) = ?",
            (len(SYSTEM_OPEN), SYSTEM_OPEN),
        ).fetchall():
            if batch_kind != "realtime_and_recall":
                result.skipped.append(Skipped(
                    "perception_batches", str(batch_id), str(batch_kind), [], _head(text),
                ))
                continue
            if _created_after_cutover(consumed_at, cutover):
                # 今の書き手 (compose_turn_realtime_and_recall_content) はタグを付けない
                result.skipped.append(Skipped(
                    "perception_batches", str(batch_id), str(batch_kind), [], _head(text),
                    reason=AFTER_CUTOVER_REASON,
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


def _persona_done_version(conn: sqlite3.Connection) -> Optional[int]:
    """完了の印の規則の版 (:func:`marker_rules_version`)。印が無ければ None。"""
    from sai_memory.memory.storage import get_embed_metadata

    return marker_rules_version(get_embed_metadata(conn, MIGRATION_MARKER_KEY))


def _set_persona_marker(conn: sqlite3.Connection) -> None:
    """取引の中で完了の印を書く (コミットは呼び出し側)。

    ``embed_metadata`` の無い古い記憶 DB では、表を同じ形で作る (sai_memory/
    memory/storage.py の init_db の ``CREATE TABLE IF NOT EXISTS embed_metadata``
    と同じ定義)。init_db は呼ばない — init_db は列の追加と既存行の埋め直しを
    自分でコミットするので、書き込みのロックの中では呼べず、ロックの外で呼ぶと
    写しが「触る前」の状態でなくなる。
    """
    from sai_memory.memory.storage import set_embed_metadata

    _ensure_embed_metadata(conn)
    set_embed_metadata(conn, MIGRATION_MARKER_KEY, _marker_value(), commit=False)


def _ensure_embed_metadata(conn: sqlite3.Connection) -> None:
    """``embed_metadata`` の無い古い記憶 DB に、init_db と同じ定義の表を作る (取引の中で)。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS embed_metadata ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )


def _write_persona_commit_record(conn: sqlite3.Connection, run_name: str) -> None:
    """記憶 DB の変換をコミットする取引の中で、その回の記録を ``embed_metadata`` に書く。

    鍵は :data:`COMMIT_RECORD_KEY_PREFIX` + 起動のフォルダ名 (起動ごとに一意)、値は
    書いた時刻。完了の印は版を上げるたびに書き換わるが、この鍵は回ごとに別なので
    書き換わらない。変換した回だけが書くので、鍵の数は変換を伴った起動の数を超えない。
    """
    from sai_memory.memory.storage import set_embed_metadata

    _ensure_embed_metadata(conn)
    set_embed_metadata(
        conn, COMMIT_RECORD_KEY_PREFIX + run_name, datetime.now(timezone.utc).isoformat(),
        commit=False,
    )


def _write_main_commit_record(conn: sqlite3.Connection, run_name: str) -> bool:
    """本体 DB の変換をコミットする取引の中で、その回の記録を ``execution_ledger`` に書く。

    実行台帳は「実行の記録」の既存の置き場で、KIND と IDEMPOTENCY_KEY の組が一意。
    KIND :data:`COMMIT_LEDGER_KIND`・IDEMPOTENCY_KEY が起動のフォルダ名・STATUS
    ``completed`` (終端) の行を一つ書く。台帳の読み手は KIND の前方一致と STATUS で
    絞るので、この行は回復処理の走査に入らない。表が無ければ書かずに False。
    """
    if not _table_exists(conn, "execution_ledger"):
        return False
    now = int(datetime.now(timezone.utc).timestamp())
    conn.execute(
        "INSERT INTO execution_ledger (EXECUTION_ID, KIND, IDEMPOTENCY_KEY, PERSONA_ID, STATUS, "
        "PAYLOAD_JSON, RESULT_JSON, ERROR, CREATED_AT, UPDATED_AT) "
        "VALUES (?, ?, ?, NULL, 'completed', NULL, NULL, NULL, ?, ?)",
        (str(uuid.uuid4()), COMMIT_LEDGER_KIND, run_name, now, now),
    )
    return True


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

    書き換えの取引を伴わない結果 (失敗・残した行だけの本体 DB) に使う。書き換えを
    コミットする結果は :func:`_stage_result_json` と :func:`_publish_staged_result`
    の二段で書く。レポートの書き出しが失敗しても、次の起動でこの JSON 群から
    レポートだけを作り直せる。
    """
    if results_dir is None:
        return
    _write_run_info(results_dir)
    _write_json_atomic(results_dir / f"{key}.json", result.to_json())
    result.result_saved = True


#: コミットの前に書く結果の JSON の仮の名前の末尾 (``<key>.json.tmp``)。
STAGED_SUFFIX = ".tmp"


def _stage_result_json(results_dir: Optional[Path], key: str, result: StoreResult) -> Optional[Path]:
    """書き換えのコミットの**前**に、結果の JSON を仮の名前 (``<key>.json.tmp``) で書く。

    書けなければ例外を送る — 呼び出し側はその DB の取引を巻き戻し、完了の印を
    立てない (次の起動で再試行)。コミットの後に書く順序だと、書き出しの失敗で
    「変換したのに目視の材料が無い」DB が残り、完了の印のために二度と走査されない。
    正式な名前 (``<key>.json``) にするのはコミットの後
    (:func:`_publish_staged_result`) — コミットの前に止まった起動の仮の JSON は、
    次の起動の :func:`regenerate_pending_reports` が DB の完了の印と突き合わせて
    捨てるので、確定していない変換がレポートの材料にならない。

    書いた中身とフォルダの項目は ``os.fsync`` で OS に確定させてから返す。確定
    させずにコミットすると、電源断で「印はコミット済み・仮の JSON は空か書きかけ」
    が残り、変換した行の目視の材料が失われる。確定に失敗したら仮の JSON を消して
    例外を送る (呼び出し側が巻き戻して印を立てない)。
    """
    if results_dir is None:
        return None
    _write_run_info(results_dir)
    staged = results_dir / f"{key}.json{STAGED_SUFFIX}"
    try:
        with open(staged, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(result.to_json(), ensure_ascii=False, indent=1))
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_dir(results_dir)
        # 起動ごとのフォルダは今回の実行で新しく作られる (初回は results/ と
        # backups/system_notice_migration/、環境によっては backups/ も新規)。
        # フォルダ自身を確定させても、その名前の項目は親に載っているので、
        # 新規作成されうる各階層の親を、既存が保証される祖先 (SAIVERSE_HOME)
        # まで確定させないと、電源断でフォルダ名ごと消えて、コミット済みの
        # 完了の印だけが残りうる (Codex 7〜8 巡目)。
        # 止まる位置は、この移行が実際に使っているホーム。環境変数のホーム
        # (get_saiverse_home) を見ると、CLI の --home で別の場所を移行している
        # とき指定先を越えて祖先へ fsync に行き、読めない親で移行ごと巻き戻る
        # (Codex 9 巡目)。results_dir の構成はこのモジュールが決めている
        # (<home>/backups/<OUTPUT_DIR_NAME>/<RESULTS_DIR_NAME>/<run_id>) ので、
        # ホームは相対で確定できる。
        home = results_dir.parents[3] if len(results_dir.parents) > 3 else results_dir.parent
        current = results_dir.parent
        for _ in range(8):  # 安全弁 (実際の深さは 3 段)
            _fsync_dir(current)
            if current == home or current.parent == current:
                break
            current = current.parent
    except BaseException:
        _discard_staged(staged)
        raise
    return staged


def _fsync_dir(directory: Path) -> None:
    """フォルダの項目 (作成・付け替え) を OS に確定させる。

    Windows では ``os.open`` がフォルダを開けない (PermissionError) ので、フォルダの
    確定はできず何もしない (ファイルの中身の確定だけになる)。それ以外の OS で
    失敗したら例外を送る。
    """
    if os.name == "nt":
        return
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard_staged(staged: Optional[Path]) -> None:
    """巻き戻した DB の仮の JSON を消す (消せなくても次の起動で印と突き合わせて捨てる)。"""
    if staged is None:
        return
    try:
        staged.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("[system-notice-migration] could not remove %s", staged, exc_info=True)


def _publish_staged_result(staged: Optional[Path], result: StoreResult) -> None:
    """コミットの後に、仮の JSON を正式な名前へ付け替える。

    付け替えられなければ ``result_save_failed`` を立てる (起動の入口が画面へ
    知らせる)。仮の JSON は残り、次の起動の :func:`regenerate_pending_reports` が
    DB の完了の印を確かめてから採用する。

    付け替えた後はフォルダの項目を OS に確定させる (:func:`_fsync_dir`)。確定に
    失敗しても警告だけにする — 付け替えは済んでいて、電源断で付け替えが失われ
    ても、残る仮の JSON は中身を確定させてあるので次の起動が印と突き合わせて
    採用する。
    """
    if staged is None:
        return
    try:
        os.replace(staged, staged.with_name(staged.name[: -len(STAGED_SUFFIX)]))
        result.result_saved = True
    except Exception:  # noqa: BLE001 — 移行は確定している。材料は次の起動で採用し直す
        result.result_save_failed = True
        LOGGER.error(
            "[system-notice-migration] could not finalize the result of %s; "
            "%s will be taken up on the next start",
            result.label, staged, exc_info=True,
        )
        return
    try:
        _fsync_dir(staged.parent)
    except OSError:
        LOGGER.warning(
            "[system-notice-migration] could not flush the folder entry of %s; "
            "if it is lost, the staged result is taken up on the next start",
            staged.parent, exc_info=True,
        )


def migrate_persona_db(
    db_path: Path,
    *,
    persona_id: str,
    output_dir: Path,
    dry_run: bool = False,
    stamp: str,
    results_dir: Optional[Path] = None,
    playbook_names: Optional[AbstractSet[str]] = None,
    hold_marker: Sequence[str] = (),
    cutover: Optional[int] = None,
    source_rows: Optional[BuildingSourceRows] = None,
) -> StoreResult:
    """一つのペルソナの記憶 DB を移行する。

    ``cutover`` は新しい形式に切り替わった時刻 (epoch 秒 — :func:`resolve_cutover`)。
    それより後に作られた行は変換しない (:func:`scan_persona_conn`)。起動の入口
    (:func:`run_migration`) は必ず渡す。

    ``source_rows`` は規則 5 の行の元の発言の行を本体 DB から引く手段
    (:class:`BuildingSourceRows`)。起動の入口は必ず渡す。渡さなければ規則 5 の行は
    確認待ちになる。

    ``hold_marker`` は、完了の印を立てる前提が欠けている理由 (``MARKER_HELD_*``)。
    一つでもあれば、変換と確認待ちの印だけをコミットして完了の印は書かない
    (今の印のまま — 次の起動で走査し直す)。``playbook_names`` を渡さなければ同梱の
    Playbook のファイルから読み、欠けなく読めなければ同じく完了の印を見送る。

    今の版 (:data:`MIGRATION_RULES_VERSION`) の印 (:data:`MIGRATION_MARKER_KEY`) が
    立っていれば何もしない。古い版の印なら、その後の版で足した規則だけで走査し
    直し (``rescan_from_version`` — 変換済みの行は条件から外れているので素通りする)、
    印を今の版に書き換える。印が無ければ全体を走査する。どちらも
    書き込みの取引 (``BEGIN IMMEDIATE`` — 他のプロセスの書き込みを止める) を
    取り、その中で印をもう一度確かめ (別のプロセスが先に終えていれば何も
    せずに終える)、変換する行を走査し、変換する行があれば写しを
    ``output_dir/<persona_id>/`` へ取ってから (写しが取れなければ書き換えない)
    書き換えと印をコミットする。写しを取ってから書き換えるまでの間に別の
    プロセスが行を書けないので、写しに無い行が移行されることはない。
    ``init_db`` は呼ばない (:func:`_set_persona_marker`) — 写しは DB に何も
    触る前の中身になる。

    確認待ちの印を付けるべき行があるのに metadata の列が無くて付けられない DB
    (``review_marks_deferred``) では、変換だけをコミットして完了の印を立てない。
    次の起動では通常の初期化が列を足しているので、走査し直して印を付ける
    (変換済みの行は変換の条件から外れているので二重に変換しない —
    :func:`strip_system_tags`)。

    ``results_dir`` を渡すと、見るべきものがあるとき結果を JSON に書く — コミットの
    前に仮の名前で書き (書けなければ巻き戻して印を立てない)、コミットの後に
    正式な名前へ付け替える (:func:`_stage_result_json`)。``dry_run`` は読み取り
    専用で開き、何も書かない。
    """
    from sai_memory.memory.storage import get_embed_metadata

    result = StoreResult(label=f"ペルソナ {persona_id}", path=str(db_path), persona=True)
    held = list(dict.fromkeys(hold_marker))
    if playbook_names is None:
        playbook_names, complete = known_playbook_names()
        if not complete and MARKER_HELD_PLAYBOOK_NAMES not in held:
            held.append(MARKER_HELD_PLAYBOOK_NAMES)
    with closing(_open_readonly(db_path)) as conn:
        done = _persona_done_version(conn)
        if done is not None and done >= MIGRATION_RULES_VERSION:
            result.already_done = True
            return result
        if dry_run:
            result.rescan_from_version = done
            scan_persona_conn(
                conn, result, rescan_from_version=done, playbook_names=playbook_names,
                cutover=cutover, source_rows=source_rows,
            )
            return result

    staged: Optional[Path] = None
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            done = _persona_done_version(conn)
            if done is not None and done >= MIGRATION_RULES_VERSION:
                # 同時に起動した別のプロセスが、こちらの読み取りの後に移行を
                # 終えた。何も書かずに終える。
                conn.rollback()
                result.already_done = True
                return result
            # 古い版の印がある DB は、その後の版で足した規則だけで走査し直す
            result.rescan_from_version = done
            scan_persona_conn(
                conn, result, rescan_from_version=done, playbook_names=playbook_names,
                cutover=cutover, source_rows=source_rows,
            )
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
            if result.updates and results_dir is not None:
                # この回の変換をコミットした記録 (仮の結果の JSON の採否の突き合わせに使う。
                # 完了の印を見送った回でも書く)
                _write_persona_commit_record(conn, results_dir.name)
                result.commit_record = results_dir.name
            if result.unreadable_metadata_count and MARKER_HELD_UNREADABLE_METADATA not in held:
                # 確認待ちの印を付けられない行が残る (元の metadata を失わずには付けられ
                # ない)。完了にすると二度と走査されず、実会話に混ざり続ける
                held.append(MARKER_HELD_UNREADABLE_METADATA)
            result.marker_held = held
            if result.review_marks_deferred:
                LOGGER.warning(
                    "[system-notice-migration] %s has no metadata column, so rows needing "
                    "review cannot be marked yet; the completion marker is left unset and "
                    "the rows will be marked on the next start",
                    db_path,
                )
            elif held:
                LOGGER.warning(
                    "[system-notice-migration] %s: the completion marker is left as it is "
                    "(%s); it will be scanned again on the next start",
                    db_path, ", ".join(held),
                )
            else:
                _set_persona_marker(conn)
                # 取引の中で書いた印の値 (孤児の仮の JSON の採否の突き合わせに使う)
                result.marker_value = get_embed_metadata(conn, MIGRATION_MARKER_KEY)
            if result.has_findings or result.backup_path:
                try:
                    staged = _stage_result_json(results_dir, f"persona_{persona_id}", result)
                except Exception as exc:  # noqa: BLE001 — 材料が無いまま変換を確定させない
                    LOGGER.error(
                        "[system-notice-migration] could not save the result of %s before "
                        "committing; skipping its migration", db_path, exc_info=True,
                    )
                    conn.rollback()
                    failed = StoreResult(
                        label=result.label, path=result.path, persona=True,
                        error=(
                            "移行の結果 (レポートの材料) を保存できなかったので、この DB は"
                            f"移行していません。次の起動で再試行します: {exc}"
                        ),
                    )
                    _try_write_result_json(results_dir, f"persona_{persona_id}", failed)
                    return failed
            conn.commit()
        except Exception:
            conn.rollback()
            _discard_staged(staged)
            raise
    _publish_staged_result(staged, result)
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


def _written_by_old_writer(legacy_seq: Any, legacy_message_id: Any, client_message_id: Any) -> bool:
    """建物履歴の行が、旧世代の発火文を書きえた書き手の行か (来歴の条件)。

    建物履歴には書き手が機構かユーザーかを持つ列が無い (旧世代の発火文も
    ユーザーの発言も role=user・persona_id 無しで載る)。代わりに、書いた経路の
    世代が列に残る:

    - ``legacy_seq`` / ``legacy_message_id``: 旧ログの取り込みと、ペルソナ側の
      番号を持ち込んでいた世代の書き込みだけが埋める。本番の写しでは正規の
      旧発火文 171 件の全部が持ち、2026-09-06 より後に書かれた行は一つも
      持たない。
    - ``client_message_id``: 画面からの送信が必ず付ける。正規の旧発火文は
      どれも持たない (本番の写しで画面から送られた 1039 件は全部持つ)。

    両方を満たす行だけを旧世代の判定に回す。今の画面からの送信は
    ``client_message_id`` を持つので外れ、今の書き込み経路の行は旧世代の列が空
    なので外れる。ただし旧世代の列は、書き込み (database/building_messages.py の
    ``insert_building_message``) が呼び出し側の渡した seq / message_id を写す
    作りなので、列の契約ではなく、本番の写しで確かめた分かれ方に立っている。
    """
    return (legacy_seq is not None or legacy_message_id is not None) and not client_message_id


def scan_main_conn(
    conn: sqlite3.Connection, result: StoreResult, *, cutover: Optional[int] = None,
) -> None:
    """本体 DB を読んで、変換を ``result`` に積む (書かない)。

    ``cutover`` (新しい形式に切り替わった時刻、epoch 秒) より後に作られた行は
    変換せず、理由 :data:`AFTER_CUTOVER_REASON` でレポートへ出す (本体 DB には確認
    待ちの印の置き場が無い)。作成時刻は ``building_messages.created_at`` (本体 DB
    に入った時刻) と、実行台帳・送信トレイの ``CREATED_AT``。ただし旧 log.json の
    取り込み (``seq`` が負 — saiverse/legacy_log_import.py) は、旧形式の過去の発言を
    後から入れたものなので、入った時刻に関わらず切り替わりの前の行として扱う。

    凍結された本文は「まだ送り直し・記録し直しがありうる行」だけを直す — 送り
    終えた行の凍結は崩さない (送信トレイの不変条件 6 の例外を最小にする):

    - building_messages: 音声通話の痕跡 (role=user + タグ + event_message +
      ``voice_call``) を、今の形 (role=host・タグ無し、saiverse/voice_call.py の
      ``_building_entry``) にする。ただし画面からの送信の印 ``client_message_id`` を
      持つ行は変換しない (:data:`SENT_FROM_SCREEN_REASON`)。印の無い旧世代のスケジュール発火文・外部
      イベント通知 (:func:`legacy_notice`。書き手の世代の列で来歴を縛る —
      :func:`_written_by_old_writer`) も同じ形にする。
    - execution_ledger: on_event の prepared 行の ``dispatch_envelope.user_input``
      (回収で応対の入力として送り直される) と、スルースの applied / completed
      行の結果の ``prompt`` (再適用で記録の paired_action_text になる)。
    - execution_outbox: 未配送 (pending / dead) の記憶への追記の ``message``。
    """
    from sai_memory.memory.storage import SYSTEM_NOTICE_KEY

    if _table_exists(conn, "building_messages"):
        bm_cols = _columns(conn, "building_messages")
        for (
            row_id, role, content, raw_meta, legacy_seq, legacy_mid, client_mid, seq, created_at,
        ) in conn.execute(
            "SELECT id, role, content, metadata_json, "
            f"{_col_or_null(bm_cols, 'legacy_seq')}, "
            f"{_col_or_null(bm_cols, 'legacy_message_id')}, "
            f"{_col_or_null(bm_cols, 'client_message_id')}, "
            f"{_col_or_null(bm_cols, 'seq')}, "
            f"{_col_or_null(bm_cols, 'created_at')} FROM building_messages "
            "WHERE substr(content, 1, ?) = ? ORDER BY id",
            (len(SYSTEM_OPEN), SYSTEM_OPEN),
        ).fetchall():
            metadata = _load_json(raw_meta)
            tags = _tags_of(metadata)
            imported_log = isinstance(seq, int) and seq < 0
            if not imported_log and _created_after_cutover(created_at, cutover):
                result.skipped.append(Skipped(
                    "building_messages", str(row_id), str(role), sorted(tags), _head(content),
                    reason=AFTER_CUTOVER_REASON,
                ))
                continue
            is_voice_call = (
                role == "user"
                and "event_message" in tags
                and metadata.get("voice_call") is True
            )
            if is_voice_call and client_mid:
                # 画面からの送信の印を持つ行は、tags と metadata を送り手が真似て書けるので、
                # 通話の痕跡と言い切れない (記憶 DB の SENT_FROM_SCREEN_REASON と同じ扱い)
                result.skipped.append(Skipped(
                    "building_messages", str(row_id), str(role), sorted(tags), _head(content),
                    reason=SENT_FROM_SCREEN_REASON,
                ))
                continue
            body = strip_system_tags(content) if is_voice_call else None
            label = "voice_call"
            if not is_voice_call and _written_by_old_writer(legacy_seq, legacy_mid, client_mid):
                # 印の無い旧世代のスケジュール発火文・外部イベント通知 (記憶 DB と同じ
                # 個別条件)。今の建物の出来事の行と同じ role=host・タグ無しにする。
                legacy = legacy_notice(role, content, metadata)
                if legacy is not None:
                    label, body = legacy
            if body is not None and is_voice_call and _speaker_has_attachments(role, metadata):
                # 通話の文字起こしの行の添付は発話者のもので、行ごと role=host にすると
                # 機構の行の持ち物になる (記憶 DB と同じ扱い)。旧世代の書き手の行
                # (画面からの送信の印を持たない) の添付は機構が付けたもの
                result.skipped.append(Skipped(
                    "building_messages", str(row_id), str(role), sorted(tags), _head(content),
                    reason=SPEAKER_ATTACHMENT_REASON,
                ))
                continue
            if body is not None:
                result.updates.append((
                    "UPDATE building_messages SET role = 'host', content = ? WHERE id = ?",
                    (body, row_id),
                ))
                result.changes.append(Change(
                    "building_messages", str(row_id), label, content, body,
                ))
                continue
            result.skipped.append(Skipped(
                "building_messages", str(row_id), str(role), sorted(tags), _head(content),
                reason=AMBIGUOUS_TAGS_REASON if is_voice_call else "",
            ))

    if _table_exists(conn, "execution_ledger"):
        ledger_created = _col_or_null(_columns(conn, "execution_ledger"), "CREATED_AT")
        for exec_id, raw_payload, created_at in conn.execute(
            f"SELECT EXECUTION_ID, PAYLOAD_JSON, {ledger_created} FROM execution_ledger "
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
            if _created_after_cutover(created_at, cutover):
                result.skipped.append(Skipped(
                    "execution_ledger(on_event)", str(exec_id), "", [], _head(text),
                    reason=AFTER_CUTOVER_REASON,
                ))
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
        for exec_id, raw_result, created_at in conn.execute(
            f"SELECT EXECUTION_ID, RESULT_JSON, {ledger_created} FROM execution_ledger "
            "WHERE KIND = ? AND STATUS IN ('applied', 'completed')",
            (_SLUICE_LEDGER_KIND,),
        ).fetchall():
            recorded = _load_json(raw_result)
            text = recorded.get("prompt")
            if not starts_with_system_tag(text):
                continue
            if _created_after_cutover(created_at, cutover):
                result.skipped.append(Skipped(
                    "execution_ledger(sluice)", str(exec_id), "", [], _head(text),
                    reason=AFTER_CUTOVER_REASON,
                ))
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
        outbox_created = _col_or_null(_columns(conn, "execution_outbox"), "CREATED_AT")
        for outbox_id, raw_payload, created_at in conn.execute(
            f"SELECT OUTBOX_ID, PAYLOAD_JSON, {outbox_created} FROM execution_outbox "
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
            if _created_after_cutover(created_at, cutover):
                result.skipped.append(Skipped(
                    "execution_outbox", str(outbox_id), str(message.get("role")),
                    sorted(_tags_of(metadata)), _head(content), reason=AFTER_CUTOVER_REASON,
                ))
                continue
            kind = classify_tagged_message(message.get("role"), content, metadata)
            body = strip_system_tags(content) if kind is not None else None
            if kind is not None and _speaker_has_attachments(message.get("role"), metadata):
                result.skipped.append(Skipped(
                    "execution_outbox", str(outbox_id), str(message.get("role")),
                    sorted(_tags_of(metadata)), _head(content), reason=SPEAKER_ATTACHMENT_REASON,
                ))
                continue
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
    cutover: Optional[int] = None,
) -> StoreResult:
    """本体 DB を移行する。

    ``cutover`` は新しい形式に切り替わった時刻 (epoch 秒 — :func:`resolve_cutover`)。
    それより後に作られた行は変換しない (:func:`scan_main_conn`)。起動の入口
    (:func:`run_migration`) は必ず渡す。

    印は持たない — 変換の条件 (タグで始まる・未配送・prepared) を満たす行は
    変換すると条件から外れるので、毎起動で呼んでも二度目以降は何も書かない。
    書き換える行があるときは、書き込みの取引 (``BEGIN IMMEDIATE``) の中で
    走査し直し、書き換える前に写しを ``output_dir`` に取る (起動時の
    pre_upgrade バックアップは数が上限を超えると消えるので別に取る。写しと
    書き換えの間に別のプロセスが書けない — :func:`migrate_persona_db` と同じ順序)。

    ``results_dir`` を渡すと、変換があるときと、残した行が前回
    (``last_leftovers`` — :meth:`StoreResult.leftover_keys`) と違うときに結果を
    JSON で書く — 本体 DB は毎起動で走査され、残した行は毎回同じものが見える
    ので、同じ行のままならレポートの材料にしない。書き換えを伴う結果は
    :func:`migrate_persona_db` と同じく、コミットの前に仮の名前で書き (書けなければ
    巻き戻す)、コミットの後に正式な名前へ付け替える。本体 DB は完了の印を持た
    ないので、変換をコミットする取引の中で、その回の記録を実行台帳に書く
    (:func:`_write_main_commit_record`)。付け替えの前に止まった起動の仮の JSON は、
    次の起動がこの記録と突き合わせて採否を決める (:func:`_settle_staged_results`)。
    本体 DB に ``execution_ledger`` が無ければこの記録を書けないので、変換をコミット
    しない (:data:`MAIN_DB_NO_COMMIT_RECORD_ERROR` を ``error`` に載せて返し、画面の
    警告は :func:`startup_alerts` が別に出す)。
    """
    result = StoreResult(label="本体 DB", path=str(db_path))
    if not db_path.exists():
        return result
    with closing(_open_readonly(db_path)) as conn:
        scan_main_conn(conn, result, cutover=cutover)
    if dry_run:
        return result
    if not result.updates:
        if result.has_leftovers and result.leftover_keys() != last_leftovers:
            _try_write_result_json(results_dir, "main", result)
        return result

    applied = StoreResult(label=result.label, path=result.path)
    staged: Optional[Path] = None
    report_it = False
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            scan_main_conn(conn, applied, cutover=cutover)
            if (
                applied.updates and results_dir is not None
                and not _table_exists(conn, "execution_ledger")
            ):
                # その回の変換の記録を書けない。コミットした後、仮の結果の JSON を正式な
                # 名前へ付け替える前に止まると、次の起動はコミットされたかを確かめられ
                # ず、変換した全行の記録を失う。だから変換をコミットしない。
                conn.rollback()
                LOGGER.error(
                    "[system-notice-migration] %s has no execution_ledger table, so the record "
                    "of this conversion cannot be written; skipping its migration", db_path,
                )
                failed = StoreResult(
                    label=result.label, path=result.path,
                    error=MAIN_DB_NO_COMMIT_RECORD_ERROR,
                    commit_record_unavailable=True,
                )
                _try_write_result_json(results_dir, "main", failed)
                return failed
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
            if (
                applied.updates and results_dir is not None
                and _write_main_commit_record(conn, results_dir.name)
            ):
                # この回の変換をコミットした記録 (本体 DB は完了の印を持たないので、
                # 仮の結果の JSON の採否はこの記録だけで決まる)
                applied.commit_record = results_dir.name
            report_it = bool(applied.changes) or (
                applied.has_leftovers and applied.leftover_keys() != last_leftovers
            )
            if report_it and applied.updates:
                # 書き換える結果はコミットの前に仮の名前で書く (ペルソナと同じ。
                # 書けなければ巻き戻し、次の起動で再試行する)
                try:
                    staged = _stage_result_json(results_dir, "main", applied)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.error(
                        "[system-notice-migration] could not save the result of %s before "
                        "committing; skipping its migration", db_path, exc_info=True,
                    )
                    conn.rollback()
                    failed = StoreResult(
                        label=result.label, path=result.path,
                        error=(
                            "移行の結果 (レポートの材料) を保存できなかったので、本体 DB は"
                            f"移行していません。次の起動で再試行します: {exc}"
                        ),
                    )
                    _try_write_result_json(results_dir, "main", failed)
                    return failed
            conn.commit()
        except Exception:
            conn.rollback()
            _discard_staged(staged)
            raise
    if staged is not None:
        _publish_staged_result(staged, applied)
    elif report_it:
        # 書き換えが無かった (同時に起動した別のプロセスが先に終えた) — 残した行の
        # 結果だけなので、コミットとの順序は無い
        _try_write_result_json(results_dir, "main", applied)
    return applied


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------


def changes_path_for(report_path: Path) -> Path:
    """レポートの隣に書く、変換した全行のファイルの場所 (``changes_<レポートの名前>``)。

    名前は ``report_`` で始めない — レポートを ``report_*.md`` で探す読み手に混ざらない。
    """
    return report_path.with_name(f"changes_{report_path.name}")


def _full_text(text: Any) -> str:
    """全件のファイルに書く本文 (切り詰めない。改行は ``\\n`` と書いて一行に収める)。"""
    return str(text or "").replace("\r", "").replace("\n", "\\n")


def render_changes(
    results: Sequence[StoreResult], *, dry_run: bool, stamp: str,
) -> str:
    """変換した全行 (表・系統・行 id・変更前後の全文) の一覧。レポートの本文はサンプルだけ。"""
    lines: List[str] = [
        "# システム通知の移行で変換した全行",
        "",
        f"- 実行: {stamp}" + (" (dry-run: 何も書き換えていない)" if dry_run else ""),
        "- 本文は切り詰めず、改行は \\n と書いて一行に収めた",
        "",
    ]
    for res in results:
        if not res.changes:
            continue
        lines.append(f"## {res.label}")
        lines.append(f"- DB: {res.path}")
        lines.append(f"- 変換した行: {sum(res.counts().values())} 件")
        if not res.changes_complete:
            lines.append(
                "- この DB の結果は全行を保存する前の版で書かれたので、ここには系統ごとの"
                "サンプルだけが載っている"
            )
        lines.append("")
        for c in res.changes:
            lines.append(f"- [{c.table} / {c.kind}] id={c.row_id}")
            lines.append(f"  - 前: {_full_text(c.before)}")
            lines.append(f"  - 後: {_full_text(c.after)}")
        lines.append("")
    return "\n".join(lines)


def _write_report_files(
    report_path: Path, results: Sequence[StoreResult], *, dry_run: bool, stamp: str,
    cutover: Optional["Cutover"], unverified: Sequence[Path] = (),
) -> None:
    """レポートと、その隣の変換した全行のファイル (変換が一件でもあれば) を書く。"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    changes_path: Optional[Path] = None
    if any(r.changes for r in results):
        changes_path = changes_path_for(report_path)
        changes_path.write_text(
            render_changes(results, dry_run=dry_run, stamp=stamp), encoding="utf-8",
        )
    report_path.write_text(
        render_report(
            results, dry_run=dry_run, stamp=stamp, cutover=cutover, changes_path=changes_path,
            unverified=unverified,
        ),
        encoding="utf-8",
    )


def render_report(
    results: Sequence[StoreResult], *, dry_run: bool, stamp: str,
    cutover: Optional["Cutover"] = None,
    changes_path: Optional[Path] = None,
    unverified: Sequence[Path] = (),
) -> str:
    from sai_memory.memory.storage import SYSTEM_NOTICE_REVIEW_KEY

    lines: List[str] = [
        "# システム通知の移行レポート",
        "",
        f"- 実行: {stamp}" + (" (dry-run: 何も書き換えていない)" if dry_run else ""),
        "- 設計: docs/intent/system_notice_delivery.md「移行」",
    ]
    if changes_path is not None:
        lines.append(
            f"- 変換した全行 (変更前後の全文): {changes_path}。このレポートの本文には"
            f"系統ごとに最大 {SAMPLES_PER_KIND} 件のサンプルだけを載せる"
        )
    if cutover is not None:
        lines.append(
            f"- 新しい形式に切り替わった時刻: {cutover.at.isoformat()} "
            f"({_CUTOVER_SOURCE_REPORT_LINES.get(cutover.source, cutover.source)})。"
            "これより後に作られた行は、規則に当たっても自動では変換せず、理由"
            f"「{AFTER_CUTOVER_REASON}」でこのレポートに出す (記憶の messages の行には"
            "確認待ちの印を付ける)"
        )
    if unverified:
        lines.append(
            "- コミットされたかを確かめられなかった仮の結果の JSON: "
            + ", ".join(str(p) for p in unverified)
            + " (照合に使う欄 commit_record・marker_value か DB のパスを持たないので、削除せず"
            f"名前の末尾に {UNVERIFIED_SUFFIX} を付けて残した。中の変換はこのレポートの件数と"
            "全行のファイルに含めていない)"
        )
    lines.append("")
    for res in results:
        if not res.has_findings and not res.backup_path:
            continue
        lines.append(f"## {res.label}")
        lines.append(f"- DB: {res.path}")
        if res.backup_path:
            lines.append(f"- 移行前の写し: {res.backup_path}")
        if res.error:
            lines.append(f"- エラー: {res.error}")
        if res.rescan_from_version is not None:
            lines.append(
                f"- 走査し直し: 規則の版 {res.rescan_from_version} で移行を済ませた記憶なので、"
                f"版 {res.rescan_from_version + 1}〜{MIGRATION_RULES_VERSION} で足した規則"
                "だけを当て直した。前の版で変換しなかった行は、前の版のレポートと"
                "確認待ちの印にある (確認待ちの印の無かった行は、全部の規則で判定し直して"
                "このレポートに出す)"
            )
        if res.review_marks_deferred:
            lines.append(
                "- 確認待ちの印: この記憶 DB は metadata の列が無い古い形なので、確認が要る行に"
                "印を付けられなかった。移行の完了の印を立てずにおき、次の起動 (通常の初期化が"
                "列を足した後) で走査し直して印を付け直す"
            )
        for held in res.marker_held:
            lines.append(
                "- 完了の印を見送った: "
                + _MARKER_HELD_REPORT_LINES.get(held, held)
                + "。変換と確認待ちの印だけをコミットし、次の起動で走査し直す"
            )
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
                f"### 変換しなかった行 (全 {len(res.skipped)} 件。<system> 始まりの行と、"
                "新しい形式に切り替わった後に作られた行)"
            )
            if (
                res.persona and not res.review_marks_deferred
                and any(s.table == "messages" for s in res.skipped)
            ):
                lines.append(
                    f"(messages の行には確認待ちの印 `{SYSTEM_NOTICE_REVIEW_KEY}` を metadata に付け、"
                    "会話の手本・想起・あらすじの発話の判定から外した。本文と role は"
                    "そのまま。行を直したらこの印を消す)"
                )
            for s in res.skipped:
                reason = f" 理由={s.reason}" if s.reason else ""
                lines.append(
                    f"- [{s.table}] id={s.row_id} role={s.role} tags={s.tags}{reason}: {s.head}"
                )
        if res.unknown_system_count:
            lines.append("")
            marked = (
                "確認待ちの印は次の起動で付ける" if res.review_marks_deferred
                else f"確認待ちの印 `{SYSTEM_NOTICE_REVIEW_KEY}` を付けた"
            )
            lines.append(
                f"### 系統を推定できなかった role=system の行 (全 {res.unknown_system_count} 件、"
                f"先頭 {len(res.unknown_system_samples)} 件を表示。通知の印は付けず、{marked})"
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
        if res.unreadable_metadata_count:
            lines.append("")
            lines.append(
                "### metadata が辞書として読めない行 (壊れた JSON・配列など。全 "
                f"{res.unreadable_metadata_count} 件、先頭 {len(res.unreadable_metadata_samples)} "
                "件を表示。元の metadata を失わないよう、変換も確認待ちの印もしていない。"
                "確認の画面には出ないので、ここで確かめる)"
            )
            for s in res.unreadable_metadata_samples:
                lines.append(f"- id={s.row_id} role={s.role}: {s.head}")
        lines.append("")
    return "\n".join(lines)


#: レポートに書く、完了の印を見送った理由の説明。
_MARKER_HELD_REPORT_LINES = {
    MARKER_HELD_PLAYBOOK_NAMES: "規則 3 の材料 (実在する Playbook 名) を完全に読めなかった",
    MARKER_HELD_MAIN_DB_FAILED: "本体 DB の移行が失敗した (記憶へ書かれる前の旧形式の本文が残りうる)",
    MARKER_HELD_STAGED_RESULT: "前の起動の仮の結果の JSON の採否がまだ決まっていない",
    MARKER_HELD_UNREADABLE_METADATA: (
        "metadata が辞書として読めない行が残っていて、確認待ちの印を付けられない "
        "(下の「metadata が辞書として読めない行」の節。行が直るまで毎起動で走査し直す)"
    ),
}


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


# ---------------------------------------------------------------------------
# 新しい形式に切り替わった時刻
# ---------------------------------------------------------------------------

#: 切り替わりの時刻の決め方 (記録の ``source``)。
#: 記憶 DB のどれかに既にあった完了の印のうち、最も早い完了時刻 (この移行を
#: 含む版 — v0.3.27 以降 — を最初に動かした起動の、最初の移行の時刻)。
CUTOVER_FROM_MARKER = "earliest_completion_marker"
#: この移行が前の起動で書いた記録 (:func:`_earliest_migration_trace`) のうち最も早い
#: 時刻。完了の印より早かったか、印がどこにも無かったとき。
CUTOVER_FROM_TRACE = "earliest_migration_trace"
#: 完了の印も、移行が前に書いた記録もどこにも無かったので、記録した起動の、移行を
#: 始めた時刻。
CUTOVER_FROM_START = "first_start"
#: 前の起動が境を決めようとして ``cutover.json`` に記録できなかった時刻 (本体 DB の
#: ``execution_ledger`` の :data:`CUTOVER_ATTEMPT_LEDGER_KIND` の行 —
#: :func:`_leave_cutover_attempt`)。完了の印や移行が前に書いた記録より早かったとき。
CUTOVER_FROM_ATTEMPT = "unrecorded_attempt"

#: ``cutover.json`` を書けなかった起動が、その起動で決めようとした境の時刻を残す本体 DB の
#: ``execution_ledger`` の行の KIND と IDEMPOTENCY_KEY (固定で一行だけ。既にあれば、より早い
#: 時刻のときだけ書き換える — :func:`_write_cutover_attempt`)。STATUS は ``completed`` (終端)。台帳の読み手
#: (saiverse/execution_ledger.py・saiverse/building_id_repair.py) は STATUS (と KIND の
#: 前方一致) で絞るので、この行は回復の処理の走査に入らない (:data:`COMMIT_LEDGER_KIND`
#: の行と同じ)。
CUTOVER_ATTEMPT_LEDGER_KIND = "system_notice_migration.cutover_attempt"
CUTOVER_ATTEMPT_LEDGER_KEY = "first"

_CUTOVER_SOURCE_REPORT_LINES = {
    CUTOVER_FROM_MARKER: "記憶の完了の印のうち最も早いもの — この移行を含む版を最初に動かした起動の時刻",
    CUTOVER_FROM_TRACE: (
        "この移行が前の起動で書いた記録 (起動ごとの結果のフォルダ・移行前の写し・"
        "last_leftovers.json) のうち最も早いもの"
    ),
    CUTOVER_FROM_START: "完了の印も、移行が前に書いた記録も無かったので、記録した起動の時刻",
    CUTOVER_FROM_ATTEMPT: (
        "前の起動が境を決めようとして cutover.json に記録できなかった時刻 (本体 DB の "
        f"execution_ledger の KIND {CUTOVER_ATTEMPT_LEDGER_KIND} の行に残したもの)"
    ),
}


@dataclass(frozen=True)
class Cutover:
    """この環境で新しい形式に切り替わった時刻 (:func:`resolve_cutover`)。"""

    #: UTC の aware な時刻。
    at: datetime
    #: 決め方 (:data:`CUTOVER_FROM_MARKER` / :data:`CUTOVER_FROM_TRACE` /
    #: :data:`CUTOVER_FROM_START` / :data:`CUTOVER_FROM_ATTEMPT`)。
    source: str

    @property
    def epoch(self) -> int:
        """秒に切り捨てた epoch 秒 (行の作成時刻と比べる値 — :func:`_created_after_cutover`)。"""
        return math.floor(self.at.timestamp())


def _cutover_path(saiverse_home: Path) -> Path:
    return saiverse_home / "backups" / OUTPUT_DIR_NAME / CUTOVER_FILE_NAME


def _read_cutover(path: Path) -> Optional[Cutover]:
    """記録した切り替わりの時刻。記録が無ければ None。読めなければ :class:`CutoverUnavailable`。

    読めない記録 (壊れた JSON・時刻の無い記録・I/O の失敗) を「無い」と扱って決め
    直すと、記録した時刻が変わりうる (完了の印は版を上げるたびに新しい時刻で
    書き換わる) ので、決め直さない。
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CutoverUnavailable(f"切り替わりの時刻の記録 {path} を読めませんでした: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
        at = _parse_utc(data["cutover"])
        source = str(data.get("source") or "")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CutoverUnavailable(f"切り替わりの時刻の記録 {path} が壊れています: {exc}") from exc
    if at is None:
        raise CutoverUnavailable(f"切り替わりの時刻の記録 {path} の時刻を読めません")
    return Cutover(at, source)


def _marker_completed_at(value: str) -> Optional[datetime]:
    """完了の印の値 (``v<版> <完了時刻>``、版 1 は完了時刻だけ) の完了時刻。読めなければ None。"""
    head, _sep, rest = str(value).strip().partition(" ")
    if head.startswith("v") and head[1:].isdigit():
        return _parse_utc(rest)
    return _parse_utc(value)


def _earliest_completion_marker(
    saiverse_home: Path, found: Optional[List[datetime]] = None,
) -> Optional[datetime]:
    """記憶 DB の完了の印の完了時刻のうち最も早いもの。印がどこにも無ければ None。

    読めない記憶 DB・読めない印が一つでもあれば :class:`CutoverUnavailable` —
    読み落とした印が最も早いものだと、切り替わりの時刻が遅く決まり、その間に
    新しい形式のコードが書いた行が自動の変換の対象に入る。

    ``found`` を渡せば、読めた完了時刻を読んだ順に足す (途中で
    :class:`CutoverUnavailable` を送るときも、それまでに読めた時刻は残る —
    :func:`resolve_cutover` が本体 DB に残す時刻に使う)。
    """
    from sai_memory.memory.storage import get_embed_metadata_strict

    earliest: Optional[datetime] = None
    try:
        dbs = list(_iter_persona_dbs(saiverse_home))
    except OSError as exc:
        raise CutoverUnavailable(f"記憶の一覧を読めませんでした: {exc}") from exc
    for persona_id, db in dbs:
        try:
            with closing(_open_readonly(db)) as conn:
                value = get_embed_metadata_strict(conn, MIGRATION_MARKER_KEY)
        except sqlite3.Error as exc:
            raise CutoverUnavailable(
                f"記憶 {persona_id} の完了の印を読めませんでした: {exc}"
            ) from exc
        if value is None:
            continue
        at = _marker_completed_at(value)
        if at is None:
            raise CutoverUnavailable(
                f"記憶 {persona_id} の完了の印の時刻を読めませんでした: {value!r}"
            )
        if found is not None:
            found.append(at)
        if earliest is None or at < earliest:
            earliest = at
    return earliest


def _file_written_at(path: Path) -> datetime:
    """ファイルを最後に書いた時刻 (更新時刻、UTC)。読めなければ :class:`CutoverUnavailable`。"""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except (OSError, OverflowError, ValueError) as exc:
        raise CutoverUnavailable(f"移行の記録 {path} の時刻を読めませんでした: {exc}") from exc


#: 起動ごとの結果のフォルダの名前の先頭 (``<時刻>_<pid>_<乱数>`` の ``<時刻>``。
#: その起動の地方時を秒まで — :func:`run_migration` の ``stamp``)。
_RUN_FOLDER_STAMP = re.compile(r"^(\d{8}_\d{6})(?:_|$)")
#: 地方時の UTC からのずれの最大 (UTC+14)。タイムゾーンを持たない地方時の時刻から、
#: ありうる最も早い UTC を出すのに使う (:func:`_legacy_run_folder_started_at`)。
_MAX_UTC_OFFSET = timedelta(hours=14)


def _legacy_run_folder_started_at(run_dir: Path, results: Sequence[Path]) -> datetime:
    """``RUN`` の無い結果のフォルダ (``RUN`` を書く前の版の形式) の起動の時刻。

    ``results/<起動>/`` は 938ec0f8 から書かれ、``RUN`` は a56380fd から書かれる。
    その間の版は ``RUN`` を書かずに結果の JSON だけを書いた — それは正当な保存形式で、
    壊れた記録ではない。

    - フォルダ名の先頭の時刻 (その起動の地方時、秒まで) を読めれば、その時刻を UTC と
      見なしたうえで :data:`_MAX_UTC_OFFSET` (14 時間) 引いた時刻。名前の時刻は
      タイムゾーンを持たないので、今の計算機のタイムゾーンで読み直すと、起動の後に
      タイムゾーンを変えた環境で境が遅れる (遅れた分の改修後の行が自動で変換される)。
      地方時の UTC からのずれは最大 +14 時間なので、引いた時刻はありうる最も早い UTC
      で、実際の起動より遅いことはない。
    - 読めなければ、フォルダの中の結果の JSON の更新時刻のうち最も早いもの (その起動
      より後に書いた時刻なので、起動より早いことはない)。
    - どちらも取れなければ :class:`CutoverUnavailable`。
    """
    match = _RUN_FOLDER_STAMP.match(run_dir.name)
    if match:
        try:
            named = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            return named - _MAX_UTC_OFFSET
        except (ValueError, OverflowError):
            pass  # 日付として成り立たない名前 — 更新時刻で決める
    times: List[datetime] = []
    for path in results:
        try:
            times.append(_file_written_at(path))
        except CutoverUnavailable:
            LOGGER.warning(
                "[system-notice-migration] could not read the time of %s", path, exc_info=True,
            )
    if not times:
        raise CutoverUnavailable(
            f"移行の記録 {run_dir} (RUN の無い前の版の形式) から起動の時刻を読めませんでした"
        )
    return min(times)


def _run_folder_started_at(run_dir: Path) -> Optional[datetime]:
    """起動ごとの結果のフォルダの ``RUN`` に書いた、その起動の開始時刻。

    ``RUN`` も結果の JSON も無いフォルダ (何も記録しないまま止まった起動) は痕跡では
    ないので None。``RUN`` が無く結果の JSON だけがあるフォルダは ``RUN`` を書く前の
    版の形式で、フォルダ名か JSON の更新時刻から時刻を決める
    (:func:`_legacy_run_folder_started_at`)。``RUN`` があるのに時刻を読めないときは
    :class:`CutoverUnavailable`。
    """
    run_info = run_dir / RUN_INFO_NAME
    try:
        exists = run_info.is_file()
        results = (
            [] if exists
            else sorted(run_dir.glob("*.json")) + sorted(run_dir.glob(f"*.json{STAGED_SUFFIX}"))
            # 付け替えは更新時刻を変えない (:func:`_settle_staged_results`)
            + _unverified_results(run_dir)
        )
    except OSError as exc:
        raise CutoverUnavailable(f"移行の記録 {run_dir} を読めませんでした: {exc}") from exc
    if not exists:
        return _legacy_run_folder_started_at(run_dir, results) if results else None
    try:
        info = json.loads(run_info.read_text(encoding="utf-8"))
        at = _parse_utc(info["started_at"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CutoverUnavailable(
            f"移行の記録 {run_info} から起動の時刻を読めませんでした: {exc}"
        ) from exc
    if at is None:
        raise CutoverUnavailable(f"移行の記録 {run_info} の起動の時刻を読めませんでした")
    return at


def _earliest_migration_trace(
    saiverse_home: Path, found: Optional[List[datetime]] = None,
) -> Optional[datetime]:
    """この移行が前の起動で書いた記録の時刻のうち、最も早いもの。記録が無ければ None。

    完了の印は記憶 DB ごとに版を上げるたびに書き換わり、記憶 DB を消せば消える
    (v0.3.27 の起動のときに記憶 DB が無かった環境では、どこにも残らない)。そこで
    移行が ``<SAIVERSE_HOME>/backups/system_notice_migration/`` に書く永続の記録も
    候補にする (どれも移行を書き換えありで走らせた起動だけが書く。dry-run は書かない):

    - ``results/<起動>/RUN`` の ``started_at`` (その起動の開始時刻)
    - 移行前の写し (``*.pre_migration_*.bak`` — 本体 DB の分は直下、記憶の分は
      ``<persona_id>/`` の下) の更新時刻 (写しを書いた時刻)
    - ``last_leftovers.json`` の更新時刻 (最後に書き換えた時刻)

    更新時刻はその記録を最後に書いた時刻なので、最初の起動より遅いことはあっても
    早いことはない。最も早いものを取れば、最初の起動の時刻に最も近い。レポート
    (``report_*.md``) は結果のフォルダから作るので数えない (dry-run のレポートも同じ
    置き場に書かれる)。``cutover.json`` 自身も数えない。

    記録はあるのに時刻を読めない・置き場を読めないときは :class:`CutoverUnavailable`
    — 読み落とした記録が最も早いものだと、境が遅く決まる。``found`` は
    :func:`_earliest_completion_marker` と同じ (読めた時刻を読んだ順に足す)。
    """
    output_dir = saiverse_home / "backups" / OUTPUT_DIR_NAME
    times: List[datetime] = []

    def _note(at: datetime) -> None:
        times.append(at)
        if found is not None:
            found.append(at)

    try:
        if not output_dir.is_dir():
            return None
        results_root = output_dir / RESULTS_DIR_NAME
        run_dirs = (
            [p for p in results_root.iterdir() if p.is_dir()] if results_root.is_dir() else []
        )
        copies = [
            p for p in output_dir.glob("*.pre_migration_*.bak") if p.is_file()
        ] + [
            p for p in output_dir.glob("*/*.pre_migration_*.bak") if p.is_file()
        ]
        leftovers = output_dir / LAST_LEFTOVERS_NAME
        has_leftovers = leftovers.is_file()
    except OSError as exc:
        raise CutoverUnavailable(f"移行の記録の置き場 {output_dir} を読めませんでした: {exc}") from exc
    for run_dir in run_dirs:
        at = _run_folder_started_at(run_dir)
        if at is not None:
            _note(at)
    for path in copies:
        _note(_file_written_at(path))
    if has_leftovers:
        _note(_file_written_at(leftovers))
    return min(times) if times else None


def _record_cutover(path: Path, cutover: Cutover, *, saiverse_home: Path) -> Cutover:
    """切り替わりの時刻を新しく記録し、記録にある時刻を返す。

    既に記録があれば書き換えない — 同時に起動した別のプロセスが先に記録していれば、
    その時刻を返す。中身を OS に確定させた仮のファイルを、上書きしない固いリンクで
    正式な名前に付ける (書きかけの記録が正式な名前で残らない)。固いリンクを作れない
    ファイルシステムでは、Windows でだけ、確定させた仮のファイルを名前の付け替え
    (:func:`_publish_without_link`) で公開する。Windows 以外では境を記録できない
    ものとして :class:`CutoverUnavailable` を送る。書けなければ
    :class:`CutoverUnavailable`。
    """
    data = {
        "cutover": cutover.at.isoformat(),
        "source": cutover.source,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    text = json.dumps(data, ensure_ascii=False, indent=1)

    def _write_new(target: Path) -> None:
        with open(target, "x", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{_unique_suffix()}.tmp")
        _write_new(tmp)
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass  # 別のプロセスが先に記録した
        except OSError:
            _publish_without_link(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        # 新しく作られうるフォルダ (backups/ と system_notice_migration/) の項目も確定させる
        for directory in (path.parent, path.parent.parent, saiverse_home):
            _fsync_dir(directory)
    except OSError as exc:
        raise CutoverUnavailable(f"切り替わりの時刻を {path} に記録できませんでした: {exc}") from exc
    recorded = _read_cutover(path)
    if recorded is None:
        raise CutoverUnavailable(f"切り替わりの時刻の記録 {path} が見つかりません")
    return recorded


def _publish_without_link(tmp: Path, path: Path) -> None:
    """固いリンクを作れないとき、中身を確定させた仮のファイル ``tmp`` を正式な名前 ``path`` で公開する。

    正式な名前に直接書くと、途中で止まったときに空か書きかけの記録が残り、次の起動
    からは壊れた記録として :class:`CutoverUnavailable` が続く。だから名前の付け替え
    (原子的) で、完成した中身だけを公開する。

    - Windows の ``os.rename`` は、付け替え先の名前が既にあると FileExistsError で
      失敗する。付け替えそのものが排他になるので、先に記録したプロセスの記録を
      上書きしない。
    - それ以外の OS の ``os.rename`` は既存の名前を上書きするので、同時に起動した別の
      プロセスが先に記録した境を後の時刻で上書きしうる。上書きしない公開の手段が
      無いので記録せず、:class:`CutoverUnavailable` を送る (その起動は何も変換せず、
      起動時の警告で知らせる — :func:`run_startup_system_notice_migration`)。
    """
    if os.name != "nt":
        raise CutoverUnavailable(
            f"固いリンクを作れないファイルシステムなので、切り替わりの時刻を {path} に"
            "上書きせずに記録できません"
        )
    try:
        os.rename(tmp, path)
    except FileExistsError:
        pass  # 別のプロセスが先に記録した


def _write_cutover_attempt(conn: sqlite3.Connection, cutover: Cutover) -> bool:
    """境の時刻 ``cutover`` を本体 DB の ``execution_ledger`` に一行だけ残す (コミットは呼び出し側)。

    KIND :data:`CUTOVER_ATTEMPT_LEDGER_KIND`・IDEMPOTENCY_KEY :data:`CUTOVER_ATTEMPT_LEDGER_KEY`・
    STATUS ``completed``・PAYLOAD_JSON ``{"cutover": <ISO 8601>, "source": <決め方>}``。
    既にその行があれば、``cutover`` がその行の時刻より早いときだけ PAYLOAD_JSON と
    UPDATED_AT を書き換える (前の起動が最古の候補を読む前に止まり、後の起動が読んだ後に
    止まると、後の起動の時刻のほうが早い)。その行の時刻を読めなければ書き換えない
    (より早い時刻かもしれない。読む側 :func:`_cutover_attempt_in_main_db` が
    :class:`CutoverUnavailable` で止め続ける)。表が無ければ書かずに False。
    """
    if not _table_exists(conn, "execution_ledger"):
        return False
    now = int(datetime.now(timezone.utc).timestamp())
    payload = json.dumps(
        {"cutover": cutover.at.isoformat(), "source": cutover.source}, ensure_ascii=False,
    )
    existing = conn.execute(
        "SELECT PAYLOAD_JSON FROM execution_ledger WHERE KIND = ? AND IDEMPOTENCY_KEY = ?",
        (CUTOVER_ATTEMPT_LEDGER_KIND, CUTOVER_ATTEMPT_LEDGER_KEY),
    ).fetchone()
    if existing is not None:
        try:
            left_at = _parse_utc(json.loads(existing[0])["cutover"])
        except (ValueError, KeyError, TypeError):
            left_at = None
        if left_at is not None and cutover.at < left_at:
            conn.execute(
                "UPDATE execution_ledger SET PAYLOAD_JSON = ?, UPDATED_AT = ? "
                "WHERE KIND = ? AND IDEMPOTENCY_KEY = ?",
                (payload, now, CUTOVER_ATTEMPT_LEDGER_KIND, CUTOVER_ATTEMPT_LEDGER_KEY),
            )
        return True
    conn.execute(
        "INSERT INTO execution_ledger (EXECUTION_ID, KIND, IDEMPOTENCY_KEY, PERSONA_ID, STATUS, "
        "PAYLOAD_JSON, RESULT_JSON, ERROR, CREATED_AT, UPDATED_AT) "
        "SELECT ?, ?, ?, NULL, 'completed', ?, NULL, NULL, ?, ? "
        "WHERE NOT EXISTS (SELECT 1 FROM execution_ledger WHERE KIND = ? AND IDEMPOTENCY_KEY = ?)",
        (
            str(uuid.uuid4()), CUTOVER_ATTEMPT_LEDGER_KIND, CUTOVER_ATTEMPT_LEDGER_KEY,
            payload, now, now, CUTOVER_ATTEMPT_LEDGER_KIND, CUTOVER_ATTEMPT_LEDGER_KEY,
        ),
    )
    return True


def _leave_cutover_attempt(main_db: Optional[Path], cutover: Cutover) -> None:
    """境を ``cutover.json`` に記録できなかった起動で、決めようとした境の時刻を本体 DB に残す。

    この起動は何も変換しないが、アプリは起動を続け、新しい形式の行を書く。次の起動が
    その起動の時刻で境を決め直すと、間に書かれた行が境より前の旧形式として変換されうる。
    だから本体 DB の ``execution_ledger`` に残し (:func:`_write_cutover_attempt`)、
    :func:`resolve_cutover` が境の候補に含める。本体 DB が無い・``execution_ledger`` が
    無い・書けないときは残さない (ログだけ。呼び出し側は元の :class:`CutoverUnavailable`
    を送る)。
    """
    if main_db is None or not main_db.is_file():
        return
    try:
        with closing(sqlite3.connect(str(main_db))) as conn:
            conn.execute("BEGIN IMMEDIATE")
            wrote = _write_cutover_attempt(conn, cutover)
            conn.commit()
    except sqlite3.Error:
        LOGGER.error(
            "[system-notice-migration] could not leave the cutover time %s in %s",
            cutover.at.isoformat(), main_db, exc_info=True,
        )
        return
    if wrote:
        LOGGER.warning(
            "[system-notice-migration] left the cutover time %s (%s) in execution_ledger of %s "
            "because it could not be recorded in cutover.json (an earlier time already left "
            "there is kept)",
            cutover.at.isoformat(), cutover.source, main_db,
        )
    else:
        LOGGER.warning(
            "[system-notice-migration] %s has no execution_ledger table; the cutover time %s "
            "was not left anywhere", main_db, cutover.at.isoformat(),
        )


def _cutover_attempt_in_main_db(main_db: Optional[Path]) -> Optional[datetime]:
    """本体 DB に残した、前の起動が決めようとした境の時刻 (:func:`_leave_cutover_attempt`)。

    本体 DB・``execution_ledger``・その行のどれかが無ければ None。本体 DB を読めない・
    行の時刻を読めないときは :class:`CutoverUnavailable` (読み落とした時刻が最も早い
    ものだと、境が遅く決まる)。
    """
    if main_db is None or not main_db.is_file():
        return None
    try:
        with closing(_open_readonly(main_db)) as conn:
            if not _table_exists(conn, "execution_ledger"):
                return None
            row = conn.execute(
                "SELECT PAYLOAD_JSON FROM execution_ledger WHERE KIND = ? AND IDEMPOTENCY_KEY = ?",
                (CUTOVER_ATTEMPT_LEDGER_KIND, CUTOVER_ATTEMPT_LEDGER_KEY),
            ).fetchone()
    except sqlite3.Error as exc:
        raise CutoverUnavailable(
            f"本体 DB {main_db} の execution_ledger を読めませんでした: {exc}"
        ) from exc
    if row is None:
        return None
    try:
        at = _parse_utc(json.loads(row[0])["cutover"])
    except (ValueError, KeyError, TypeError) as exc:
        raise CutoverUnavailable(
            f"本体 DB {main_db} の {CUTOVER_ATTEMPT_LEDGER_KIND} の行の時刻を読めませんでした: {exc}"
        ) from exc
    if at is None:
        raise CutoverUnavailable(
            f"本体 DB {main_db} の {CUTOVER_ATTEMPT_LEDGER_KIND} の行の時刻を読めませんでした"
        )
    return at


def resolve_cutover(
    saiverse_home: Path, *, now: datetime, dry_run: bool = False,
    main_db: Optional[Path] = None,
) -> Cutover:
    """この環境で新しい形式に切り替わった時刻 (それより後に作られた行は自動では変換しない)。

    記録 (``<SAIVERSE_HOME>/backups/system_notice_migration/cutover.json``) があれば
    それを返し、決め直さない。記録が無ければ決めて記録する:

    - 記憶 DB の完了の印の完了時刻と、この移行が前の起動で書いた記録の時刻
      (:func:`_earliest_migration_trace`) と、``cutover.json`` を書けなかった前の起動が
      本体 DB ``main_db`` に残した時刻 (:func:`_cutover_attempt_in_main_db`) のうち、
      最も早いもの。この移行を含む版を既に動かした環境の、最初の移行の時刻に最も近い
      時刻 — その起動からは、機構は旧形式の本文を書かない。印だけで決めないのは、
      v0.3.27 の起動のときに記憶 DB が無かった環境 (印がどこにも残らない) と、古い
      記憶 DB を消して残った最古の印が遅くなった環境で、境が実際の切り替えより遅く
      決まるため。
    - どれも無ければ ``now`` (この起動の、移行を始めた時刻)。

    ``dry_run`` は決めた時刻を記録しない。決める材料 (記録・完了の印・移行が前に書いた
    記録・本体 DB に残した時刻) を読めない、または記録できなければ
    :class:`CutoverUnavailable` — 呼び出し側はその起動で自動の変換を一つもしない。
    そのとき (``dry_run`` でなければ) 失敗した時点までに読めた候補と ``now`` のうち
    最も早い時刻を本体 DB に残す (:func:`_leave_cutover_attempt`)。候補を全部読み
    終えてから比べると、最古の印を読んだ後に別の記録の読み取りで止まった起動は
    ``now`` を残し、その印を持つ記憶 DB が後で消えると、印の時刻から ``now`` までの
    行が境より前になる。読めた候補はどれも実際の切り替えより早いことはなく、
    ``now`` も同じなので、残した時刻で境が実際より早まることはない。
    """
    path = _cutover_path(saiverse_home)
    start = Cutover(now.astimezone(timezone.utc), CUTOVER_FROM_START)
    # 決め方ごとの、読めた時刻 (読んだ順)。途中で止まっても、それまでの分は残る。
    # 同じ時刻なら先に並べた決め方を取る (min は最初の最小を返す)
    found: Dict[str, List[datetime]] = {
        CUTOVER_FROM_MARKER: [], CUTOVER_FROM_TRACE: [], CUTOVER_FROM_ATTEMPT: [],
    }

    def _earliest_found() -> Optional[Cutover]:
        candidates = [Cutover(at, source) for source, times in found.items() for at in times]
        return min(candidates, key=lambda c: c.at) if candidates else None

    try:
        recorded = _read_cutover(path)
        if recorded is not None:
            return recorded
        _earliest_completion_marker(saiverse_home, found[CUTOVER_FROM_MARKER])
        _earliest_migration_trace(saiverse_home, found[CUTOVER_FROM_TRACE])
        attempt = _cutover_attempt_in_main_db(main_db)
        if attempt is not None:
            found[CUTOVER_FROM_ATTEMPT].append(attempt)
        cutover = _earliest_found() or start
        if dry_run:
            return cutover
        recorded = _record_cutover(path, cutover, saiverse_home=saiverse_home)
    except CutoverUnavailable:
        if not dry_run:
            earliest = _earliest_found()
            _leave_cutover_attempt(
                main_db, earliest if earliest is not None and earliest.at < start.at else start,
            )
        raise
    LOGGER.info(
        "[system-notice-migration] recorded the cutover time %s (%s) in %s",
        recorded.at.isoformat(), recorded.source, path,
    )
    return recorded


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


class RunResultsUnreadable(Exception):
    """起動のフォルダの結果の JSON のうち、読めないものがある (:func:`_load_run_results`)。"""


def _load_run_results(run_dir: Path) -> List[StoreResult]:
    """起動のフォルダの結果の JSON を全部読む。一つでも読めなければ :class:`RunResultsUnreadable`。

    読めた分だけでレポートを書くと、読めなかった DB の変換が全件のファイルから欠けた
    まま ``REPORTED`` が置かれ、次の起動でも作り直されない。
    """
    out: List[StoreResult] = []
    unreadable: List[Path] = []
    for path in sorted(run_dir.glob("*.json")):
        try:
            out.append(StoreResult.from_json(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, KeyError, TypeError):
            LOGGER.warning(
                "[system-notice-migration] unreadable result file %s", path, exc_info=True,
            )
            unreadable.append(path)
    if unreadable:
        raise RunResultsUnreadable(
            "読めない結果の JSON があるので、レポートを確定しません (次の起動で再び試す): "
            + ", ".join(str(p) for p in unreadable)
        )
    # 本体 DB を先に、ペルソナは名前の順に
    out.sort(key=lambda r: (r.persona, r.label))
    return out


#: コミットされたかを確かめられない仮の結果の JSON に付ける名前の末尾
#: (``<key>.json.tmp.unverified`` — :func:`_settle_staged_results`)。以後の起動では採否の
#: 対象にしない (``*.json`` にも ``*.json.tmp`` にも当たらない)。
UNVERIFIED_SUFFIX = ".unverified"


def _unverified_results(run_dir: Path) -> List[Path]:
    return sorted(run_dir.glob(f"*.json{STAGED_SUFFIX}{UNVERIFIED_SUFFIX}"))


def _write_run_report(run_dir: Path, report_path: Path, *, stamp: str) -> Optional[Path]:
    """一回の起動の結果の JSON 群からレポートを書き、その起動のフォルダに書き終えた印を置く。

    結果の JSON を一つでも読めなければ :class:`RunResultsUnreadable` を送り、何も書かない。
    ``REPORTED`` は、全件のファイルと通常のレポートを両方とも書き終えてから置く。
    """
    results = _load_run_results(run_dir)
    unverified = _unverified_results(run_dir)
    if not results and not unverified:
        return None
    try:
        # 起動ごとのフォルダは <home>/backups/<OUTPUT_DIR_NAME>/<RESULTS_DIR_NAME>/<run_id>
        cutover = _read_cutover(run_dir.parent.parent / CUTOVER_FILE_NAME)
    except CutoverUnavailable:
        cutover = None  # レポートの一行が欠けるだけ (移行そのものは記録を読めた起動だけが走る)
    _write_report_files(
        report_path, results, dry_run=False, stamp=stamp, cutover=cutover, unverified=unverified,
    )
    (run_dir / REPORTED_MARKER_NAME).write_text(str(report_path), encoding="utf-8")
    return report_path


def _settle_staged_results(run_dir: Path) -> int:
    """終わった起動のフォルダに残った仮の結果の JSON (``*.json.tmp``) の採否を決める。

    採用した数と :data:`UNVERIFIED_SUFFIX` を付けて残した数の和 (レポートの中身を変えた数) を返す。

    仮の JSON はコミットの前に書くので、残っているのは「コミットの前に止まった」
    か「コミットの後、正式な名前へ付け替える前に止まった (付け替えに失敗した)」
    かのどちらか。区別は DB に残ったその回の記録で付ける:

    - 仮の JSON の ``commit_record`` (変換をコミットする取引が書いた回の記録の名前)
      が DB にあれば、その取引はコミットされている — 記憶 DB は ``embed_metadata`` の
      :data:`COMMIT_RECORD_KEY_PREFIX` の鍵、本体 DB は実行台帳の
      :data:`COMMIT_LEDGER_KIND` の行 (:func:`_commit_record_exists`)。完了の印を
      見送った回と本体 DB も、これで採用できる。
    - 記憶 DB は、完了の印が仮の JSON に書いた値 (``marker_value`` — 書いた取引で
      立てた印) と一致する場合も採用する (回の記録を書かなかった前の版の仮の JSON)。

    DB を開いて、その回の記録 (と完了の印) が無いと確かめられたときだけ捨てる —
    確定していない変換をレポートの材料にしない。照合に使う欄 (``commit_record`` と
    ``marker_value``) をどちらも持たない、または記録先の DB のパス (``path``) を
    持たない仮の JSON は、コミットされたかを確かめる手段が無い。捨てると、コミット
    された変換の唯一の記録を失いうるので、名前の末尾に :data:`UNVERIFIED_SUFFIX` を
    付けて残し (以後の起動では採否の対象にしない)、その起動のレポートに一行書く。
    この版が書くそういう仮の JSON は、変換をコミットしなかった回 (変換する行が無く、
    完了の印も見送った記憶) のものだけ — 本体 DB の変換は、その回の記録を書ける
    ときだけコミットする (:func:`migrate_main_db`)。回の記録を書く前の版 (v0.3.27) が
    本体 DB の分として書いた仮の JSON は、この欄を持たない。
    記録先の DB が見つからない・読めない (ロックなど) ときと、仮の JSON そのものが
    読めない (一時的な I/O・権限の失敗) ときは決めずに残し、次の起動でまた試す。読めたのに JSON と
    して壊れている仮の JSON は書きかけで止まったもので、捨てる — 仮の JSON は
    OS に確定させてからコミットする (:func:`_stage_result_json`。v0.3.27 も同じ) ので、
    コミットされた取引の仮の JSON が書きかけで残ることはない。

    採用と :data:`UNVERIFIED_SUFFIX` への付け替えはレポートの中身を変えるので、その前に
    その起動のフォルダの ``REPORTED`` を外す (作り直しの書き出しが失敗しても、次の起動で
    また作り直せるように)。外せなければその仮の JSON は決めずに残す。
    """
    from sai_memory.memory.storage import get_embed_metadata_strict

    adopted = 0
    unreported = False

    def _unreport() -> bool:
        nonlocal unreported
        if not unreported:
            try:
                (run_dir / REPORTED_MARKER_NAME).unlink(missing_ok=True)
            except OSError:
                LOGGER.warning(
                    "[system-notice-migration] could not remove %s before settling the staged "
                    "results; will retry on the next start", run_dir / REPORTED_MARKER_NAME,
                    exc_info=True,
                )
                return False
            unreported = True
        return True

    for staged in sorted(run_dir.glob(f"*.json{STAGED_SUFFIX}")):
        try:
            raw = staged.read_bytes()  # 復号の失敗 (書きかけ) は読み取りの失敗と分けて下で扱う
        except OSError:
            LOGGER.warning(
                "[system-notice-migration] could not read %s to settle it; "
                "will retry on the next start", staged, exc_info=True,
            )
            continue
        half_written = False
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            data = None
            half_written = True  # 書きかけで止まった — 確定した結果ではない
        data = data if isinstance(data, dict) else {}
        expected = data.get("marker_value")
        record = data.get("commit_record")
        persona = bool(data.get("persona"))
        keep = False
        checkable = isinstance(record, str) or (persona and isinstance(expected, str))
        db = Path(data["path"]) if checkable and isinstance(data.get("path"), str) else None
        if not half_written and db is None:
            # コミットされたかを確かめる手段が無い — 捨てずに残す
            unverified = staged.with_name(staged.name + UNVERIFIED_SUFFIX)
            if not _unreport():
                continue
            try:
                os.replace(staged, unverified)
            except OSError:
                LOGGER.warning(
                    "[system-notice-migration] could not set aside %s", staged, exc_info=True,
                )
                continue
            adopted += 1
            LOGGER.warning(
                "[system-notice-migration] kept %s as %s: it has no commit_record / marker_value "
                "/ path to check whether its migration was committed", staged, unverified.name,
            )
            continue
        if db is not None and not db.is_file():
            # 記録先の DB が見えない (一時的に外れた場所・移動の途中など)。記録が無いとは
            # 確かめられていないので捨てない
            LOGGER.warning(
                "[system-notice-migration] %s to settle %s is not found; "
                "will retry on the next start", db, staged,
            )
            continue
        if db is not None:
            try:
                with closing(_open_readonly(db)) as conn:
                    if isinstance(record, str):
                        keep = _commit_record_exists(conn, record, persona=persona)
                    if not keep and persona and isinstance(expected, str):
                        keep = get_embed_metadata_strict(conn, MIGRATION_MARKER_KEY) == expected
            except sqlite3.Error:
                LOGGER.warning(
                    "[system-notice-migration] could not read %s to settle %s; "
                    "will retry on the next start", db, staged, exc_info=True,
                )
                continue
        final = staged.with_name(staged.name[: -len(STAGED_SUFFIX)])
        if keep and not _unreport():
            continue
        try:
            if keep:
                os.replace(staged, final)
                adopted += 1
                LOGGER.info("[system-notice-migration] took up the committed result %s", final)
            else:
                staged.unlink()
                LOGGER.info(
                    "[system-notice-migration] discarded %s (its migration was not committed)",
                    staged,
                )
        except OSError:
            LOGGER.warning(
                "[system-notice-migration] could not settle %s", staged, exc_info=True,
            )
    return adopted


def _commit_record_exists(conn: sqlite3.Connection, record: str, *, persona: bool) -> bool:
    """その回の変換をコミットした記録が DB にあるか (読めなければ sqlite3.Error を送る)。"""
    from sai_memory.memory.storage import get_embed_metadata_strict

    if persona:
        return get_embed_metadata_strict(conn, COMMIT_RECORD_KEY_PREFIX + record) is not None
    if not _table_exists(conn, "execution_ledger"):
        return False
    row = conn.execute(
        "SELECT 1 FROM execution_ledger WHERE KIND = ? AND IDEMPOTENCY_KEY = ?",
        (COMMIT_LEDGER_KIND, record),
    ).fetchone()
    return row is not None


_STAGED_PERSONA_PREFIX = "persona_"


def _unsettled_staged_persona_ids(
    output_dir: Path, *, current_run: Optional[Path] = None,
) -> Optional[FrozenSet[str]]:
    """終わった起動のフォルダに、採否の決まっていない仮の結果の JSON が残る記憶の persona_id。

    :func:`regenerate_pending_reports` が採否を決められなかった (仮の JSON か DB が
    一時的に読めなかった) 分。その記憶の完了の印を書き換えると、仮の JSON に書いた
    印の値と一致しなくなり、次の起動でコミット済みの結果が捨てられる — だから
    呼び出し側はその起動で完了の印を見送る (:data:`MARKER_HELD_STAGED_RESULT`)。
    走っている別の起動のフォルダ (:func:`_run_in_progress`) と、この起動のフォルダは
    数えない。置き場を読めなければ None (どの記憶のものか分からない)。
    :data:`UNVERIFIED_SUFFIX` を付けた仮の JSON は採否の対象ではないので数えない。

    走っている別の起動が、同じ SAIVERSE_HOME で動く別の版 (旧版) のプロセスで、仮の
    JSON を書いた直後にこの起動が完了の印を書き換えると、旧版の付け替えが失敗したときに
    次の起動で印が一致しなくなりうる。同じ SAIVERSE_HOME で二つの版を同時に動かすことは
    想定しない使い方なので、この競合は扱わない。
    """
    root = output_dir / RESULTS_DIR_NAME
    suffix = f".json{STAGED_SUFFIX}"
    ids = set()
    try:
        if not root.is_dir():
            return frozenset()
        for run_dir in root.iterdir():
            if not run_dir.is_dir() or (current_run is not None and run_dir == current_run):
                continue
            staged = [
                p.name for p in run_dir.glob(f"{_STAGED_PERSONA_PREFIX}*{suffix}")
            ]
            if not staged or _run_in_progress(run_dir):
                continue
            ids.update(name[len(_STAGED_PERSONA_PREFIX):-len(suffix)] for name in staged)
    except OSError:
        LOGGER.warning(
            "[system-notice-migration] could not look for unsettled staged results in %s",
            root, exc_info=True,
        )
        return None
    return frozenset(ids)


def regenerate_pending_reports(saiverse_home: Path) -> List[Path]:
    """前の起動で結果の JSON を書いたのにレポートを書けなかった分を、JSON 群から作り直す。

    DB には書かない (完了の印はそのまま尊重する)。作り直したレポートのパスを返す。
    まだ走っている起動のフォルダ (:func:`_run_in_progress` — 同時に起動した別の
    プロセスが移行の途中) は対象にしない。その起動が自分でレポートを書くか、
    書けずに終われば次の起動で作り直される。

    終わった起動のフォルダに仮の結果の JSON が残っていれば、先に採否を決める
    (:func:`_settle_staged_results`)。採用した JSON、または :data:`UNVERIFIED_SUFFIX`
    を付けて残した JSON があれば、レポートを書き終えた起動のフォルダでもレポートを
    作り直す (その前に ``REPORTED`` を外すので、作り直しの書き出しが失敗しても次の起動で
    また作り直す)。結果の JSON を一つでも読めないフォルダは、レポートを確定せずに残す
    (:class:`RunResultsUnreadable`)。
    """
    output_dir = saiverse_home / "backups" / OUTPUT_DIR_NAME
    results_root = output_dir / RESULTS_DIR_NAME
    if not results_root.is_dir():
        return []

    def _has_material(run_dir: Path) -> bool:
        return any(run_dir.glob("*.json")) or bool(_unverified_results(run_dir))

    written: List[Path] = []
    for run_dir in sorted(results_root.iterdir()):
        if not run_dir.is_dir():
            continue
        has_staged = any(run_dir.glob(f"*.json{STAGED_SUFFIX}"))
        if (run_dir / REPORTED_MARKER_NAME).exists() and not has_staged:
            continue
        if not has_staged and not _has_material(run_dir):
            continue
        if _run_in_progress(run_dir):
            continue
        if has_staged:
            _settle_staged_results(run_dir)
        # 採否で中身が変わる前に REPORTED は外れている (_settle_staged_results)
        if (run_dir / REPORTED_MARKER_NAME).exists():
            continue
        if not _has_material(run_dir):
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
    (書き換えを伴う DB の JSON はコミットの前に仮の名前で書き、コミットの後に
    正式な名前へ付け替える — :func:`_stage_result_json`。レポートが書けなくても
    次の起動で :func:`regenerate_pending_reports` が作り直せる)。本体 DB の残した行は、
    前回と同じ行なら材料にしない。``dry_run`` は JSON を書かず、見るべきものが
    あればその場の結果からレポートを書く。

    どの DB にも触る前に、新しい形式に切り替わった時刻を決める (:func:`resolve_cutover`)。
    決められない・記録できなければ :class:`CutoverUnavailable` を送り、何も変換しない
    (次の起動で再試行する)。
    """
    started = datetime.now(timezone.utc)
    stamp = started.astimezone().strftime("%Y%m%d_%H%M%S")
    cutover = resolve_cutover(saiverse_home, now=started, dry_run=dry_run, main_db=db_path)
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
                cutover=cutover.epoch,
            ))
        except Exception as exc:  # noqa: BLE001 — 一つの DB の失敗で他を止めない
            LOGGER.error("[system-notice-migration] main DB migration failed", exc_info=True)
            failed = StoreResult(
                label="本体 DB", path=str(db_path), error=f"移行に失敗しました (変更は巻き戻し済み): {exc}",
            )
            results.append(failed)
            _try_write_result_json(results_dir, "main", failed)

    # 規則 3 の「実在する Playbook 名」は移行の時点の世界で一度だけ決める
    playbook_names, names_complete = known_playbook_names(db_path)
    # 記憶 DB の完了の印を立てる前提 (欠けがあれば、変換だけをコミットして印は見送る)
    held_for_all: List[str] = []
    if not names_complete:
        held_for_all.append(MARKER_HELD_PLAYBOOK_NAMES)
    if any(r.error for r in results):
        held_for_all.append(MARKER_HELD_MAIN_DB_FAILED)
    # 前の起動の仮の結果の JSON の採否がまだ決まっていない記憶 (None = 確かめられない
    # ので全部をそう扱う)
    unsettled = (
        frozenset() if results_dir is None
        else _unsettled_staged_persona_ids(output_dir, current_run=results_dir)
    )
    # 規則 5 の行の元の発言の行は本体 DB から引く (本体 DB が無ければ、規則 5 の行は
    # 確認待ちになる)
    with BuildingSourceRows(db_path) as source_rows:
        for persona_id, persona_db in _iter_persona_dbs(saiverse_home):
            held = list(held_for_all)
            if unsettled is None or persona_id in unsettled:
                held.append(MARKER_HELD_STAGED_RESULT)
            try:
                results.append(migrate_persona_db(
                    persona_db, persona_id=persona_id, output_dir=output_dir,
                    dry_run=dry_run, stamp=stamp, results_dir=results_dir,
                    playbook_names=playbook_names, hold_marker=held, cutover=cutover.epoch,
                    source_rows=source_rows,
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
            _write_report_files(written, results, dry_run=True, stamp=stamp, cutover=cutover)
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
    # レポートは開発者向けのファイルなので、作り直しは画面に出さずログにだけ残す
    # (regenerate_pending_reports が作り直したパスを info で記録する)
    try:
        regenerate_pending_reports(home)
    except Exception:  # noqa: BLE001
        LOGGER.error("[system-notice-migration] report regeneration failed", exc_info=True)
    try:
        results, report = run_migration(db_path=Path(db_path), saiverse_home=home)
    except CutoverUnavailable:
        # 何も変換していない。次の起動で、切り替わりの時刻を決め直して (記録が
        # あればそれを読んで) 再試行する
        LOGGER.error(
            "[system-notice-migration] the cutover time is unavailable; no record was "
            "converted on this start", exc_info=True,
        )
        return alerts + [_alert(
            "system_notice_migration_cutover_unavailable", "warning", "cutoverUnavailable",
            _ui("cutoverUnavailableMessage"),
        )]
    except Exception:  # noqa: BLE001
        LOGGER.error("[system-notice-migration] migration aborted", exc_info=True)
        return alerts + [_alert(
            "system_notice_migration_failed", "warning", "aborted", _ui("abortedMessage"),
        )]
    return alerts + startup_alerts(results, report)


#: 起動時の知らせの文言の鍵の接頭辞 (文言は frontend/src/i18n/messages.json にある。
#: 確認の画面の警告 saiverse/system_notice_review.py の ``review_alert`` と同じ方式)。
_UI_PREFIX = "api.systemNoticeMigration."


def _ui(name: str, **params: Any) -> Dict[str, Any]:
    from api.ui_messages import ui_message

    return ui_message(_UI_PREFIX + name, **params)


def _alert(
    alert_id: str, level: str, title: str, message: Dict[str, Any],
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "id": alert_id,
        "level": level,
        "title": _ui(f"{title}Title"),
        "message": message,
        "details": details or {},
    }


def _alert_label(result: StoreResult) -> Dict[str, Any]:
    """知らせに並べる DB の名前 (ペルソナの記憶は persona_id。:func:`_iter_persona_dbs` の名前)。"""
    if result.persona:
        return _ui("personaLabel", id=Path(result.path).parent.name)
    return _ui("mainDbLabel")


def startup_alerts(
    results: Sequence[StoreResult], report: Optional[Path],
) -> List[Dict[str, Any]]:
    """移行の結果から、画面のバナーへ積む知らせを組む (文言は翻訳の鍵と引数)。

    確認待ちの行の件数と、レポートのファイルの場所は画面に出さない
    (docs/intent/system_notice_delivery.md「追補」— 確認待ちの行の案内は、
    確認待ちの行を確定する画面が自分の入口として出す)。
    """
    alerts: List[Dict[str, Any]] = []
    failed = [r for r in results if r.error]
    saved = [r for r in results if r.result_saved]
    unsaved = [r for r in results if r.result_save_failed]
    converted_rows = sum(
        n for r in results if not r.error
        for (table, _kind), n in r.counts().items() if table != SPLIT_ADDED_TABLE
    )
    if unsaved:
        persona_note = (
            _ui("resultUnsavedPersonaNote")
            if any(r.persona and not r.error for r in unsaved) else ""
        )
        main_note: Any = ""
        if any(not r.persona for r in unsaved):
            main_note = _ui(
                "resultUnsavedMainNoteWithLog"
                if any(not r.persona and r.changes for r in unsaved)
                else "resultUnsavedMainNote"
            )
        alerts.append(_alert(
            "system_notice_migration_result_unsaved", "warning", "resultUnsaved",
            _ui("resultUnsavedMessage", personaNote=persona_note, mainNote=main_note),
            {"unsaved": [_alert_label(r) for r in unsaved]},
        ))
    if saved and report is None:
        # 材料は書けたのにレポートが書けなかった (レポートは次の起動で材料から
        # 作り直す — regenerate_pending_reports)。
        converted = any(r.changes for r in saved)
        alerts.append(_alert(
            "system_notice_migration_report_failed", "warning", "reportFailed",
            _ui(
                "reportFailedMessageAfterDone" if converted and not failed
                else "reportFailedMessage"
            ),
        ))
    # 前提が欠けて完了の印を見送った記憶 (本体 DB の失敗による見送りは、下の
    # 「移行できなかった」の知らせが伝える)
    held = [
        r for r in results if not r.error
        and {MARKER_HELD_PLAYBOOK_NAMES, MARKER_HELD_STAGED_RESULT} & set(r.marker_held)
    ]
    if held:
        reasons = {reason for r in held for reason in r.marker_held}
        alerts.append(_alert(
            "system_notice_migration_held", "warning", "held",
            _ui(
                "heldMessage",
                playbookNote=(
                    _ui("heldPlaybookNote") if MARKER_HELD_PLAYBOOK_NAMES in reasons else ""
                ),
                stagedNote=(
                    _ui("heldStagedNote") if MARKER_HELD_STAGED_RESULT in reasons else ""
                ),
            ),
            {"held": [_alert_label(r) for r in held]},
        ))
    # metadata を読めないので確認待ちの印を付けられない行が残る記憶 (完了の印を見送り、
    # 行が残る間は毎起動でここに出る)
    unreadable = [
        r for r in results if not r.error
        and MARKER_HELD_UNREADABLE_METADATA in r.marker_held and r.unreadable_metadata_count
    ]
    if unreadable:
        alerts.append(_alert(
            "system_notice_migration_unreadable_metadata", "warning", "unreadableMetadata",
            _ui(
                "unreadableMetadataMessage",
                count=sum(r.unreadable_metadata_count for r in unreadable),
            ),
            {"unreadable_metadata": [
                _ui(
                    "personaCountLabel", id=Path(r.path).parent.name,
                    count=r.unreadable_metadata_count,
                )
                for r in unreadable
            ]},
        ))
    # 本体 DB に execution_ledger が無いので変換をコミットしなかった (理由が他の失敗と
    # 違い、表が作られるまで毎起動で続くので、別の知らせにする)
    if any(r.commit_record_unavailable for r in failed):
        alerts.append(_alert(
            "system_notice_migration_commit_record_unavailable", "warning",
            "commitRecordUnavailable", _ui("commitRecordUnavailableMessage"),
        ))
    other_failed = [r for r in failed if not r.commit_record_unavailable]
    if other_failed:
        alerts.append(_alert(
            "system_notice_migration_failed", "warning", "failed", _ui("failedMessage"),
            {"failed": [_alert_label(r) for r in other_failed]},
        ))
    elif not failed and report is not None and converted_rows:
        # 古いスキーマで確認待ちの印を付けられず、完了の印を見送った記憶
        deferred_note = (
            _ui("doneDeferredNote")
            if any(r.review_marks_deferred and not r.error for r in results) else ""
        )
        alerts.append(_alert(
            "system_notice_migration_done", "info", "done",
            _ui("doneMessage", count=converted_rows, deferredNote=deferred_note),
            {"converted": converted_rows},
        ))
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
    # 指定の打ち間違いで、何も無い場所にレポートや結果の置き場を作り「移行するものが
    # 無かった」と見える結果を出さない。移行を始める前に拒否する。
    if not home.is_dir():
        parser.error(f"--home {home} はフォルダとして存在しません (何も作らずに止めます)")
    if args.db and not Path(args.db).is_file():
        parser.error(f"--db {args.db} はファイルとして存在しません (何も作らずに止めます)")
    db = Path(args.db) if args.db else home / "user_data" / "database" / "saiverse.db"
    if not args.dry_run:
        for regenerated in regenerate_pending_reports(home):
            print(f"regenerated report: {regenerated}")
    try:
        results, report = run_migration(
            db_path=db if db.exists() else None,
            saiverse_home=home,
            dry_run=args.dry_run,
            report_path=Path(args.report) if args.report else None,
        )
    except CutoverUnavailable as exc:
        print(f"nothing was converted: {exc}")
        return 1
    for res in results:
        print(f"{res.label}: converted={len(res.changes)} skipped={len(res.skipped)} "
              f"unknown_system={res.unknown_system_count} done_before={res.already_done} "
              f"rescan_from_version={res.rescan_from_version} error={res.error}")
    print(f"report: {report}")
    return 0 if not any(r.error for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
