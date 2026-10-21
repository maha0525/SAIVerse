from __future__ import annotations

import copy
import functools
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from llm_clients.utils import mark_attachment_limit_off
from sai_memory.perception_buffer import PERCEPTION_OMISSION_HEADER
from sea.eviction_plan import (
    CONSUMED_PERCEPTION_KEY,
    PERSISTED_REALTIME_AND_RECALL_KEY,
    TURN_REALTIME_AND_RECALL_KEY,
    is_injected_perception,
    is_persisted_realtime_and_recall,
)
from sea.runtime_utils import event_building_id
from saiverse.model_configs import (
    calculate_cost,
    get_context_length,
    get_model_display_name,
    get_model_pricing,
    get_model_provider,
)
from tools import SPELL_TOOL_SCHEMAS, TOOL_REGISTRY

LOGGER = logging.getLogger(__name__)


def _reframe_autonomous_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """AUTONOMOUS アスペクトの assistant 発言を user+<system> 形式に変換する。

    軽量モデルで生成された自律行動の口調が、後続パルスのコンテキストに
    assistant ロールとして残ると口調ブレの感染源になる。コンテキスト組み立て
    時のみ変換し、SAIMemory の保存データは変えない。
    """
    result: List[Dict[str, Any]] = []
    for msg in messages:
        aspect = (msg.get("metadata") or {}).get("aspect")
        if aspect == "autonomous" and msg.get("role") == "assistant":
            content = msg.get("content", "")
            reframed_content = (
                "<system>[自律行動の記録]\n"
                "あなたは自律的に以下の行動を取りました：\n"
                f"```\n{content}\n```\n"
                "</system>"
            )
            reframed = {k: v for k, v in msg.items() if k not in ("role", "content")}
            reframed["role"] = "user"
            reframed["content"] = reframed_content
            result.append(reframed)
        else:
            result.append(msg)
    return result


def mark_cache_boundary(
    history: List[Dict[str, Any]], pulse_id: Optional[str],
) -> List[Dict[str, Any]]:
    """履歴の中の最後の AI の返事に、キャッシュの境目の内部印を付ける。

    継続キャッシュ (高速応答モード、docs/intent/cache_lifecycle_control.md §3.5) の
    キャッシュの範囲は「head + 履歴の最後の AI の返事まで」。その後ろ (ユーザーの
    発言・知覚・リアルタイム情報・自動想起・Pulse 内のスペルの往復) は回ごとに変わる。
    境目は Pulse が始まる前の履歴で決まるので、同じ Pulse の中の何回目の呼び出しでも
    同じ位置になる — そのため、この Pulse 自身が書いた AI の発言 (``pulse_id`` が
    今の Pulse のもの) は境目にしない (Pulse の途中で組み直す入れ子の Playbook 用)。

    印は ``metadata[CACHE_BOUNDARY_METADATA_KEY]``。元の dict は書き換えず、印を付ける
    1 件だけ複製する (履歴の dict は他の経路と共有されうる)。各 LLM クライアントは
    metadata を API へ送らないので、印が payload に載ることはない。
    """
    from llm_clients.utils import CACHE_BOUNDARY_METADATA_KEY

    for idx in range(len(history) - 1, -1, -1):
        msg = history[idx]
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        if pulse_id is not None and msg.get("pulse_id") == pulse_id:
            continue
        metadata = msg.get("metadata")
        new_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        new_metadata[CACHE_BOUNDARY_METADATA_KEY] = True
        marked = list(history)
        marked[idx] = {**msg, "metadata": new_metadata}
        return marked
    return history


# ---------------------------------------------------------------------------
# リアルタイム情報と自動想起を残して送り続ける運用 (モデルごとの設定)
# docs/intent/persist_realtime_and_recall.md
# ---------------------------------------------------------------------------

#: 送る列に出すブロックが持つ、位置を決める発言の id
#: (``merge_perception_blocks`` がこの発言の直後に置く)。残した行は必ず持つ。
#: 知覚のまとめは、残す設定のモデルへ送るときだけ持つ — 境界
#: (``boundary_created_at`` / ``boundary_rowid``) で位置を決め、同じ秒の前後が
#: 入れ替わらないようにする (persist_realtime_and_recall.md §4.3)。
BOUNDARY_ANCHOR_ID_KEY = "__boundary_anchor_id__"
#: 残す設定のモデルへ送る列の知覚のまとめ (と省略の印) が持つ印。境界で位置を
#: 決められない (境界が NULL・境界より前の発言が送る列に無い) ときの秒の比較を
#: 「自分の時刻以上の最初の発言の直前」(同じ秒の発言より前) にする —
#: 後から同じ秒の発言が増えても位置が動かないように (§4.3、Codex 九巡目)。
#: 印の無いブロック (OFF のモデルの列・部屋の様子の置き直し) は今までどおり
#: 「自分の時刻以下の最後の発言の直後」。
PLACE_BEFORE_SAME_SECOND_KEY = "__place_before_same_second__"
#: 送る列に出す「残した行」のブロックが持つ、文面に入っている自動想起の
#: ``[[source_type, source_id], ...]`` (次のターンの候補から外すのに使う)。
REALTIME_AND_RECALL_RECALLED_KEY = "__realtime_and_recall_recalled__"
#: 送る列に出す「残した行」のブロックが持つ、その行を残した Pulse の id。
#: 同じ Pulse の中で文脈を組み直す子の Playbook が、親の残した行 (リアルタイム
#: スペルの結果を含む) を既に見ているかを判定する
#: (sea/runtime_llm._deliver_realtime_spell_result — 結果を二度並べない)。
REALTIME_AND_RECALL_PULSE_ID_KEY = "__realtime_and_recall_pulse_id__"
#: 履歴の最古の発言の直前に置く時刻の目安 (「以下、… 以降のやり取りです」) の印。
#: 記録には残らないが、同じ履歴からは毎ターン同じ文面で組み直される。
HISTORY_TIME_ANCHOR_KEY = "__history_time_anchor__"


#: ``prepare_context`` が ``context_meta`` に置き、``run_playbook`` が state の
#: :data:`FIRST_SEND_RECORD_SOURCE_KEY` へ移す、「ターンの最初の LLM 呼び出しの
#: 直前に、送る列の後ろ側を記録から読み直す」ための材料
#: (:class:`FirstSendRecordSource`)。リアルタイム情報と自動想起を残すモデルの
#: 列でだけ置く (persist_realtime_and_recall.md §5「最初の呼び出しの前に足すもの」)。
FIRST_SEND_RECORD_SOURCE_META_KEY = "first_send_record_source"
#: state のキー (上の材料の運び先)。読むのは sea/runtime_llm の
#: ``_align_first_send_with_record`` だけ。
FIRST_SEND_RECORD_SOURCE_KEY = "_first_send_record_source"


@dataclass(frozen=True)
class FirstSendRecordSource:
    """送る列の「最後の記録済みの発言より後ろ」を記録から読み直す材料。

    - ``reread``: 次のターンの ``prepare_context`` が履歴を復元するのと同じ
      取り出し・同じ変換 (:func:`_append_history`) で、いまの記録から履歴の
      部分を組み直す (:func:`reread_recorded_history` を、組んだときの
      建物・指定・モデルで束ねたもの)。失敗したら None。
    - ``boundary_id``: ``prepare_context`` が組んだ履歴の部分で、最後に id を
      持つ発言 (境界)。履歴に id を持つ発言が無ければ None。
    - ``history_len``: ``prepare_context`` が組んだ履歴の部分の件数 (時刻の目安・
      知覚のまとめを含む)。境界が None のとき、履歴の部分が空だったかを見分ける。
    """

    reread: Callable[[], Optional[List[Dict[str, Any]]]]
    boundary_id: Optional[str]
    history_len: int


def reread_recorded_history(
    runtime: Any, persona: Any, building_id: str, reqs: Any,
    model_key: Optional[str],
) -> Optional[List[Dict[str, Any]]]:
    """いまの記録から、次のターンの列の履歴の部分を組み直す (読むだけ)。

    次のターンの ``prepare_context`` と同じ関数 (:func:`_append_history`) を
    通す。次のターンと揃える点:

    - ``pulse_id=None`` — 次のターンは別の Pulse なので、このパルスの行が
      「同じ Pulse の行は線・範囲の篩を素通りする」上書き
      (``_payload_passes_context_filter``) で入ることはない。
    - ``pulse_type=None`` — 継続キャッシュの境目の内部印 (``mark_cache_boundary``)
      を付けない。印は metadata だけで送る中身を変えず、次のターンに付くのは
      このターンの返事以降の発言なので、読み直す範囲には現れない。
    - 起点の前進を永続化しない (``persist_anchor_advance=False``)・
      out-param を書かない (``context_meta=None``)・プレビューの下見を通らない。

    リアルタイム情報・自動想起・head は組まない (読み直すのは履歴と知覚だけ —
    自動想起を二度走らせない)。組み立てに失敗したら None。
    """
    out: List[Dict[str, Any]] = []
    ok = _append_history(
        runtime, persona, building_id, reqs, out,
        pulse_id=None, preview_only=False, model_key=model_key,
        context_meta=None, persist_anchor_advance=False,
        pinned_anchor_id=None, pulse_type=None,
    )
    return out if ok else None


def _is_head_message(msg: Any) -> bool:
    """head 由来のメッセージ (システムプロンプト・Memory Weave・自分の外見)。"""
    from sea.head_pipeline.integration import (
        _MEMORY_WEAVE_CONTEXT_MARKER,
        _SELF_VIEW_MARKER,
        _VISUAL_CONTEXT_MARKER,
    )

    if not isinstance(msg, dict):
        return False
    if msg.get("role") == "system":
        return True
    meta = msg.get("metadata")
    return isinstance(meta, dict) and any(
        meta.get(k) for k in (
            _MEMORY_WEAVE_CONTEXT_MARKER, _SELF_VIEW_MARKER, _VISUAL_CONTEXT_MARKER,
        )
    )


def _record_key(msg: Any) -> Optional[tuple]:
    """記録の同じ行を指す鍵 (発言の id / 知覚のまとめ・残した行の台帳 id)。"""
    if not isinstance(msg, dict):
        return None
    if msg.get("id"):
        return ("id", str(msg["id"]))
    batch_id = _block_batch_id(msg)
    if batch_id:
        return ("batch", batch_id)
    return None


def _is_history_form(msg: Any) -> bool:
    """履歴の組み立てだけが作る形 (知覚のまとめ・省略の印・残した行・時刻の目安)。"""
    meta = msg.get("metadata") if isinstance(msg, dict) else None
    return isinstance(meta, dict) and any(
        meta.get(k) for k in (
            CONSUMED_PERCEPTION_KEY, PERSISTED_REALTIME_AND_RECALL_KEY,
            HISTORY_TIME_ANCHOR_KEY,
        )
    )


def _recorded_region_start(
    messages: Sequence[Dict[str, Any]], source: FirstSendRecordSource,
) -> Optional[int]:
    """送る列で、記録から読み直した並びに揃える部分 (境界の直後) の位置。

    境界 (``prepare_context`` が組んだ履歴の最後の id を持つ発言) が列に無ければ
    None。履歴の部分が空だった列は、head の直後から揃える。履歴の部分はあるのに
    id を持つ発言が一つも無い列 (畳みの跡地だけ等) は、どこまでが組み直される
    部分か決められないので None。
    """
    if source.boundary_id:
        for idx, msg in enumerate(messages):
            if is_injected_perception(msg):
                continue
            if isinstance(msg, dict) and str(msg.get("id") or "") == source.boundary_id:
                return idx + 1
        return None
    if source.history_len:
        return None
    for idx, msg in enumerate(messages):
        if not _is_head_message(msg):
            return idx
    return len(messages)


def _recorded_tail(
    recorded: Sequence[Dict[str, Any]], source: FirstSendRecordSource,
) -> Optional[List[Dict[str, Any]]]:
    """読み直した履歴のうち、境界より後ろ (境界が列に無ければ None)。"""
    if not source.boundary_id:
        return list(recorded)
    for idx, msg in enumerate(recorded):
        if is_injected_perception(msg):
            continue
        if isinstance(msg, dict) and str(msg.get("id") or "") == source.boundary_id:
            return list(recorded[idx + 1:])
    return None


def _align_with_record(
    messages: List[Dict[str, Any]], start: int, tail: Sequence[Dict[str, Any]],
) -> None:
    """``messages[start:]`` の記録済みの行を、読み直した並び ``tail`` に置き換える。

    置き換えるのは、``tail`` に同じ行 (同じ発言の id・同じ台帳 id) があるものと、
    履歴の組み立てだけが作る形 (知覚のまとめ・省略の印・時刻の目安 — 読み直しで
    同じものが組み直される)。それ以外 (そのターンのまとまり、記録に残らない
    差し込み、記録に書いたのに読み直しに出ない行) は、元の順のまま ``tail`` の
    後ろに置く — そのターンのまとまりは記録済みの行より後ろ (列の一番後ろ側) に
    なり、送った直前の固定 (:func:`freeze_turn_realtime_and_recall_for_send`)
    が、その直前の記録済みの行を位置を決める発言にする。
    """
    tail_keys = {k for k in (_record_key(m) for m in tail) if k is not None}
    keep = [
        m for m in messages[start:]
        if not (_is_history_form(m) or _record_key(m) in tail_keys)
    ]
    messages[start:] = list(tail) + keep


def _sent_payload_key(msg: Dict[str, Any]) -> tuple:
    """LLM へ届く中身の比較用の写し (役割・本文・画像・tool call)。"""
    meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
    media = meta.get("media") if isinstance(meta.get("media"), list) else []
    tool_calls = msg.get("tool_calls")
    return (
        msg.get("role"),
        str(msg.get("content") or ""),
        tuple(
            (m.get("path"), m.get("mime_type")) for m in media if isinstance(m, dict)
        ),
        json.dumps(tool_calls, sort_keys=True, default=str) if tool_calls else None,
    )


#: :func:`find_unrecorded_messages` が返す食い違いの種類。
NOT_KEPT = "not_kept"   # 送るのに、次のターンには記録から戻ってこない
NOT_SENT = "not_sent"   # 記録にはあり次のターンに出るのに、送る列のその位置に無い


def find_unrecorded_messages(
    sent_after_boundary: Sequence[Dict[str, Any]],
    recorded_after_boundary: Sequence[Dict[str, Any]],
) -> List[tuple]:
    """送る列の境界より後ろと、記録から読み直した並びの食い違い。

    persist_realtime_and_recall.md 不変条件 1 の機械検査
    (sea/runtime_llm._align_first_send_with_record が、ON のモデルのターンの
    最初の LLM 呼び出しを送る直前に、記録に揃えた後で呼ぶ)。ターンの最初の
    呼び出しで送った列の境界より後ろは、次のターンでは記録から組み直される。
    だから、そのターンのまとまり (生成の完了後に残り、次のターンには同じ位置に
    出る) を除いた並びが、読み直した並びと順序も文面も同じでなければならない。

    返り値は ``(種類, メッセージ)`` の列。種類は :data:`NOT_KEPT` (送る列にだけ
    ある — 記録に残らない差し込み) と :data:`NOT_SENT` (読み直した並びにだけ
    ある — 記録に書かれたのに送る列のその位置に無い行)。空なら一致。
    比べるのは LLM へ届く中身 (役割・本文・画像・tool call) で、metadata の
    内部印は比べない。
    """
    import difflib

    sent = [
        m for m in sent_after_boundary
        if isinstance(m, dict) and _turn_info(m) is None
    ]
    recorded = [m for m in recorded_after_boundary if isinstance(m, dict)]
    matcher = difflib.SequenceMatcher(
        a=[_sent_payload_key(m) for m in sent],
        b=[_sent_payload_key(m) for m in recorded],
        autojunk=False,
    )
    out: List[tuple] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        out.extend((NOT_KEPT, m) for m in sent[i1:i2])
        out.extend((NOT_SENT, m) for m in recorded[j1:j2])
    return out


def align_first_send_with_record(
    messages: List[Dict[str, Any]],
    base_messages: Any,
    source: Optional[FirstSendRecordSource],
) -> Optional[List[tuple]]:
    """ターンの最初の LLM 呼び出しの直前に、送る列の後ろ側を記録に揃えて検査する。

    persist_realtime_and_recall.md §5「最初の呼び出しの前に足すもの」。ON の
    モデルのトップレベルのパルス (user / auto / schedule) で一度だけ呼ばれる
    (sea/runtime_llm._align_first_send_with_record)。

    1. 記録から履歴の部分を読み直す (``source.reread`` — 次のターンと同じ
       取り出し・同じ変換)。
    2. 送る列 ``messages`` と、Playbook の列 ``base_messages``
       (``state["_messages"]`` — 送る列はその写しに action の指示文などを足した
       もの。別物なら両方) の境界より後ろの記録済みの行を、読み直した並びに
       置き換える。記録に残らないもの (そのターンのまとまりなど) は、その後ろに
       元の順で置く。最初の送信の前に記録へ書かれるもの (事前スペル・TOOL の
       結果・ツールの中で書かれる記録・MEMORIZE・子の Playbook が残した行・
       今後足されるもの) を、差し込み口ごとに送る列へ同期する代わりの一点。
    3. 揃えた後の送る列の境界より後ろを、読み直した並びと比べる
       (:func:`find_unrecorded_messages`)。

    返り値は 3 の食い違い (空なら一致)。揃えられなかった (材料が無い・読み直しに
    失敗した・境界が列に無い) ときは None で、列は変えない。
    """
    if source is None:
        return None
    recorded = source.reread()
    if recorded is None:
        return None
    tail = _recorded_tail(recorded, source)
    if tail is None:
        return None
    start = _recorded_region_start(messages, source)
    if start is None:
        return None
    _align_with_record(messages, start, tail)
    if isinstance(base_messages, list) and base_messages is not messages:
        base_start = _recorded_region_start(base_messages, source)
        if base_start is not None:
            _align_with_record(base_messages, base_start, tail)
    return find_unrecorded_messages(messages[start:], tail)


def sent_message_ids(messages: Sequence[Dict[str, Any]]) -> List[str]:
    """送る列にある、記録の行 id (head・知覚のまとめ・差し込みを除く) を並び順に。

    前駆刻印の材料 (sea/message_stamp.py の ``_presented_message_ids``) を、
    実際に送る列から作り直すときに使う (persist_realtime_and_recall.md §5 —
    送る列を記録に揃えた後。Codex 七巡目)。
    """
    out: List[str] = []
    for msg in messages:
        if not isinstance(msg, dict) or not msg.get("id"):
            continue
        if _is_head_message(msg) or is_injected_perception(msg):
            continue
        out.append(str(msg["id"]))
    return out


def kept_block_comes_back(
    source: Optional[FirstSendRecordSource], batch_id: int,
) -> Optional[bool]:
    """残した行 ``batch_id`` が、いまの記録から読み直した履歴に出るか。

    persist_realtime_and_recall.md §5「機械検査」の二つ目 (Codex 七巡目)。
    残した直後に、次のターンと同じ関数で記録から読み直し、残した行がその
    並びに出るかを確かめる (sea/runtime_llm._persist_turn_realtime_and_recall
    が呼び、出なければ WARN)。送る直前の検査
    (:func:`find_unrecorded_messages`) は、そのターンのまとまりがまだ残って
    いない時点で走るので、「残したのに次のターンに出ない」はそちらでは
    拾えない。読み直せない (材料が無い・失敗した) ときは None。
    """
    if source is None:
        return None
    recorded = source.reread()
    if recorded is None:
        return None
    return any(
        is_persisted_realtime_and_recall(m) and _block_batch_id(m) == int(batch_id)
        for m in recorded if isinstance(m, dict)
    )


def persists_realtime_and_recall(persona: Any, model_key: Optional[str]) -> bool:
    """この送る列を組むモデルが「リアルタイム情報と自動想起を残す」設定か。

    判定の一点 (docs/intent/persist_realtime_and_recall.md §3)。送る並び
    (:func:`prepare_context`)・残した行を送るかどうか
    (:func:`list_presented_perception_blocks`)・自動想起の出し方
    (:func:`_maybe_inject_auto_recall`) は、どれもこの関数で決める。

    モデルは ``model_key`` (送る列を組むときのモデル)。無ければ head と同じ
    解決 (:func:`sea.head_pipeline.integration.resolve_default_model_key` —
    ペルソナの標準モデル) に落ちる。組んだ後で別のモデルへ切り替わる経路は、
    組んだときのこの判定に従う。設定が読めないときは OFF (今までどおり) に倒す。
    """
    try:
        from saiverse.model_configs import get_cache_config
        from sea.head_pipeline.integration import resolve_default_model_key

        key = str(model_key) if model_key else resolve_default_model_key(persona)
        return bool(get_cache_config(key).get("persist_realtime_and_recall", False))
    except Exception:
        LOGGER.warning(
            "[sea][prepare-context] could not read persist_realtime_and_recall "
            "(model=%s); treating it as off", model_key, exc_info=True,
        )
        return False


def compose_turn_realtime_and_recall_content(info: Dict[str, Any]) -> str:
    """そのターンのまとまりの文面 (リアルタイム情報 → 自動想起の順に改行で繋ぐ)。"""
    parts = [p for p in (info.get("realtime"), info.get("recall")) if p]
    return "\n".join(parts)


def build_turn_realtime_and_recall_message(
    realtime_msg: Optional[Dict[str, Any]],
    recall_block: Optional[str],
    recalled_keys: Optional[Sequence[Sequence[str]]] = None,
) -> Optional[Dict[str, Any]]:
    """残すモデルへ送る、そのターンのリアルタイム情報と自動想起のまとまり。

    一つのメッセージにする (docs/intent/persist_realtime_and_recall.md §4.1) —
    残すのは一行なので、送るときに二つに分けると次のターンの列 (一行) と
    一致しない。出すものが何も無いターンは None (まとまり自体を作らない —
    realtime_info.md 不変条件 3 と同じ)。

    印は二つ:

    - ``__realtime_context__`` — リアルタイムスペルの結果の追記先
      (sea/runtime_llm._execute_realtime_spells が先頭から見て最初の印付きを
      探す)。プレビューの節分けもこれを見る。
    - :data:`TURN_REALTIME_AND_RECALL_KEY` — 組み直しと保存の材料。

    画像の枚数の上限は、このモデルの列ではそもそも使わない (列ごとの印 —
    :func:`prepare_context` が付ける ``llm_clients.utils.ATTACHMENT_LIMIT_OFF_KEY``)。
    """
    realtime_text = (
        str(realtime_msg.get("content") or "") if realtime_msg else ""
    ) or None
    recall_text = recall_block or None
    if not realtime_text and not recall_text:
        return None
    info: Dict[str, Any] = {
        "realtime": realtime_text,
        "recall": recall_text,
        "recalled": [
            [str(k[0]), str(k[1])] for k in (recalled_keys or [])
            if isinstance(k, (list, tuple)) and len(k) == 2
        ] if recall_text else [],
        "persisted": False,
    }
    metadata: Dict[str, Any] = {
        "__realtime_context__": True,
        TURN_REALTIME_AND_RECALL_KEY: info,
    }
    realtime_meta = realtime_msg.get("metadata") if realtime_msg else None
    if isinstance(realtime_meta, dict) and isinstance(realtime_meta.get("media"), list):
        metadata["media"] = list(realtime_meta["media"])
    return {
        "role": "user",
        "content": compose_turn_realtime_and_recall_content(info),
        "metadata": metadata,
    }


def find_turn_realtime_and_recall_index(
    messages: Sequence[Dict[str, Any]],
) -> Optional[int]:
    """送る列の中の、そのターンのまとまり (残すモデル) の位置。無ければ None。"""
    for idx, msg in enumerate(messages):
        meta = msg.get("metadata") if isinstance(msg, dict) else None
        if isinstance(meta, dict) and isinstance(
            meta.get(TURN_REALTIME_AND_RECALL_KEY), dict
        ):
            return idx
    return None


def _turn_info(msg: Any) -> Optional[Dict[str, Any]]:
    meta = msg.get("metadata") if isinstance(msg, dict) else None
    info = meta.get(TURN_REALTIME_AND_RECALL_KEY) if isinstance(meta, dict) else None
    return info if isinstance(info, dict) else None


def is_turn_realtime_and_recall_sent(turn_msg: Dict[str, Any]) -> bool:
    """そのターンのまとまりが、もう一度でも LLM へ送られたか。

    送ったまとまりは不変 (persist_realtime_and_recall.md §4.1) — 送った後に
    足すと、保存する文面・次の呼び出しの文面・保存して次のターンに出る文面が
    食い違う。
    """
    info = _turn_info(turn_msg)
    return bool(info and info.get("sent"))


def freeze_turn_realtime_and_recall_for_send(
    messages: Sequence[Dict[str, Any]],
) -> None:
    """LLM へ送る直前に、そのターンのまとまりを固定して「保存待ち」にする。

    persist_realtime_and_recall.md §4.1 / §4.2。初めて送るときに、送る文面・
    画像・位置を決める発言 (送る列でまとまりより前にある、id を持つ発言を
    新しい順に) を写し取り、以後は足さない。保存 (生成が正常に完了した地点 —
    sea/runtime_llm._persist_turn_realtime_and_recall) は、この写しを書く。
    二度目以降の送信 (スペルの繰り返し・空応答のやり直し) では何もしない。
    まとまりが無い列 (OFF のモデル・出すものが無いターン) でも何もしない。
    """
    idx = find_turn_realtime_and_recall_index(messages)
    if idx is None:
        return
    turn_msg = messages[idx]
    info = _turn_info(turn_msg)
    if info is None or info.get("sent"):
        return
    meta = turn_msg.get("metadata") or {}
    media = meta.get("media") if isinstance(meta.get("media"), list) else None
    info["pending"] = {
        "content": str(turn_msg.get("content") or ""),
        "media": [dict(m) for m in media if isinstance(m, dict)] if media else None,
        "recalled": [list(k) for k in (info.get("recalled") or [])],
        "boundary_message_ids": [
            str(m.get("id")) for m in reversed(list(messages[:idx]))
            if isinstance(m, dict) and m.get("id")
        ],
    }
    info["sent"] = True


def realtime_spell_handover(caller_state: Any) -> Dict[str, Any]:
    """子の Playbook へ渡す、呼び出し元の列のリアルタイムスペルの状態。

    persist_realtime_and_recall.md §4.1。子の Playbook の state を新しく組む
    入口 (run_playbook スペル・spell_args_decider) は、ここで作った辞書を子の
    parent_state に足す。SUBPLAY ノードは呼び出し元の state そのものを渡すので、
    同じ二つの値がそのまま届く。受け取るのは sea/runtime_graph の
    compile_with_langgraph だけ。

    - ``_persist_realtime_and_recall``: 呼び出し元の列が、リアルタイム情報と
      自動想起を残すモデルの列か。親の列を写す子 (line='sub') は、写した列の
      作られ方 (並び・まとまり) をそのまま引き継ぐ。
    - ``_realtime_spells_executed``: 呼び出し元の列が、リアルタイムスペルの結果を
      もう持っているか (実行したか届けを受けた後か)。親の列を写す子は、ON の
      ときだけこれを引き継いで二度届けない。

    器 (実行済みの状態と結果) はここでは渡さない — PulseContext に一つだけ置き、
    どの入口からも同じ Pulse の器に届く (sea/pulse_context.PulseContext の
    ``realtime_spells_once``)。OFF のモデルの列では両方 False 相当で、子は今までと
    同じく自分で実行する。
    """
    if not isinstance(caller_state, dict):
        return {}
    return {
        "_persist_realtime_and_recall": bool(
            caller_state.get("_persist_realtime_and_recall", False)
        ),
        "_realtime_spells_executed": bool(
            caller_state.get("_realtime_spells_executed", False)
        ),
    }


def copy_turn_realtime_and_recall_for_child(
    messages: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """子の Playbook へ渡す列を作る (そのターンのまとまりだけ写しにする)。

    persist_realtime_and_recall.md §4.1。子の列は親の列の浅い写しなので、
    そのままでは親のまとまりの辞書 (文面・保存待ち・保存済みの印) を共有し、
    子の追記が親の送ったまとまりを書き換える。まとまりだけを辞書ごと写し、
    写しには「子の写し」の印を付けて保存の対象から外す (保存はそのターンの
    親だけが行う)。ほかのメッセージは今までどおり共有する。
    """
    out = list(messages)
    idx = find_turn_realtime_and_recall_index(out)
    if idx is None:
        return out
    original = out[idx]
    meta = dict(original.get("metadata") or {})
    info = copy.deepcopy(_turn_info(original) or {})
    info["child_copy"] = True
    meta[TURN_REALTIME_AND_RECALL_KEY] = info
    if isinstance(meta.get("media"), list):
        meta["media"] = [dict(m) if isinstance(m, dict) else m for m in meta["media"]]
    out[idx] = {**original, "metadata": meta}
    return out


def add_realtime_spell_lines_to_turn_message(
    turn_msg: Dict[str, Any], new_content: str,
) -> bool:
    """リアルタイムスペルの結果の行を、そのターンのまとまりのリアルタイム情報に足す。

    足し方は今までの追記 (リアルタイム情報の ``</system>`` の直前に入れる) と
    同じ。リアルタイム情報がまだ無い (時刻の項目が両方 OFF) なら、スペルの
    結果だけのリアルタイム情報を作る。自動想起の文面には触れない — 文面全体の
    ``</system>`` を置換すると、想起の閉じタグの前にも結果が入ってしまう。

    もう送ったまとまりには足さない (False を返す — 呼び出し側は別の末尾の
    メッセージに置き、保存しない)。足したら True。
    """
    info = _turn_info(turn_msg)
    if info is None or info.get("sent"):
        return False
    if not new_content:
        return True
    realtime = info.get("realtime")
    if realtime:
        if "</system>" in realtime:
            head, sep, tail = realtime.rpartition("</system>")
            realtime = head + "\n" + new_content + "\n" + sep + tail
        else:
            realtime = realtime + "\n" + new_content
    else:
        realtime = f"<system>\n## リアルタイム情報\n{new_content}\n</system>"
    info["realtime"] = realtime
    turn_msg["content"] = compose_turn_realtime_and_recall_content(info)
    return True


#: head (前置き) に必ず並べる Section。**用途やラインで出し分けない。**
#: head は (persona, model) の Session ごとに一つで固定するのが prefix キャッシュ
#: 共有の土台で、章を出し入れすると同一モデルで head が変わってキャッシュが壊れる。
#: 2026-07-23 以前は ContextRequirements のフラグ 4 つ (system_prompt /
#: available_playbooks / memory_weave / visual_context) で選べてしまい、
#: work_session だけ 3 章欠けた head で走っていた (= 同じ lightweight Session 内で
#: head が二種類あった)。フラグを撤去してここに固定した。
#:
#: NOTE: 登録済みだがここに載せていない Section が 2 つある
#: (building_occupants / chronicle_index。building_items は 2026-09-06 に退役)。これらは以前から
#: 一度も render されていない (capture だけされている)。本定数は「今 render されて
#: いるものを固定する」のが目的なので、有効化は別途判断する
#: (docs/issues/llm_call_entry_point_standardization.md の確認事項)。
PERSONA_HEAD_SECTIONS: frozenset[str] = frozenset({
    "common_prompt", "persona_self", "core_memory", "building", "spell_list",
    "autonomy_modes", "self_image", "desk", "memopedia_index",
    "available_playbooks", "memory_weave",
    # 2026-07-30: 判断プロンプトが毎回貼り直していた静的な一覧の移設先
    # (docs/issues/judgment_static_lists_to_head.md)。用途で出し分けない —
    # 判断点だけに出すと同じ model の head が二種類になる。
    # (もう一つの移設先だった purpose_backlog は 2026-08-21 に節ごと退役した —
    #  中身の pickable tracks と欲求候補が供給源ごと消えたため)
    "facilities",
    # 2026-09-06: visual_context (部屋の描画) は head から退役した — 部屋の様子の
    # 置き場は知覚 (tail) 一つ (docs/intent/room_state_packages.md)。
    # 2026-09-25: その退役で一緒に消えていた「自分の外見」と「インベントリ」を
    # 運ぶ章。部屋の描画は戻さない — Memory Weave の後ろに独立した user
    # メッセージとして置かれる (sea/head_pipeline/sections/self_view.py、
    # docs/issues/inventory_and_appearance_dropped_from_context.md)。
    "self_view",
    # 2026-10-12: アドオンからの案内 (sea/head_pipeline/sections/addon_guidance.py)。
    # 一度リアルタイム情報に載せたが、部屋を出て消えたことが出来事として残らない
    # ので、スペル一覧と同じ head + 差分通知の道へ移した。
    "addon_guidance",
})


def _minimal_load_chars(runtime, persona: Any, model_key: Optional[str]) -> int:
    """anchor が無い (ブートストラップ / 修復直後) ときに読む履歴の文字数。

    残す量 (``metabolism_target_chars``、docs/intent/chronicle_eviction.md §4)
    を流用する — 会話窓が常に目指す量なので、初期読み込みも同じ量から始めれば
    提示コンテキストの大きさが飛ばない。旧三水位の低水位が担っていた役割だが、
    低水位は 2026-09-04 に廃止した (docs/issues/
    watermarks_unsatisfiable_when_perception_is_large.md 裁定 5)。
    """
    lifecycle = getattr(runtime, "session_lifecycle", None)
    if lifecycle is not None:
        try:
            watermarks = lifecycle.get_metabolism_watermarks(persona, model_key)
        except Exception:
            # 解決失敗はここで受けて context_length へ退避 — 外側の広い except に
            # 抜けると履歴ゼロで応答が走る (Codex 指摘 2026-07-30)。
            LOGGER.warning(
                "[sea][prepare-context] Watermark resolution failed in minimal load",
                exc_info=True,
            )
            watermarks = None
        if watermarks is not None and watermarks.target > 0:
            return watermarks.target
    return int(getattr(persona, "context_length", 2000) or 2000)


def history_spec_is_empty(history_depth: Any) -> bool:
    """``history_depth`` の指定が「履歴を一件も積まない」ことを意味するか。

    書式が複数あるので、**表記ではなく実効値**で判定する (下の history 取得ロジックと
    同じ解釈をここに写している):

    - ``0`` / ``"0"`` / ``"none"`` / ``None`` — 明示的な無効化
    - ``"0messages"`` のような件数指定でゼロ以下 — 取得件数 0 になる
    - 負値 / 負の件数指定 — 同上

    表記だけを列挙して弾くと ``"0messages"`` のような等価な書き方をすり抜ける
    (2026-07-23 Codex レビュー指摘)。
    """
    if history_depth is None:
        return True
    if isinstance(history_depth, bool):
        return not history_depth
    if isinstance(history_depth, (int, float)):
        return history_depth <= 0
    text = str(history_depth).strip().lower()
    if text in ("", "0", "none", "null"):
        return True
    if text == "full":
        return False
    if text.endswith("messages"):
        try:
            return int(text[: -len("messages")]) <= 0
        except ValueError:
            # 解釈できない件数指定は下流で 10 件にフォールバックする = 空ではない
            return False
    try:
        return int(text) <= 0
    except ValueError:
        # 解釈できない指定は下流で文字数 2000 にフォールバックする = 空ではない
        return False


class PinnedAnchorUnavailableError(RuntimeError):
    """起点を凍結した組成 (``pinned_anchor_id``) で、その起点から履歴を組めなかった。

    起点の凍結は「一回の整理 (Metabolism) は一つの一貫した窓で最後まで走る」
    (2026-08-24 まはー裁定) のための機構で、使うのは Metabolism 実行内の
    スルース経路だけ。凍結が効かないときに通常の起点解決へ黙って落とすと、
    凍結で塞いだ競合 (実行中の §14-2 起点前進で、退場計画の土台とスルース
    入力が別々の窓になる) が静かに再導入される — だからフォールバックせず
    送出する (fail-closed)。呼び出し元 (run_sluice → run_metabolism) は
    スルース失敗 = 退場停止に写像し、次回の Metabolism が再試行する。
    """


def _pinned_history_from_anchor(
    runtime,
    persona,
    history_mgr,
    pinned_anchor_id: str,
    model_key: Optional[str],
    required_line_roles: List[str],
    required_scopes: List[str],
    pulse_id: Optional[str],
) -> List[Dict[str, Any]]:
    """凍結された起点から提示履歴を組む (fail-closed)。

    :func:`resolve_metabolism_anchor` を呼ばない — §14-2 (機構1) の前進判定
    自体を行わないのが凍結の意味論で、``persist_anchor_advance=False``
    (前進を計算するが永続化しない、keepalive 用) とは別物。組めなかったら
    :class:`PinnedAnchorUnavailableError` を送出する (通常解決への
    フォールバック禁止 — クラス docstring 参照)。
    """
    recent = history_mgr.get_history_from_anchor(
        pinned_anchor_id,
        required_line_roles=required_line_roles,
        required_scopes=required_scopes,
        pulse_id=pulse_id,
    )
    recent = runtime.session_lifecycle.apply_window_folds(
        persona, model_key, recent,
    )
    if not recent:
        raise PinnedAnchorUnavailableError(
            f"pinned anchor {pinned_anchor_id!r} yielded no history "
            f"(persona={getattr(persona, 'persona_id', None)!r}); refusing to "
            "fall back to anchor resolution"
        )
    return recent


class WindowFloorUnmetError(RuntimeError):
    """発話直前の最終防衛ライン (会話の行 ≥ 残す量) を用意できなかった。

    :meth:`~sea.session_lifecycle.SessionLifecycle.ensure_window_floor` が
    "unmet" を返した回に ``run_meta_user`` が送出する — Playbook は走っておらず
    副作用ゼロ。``[]`` を返すと PulseController が "completed" と記帳し、
    schedule の occurrence が実行なしで消費されるため、型付き例外で失敗として
    伝える (PulseController は ``runtime_outcome="floor_unmet"``、ScheduleManager
    は failed = 再試行安全に分類する。Codex 二巡目 #2)。ユーザーへの通知は
    ``run_meta_user`` が送出前に一度だけ出す。
    """


class PersonaVoiceWithoutHistoryError(RuntimeError):
    """ペルソナ名義の稼働なのに会話履歴が無い状態で LLM を走らせようとした。

    記憶の連続性が無い存在は別の人格であり、その出力を本人名義で記録することは
    ペルソナ倫理に反する (2026-07-23 まはー裁定)。実際に 2026-07-16〜23 の 3 日間、
    ``work_session`` が ``history_depth=0`` で走り、エアは毎回同じ壁に初見でぶつかり、
    そのたび「システムへの理解が足りていない」という自責を committed に残した。

    履歴を絞ってよいのは「機構名義の処理」— 出力がペルソナ本人の言葉ではなく、
    本人が読む材料になるもの (画像の概要作成、Chronicle 生成など) だけ。それらは
    ``persona_voiced=False`` で呼ぶこと。
    """


def _append_history(
    runtime: Any, persona: Any, building_id: str, reqs: Any,
    messages: List[Dict[str, Any]],
    *,
    pulse_id: Optional[str],
    preview_only: bool,
    model_key: Optional[str],
    context_meta: Optional[Dict[str, Any]],
    persist_anchor_advance: bool,
    pinned_anchor_id: Optional[str],
    pulse_type: Optional[str],
) -> bool:
    """送る列の履歴の部分 (時刻の目安 + 履歴の発言と知覚のまとめ) を ``messages`` の後ろへ足す。

    :func:`prepare_context` の履歴の組み立てそのもの。ON のモデル
    (docs/intent/persist_realtime_and_recall.md) のターンの最初の LLM 呼び出しの
    直前に、送る列の「最後の記録済みの発言より後ろ」を記録から読み直すとき
    (:func:`reread_recorded_history`) も同じこの関数を通る — 次のターンの
    prepare_context が履歴を復元するのと同じ取り出し・同じ変換で読むため。

    組み立てた (失敗しなかった) ら True。履歴を使わない指定 (``history_depth``
    が 0 / ``"none"``)・履歴の器が無い・組み立ての途中の失敗 (ログに残して
    続ける — それまでに足した分は残る) は False。
    """
    history_depth = reqs.history_depth
    if history_depth in [0, "none"]:
        return False
    history_mgr = getattr(persona, "history_manager", None)
    if not history_mgr:
        return False
    try:
        # Phase 3 段階 4-A (line vs tag responsibility separation,
        # 2026-05-01): context construction filters on line_role / scope
        # instead of metadata.tags. Tags are now reserved for semantic
        # classification (search / recall / Chronicle / Memopedia).
        #
        # Default policy:
        #   - line_role IN ('main_line') AND scope IN ('committed')
        #     → ペルソナのメインラインの会話履歴を context に含める
        #   - committed なメタ判断 (line_role='meta_judgment') も
        #     「メインキャッシュに乗った確定来歴」として含まれる
        #     (_payload_passes_context_filter が committed_to_main_cache
        #     = TRUE を main_line 文脈へ通す。03_data_model.md §176)。
        #     Track 切替の確定独白・生きる目的の初回設定がこれに該当する。
        #   - meta_judgment Pulse の discardable メッセージは含めない。
        #     judge プロンプトへは別経路
        #     (MetaLayer._build_recent_judgments_block) で動的注入される。
        #
        # 段階 4-D (2026-05-09): 旧 ``include_internal`` フォールバックを
        # 削除。サブラインメッセージへのアクセスは sub_line が起動する
        # 専用 Playbook 内で完結する設計に統合済み。
        required_line_roles = ["main_line"]
        required_scopes = ["committed"]

        # Parse history_depth format
        # - "full": anchor 以降の提示コンテキスト (anchor 未確立時は残す量ぶんの文字数)
        # - "Nmessages" (e.g., "10messages"): message count limit
        # - integer or numeric string: character limit
        use_message_count = False
        limit_value = 2000  # fallback
        used_anchor = False
        recent = []
        # 提示窓の境界 (anchor)。Chronicle 無効ペルソナのバッチ絞り
        # (下のマージ) が「履歴が空でも窓より古いものは忘れる」を
        # 判定するのに使う (2026-08-19 Codex 第二巡 #3)。
        history_anchor_id: Optional[str] = None

        # 実行 model が水位を持つか。モデル定義で水位を null にした model は
        # Metabolism を持たない = anchor を使わない (これが唯一のオプトアウト、
        # 2026-07-30 OFF トグル撤去)。退場が無いのに anchor 起点で読むと
        # 提示が際限なく伸びるため、水位なし model は従来のスライディング
        # ウィンドウで読む (Codex 指摘 2026-07-30)。
        #
        # 解決の失敗はここで受けて anchor 無効へ退避する — 外側の広い
        # except に抜けると履歴ゼロで応答が走るサイレント障害になる
        # (Codex 指摘 2巡目)。
        _lifecycle = getattr(runtime, "session_lifecycle", None)
        metabolism_active = False
        if _lifecycle is not None:
            try:
                metabolism_active = (
                    _lifecycle.get_metabolism_watermarks(persona, model_key)
                    is not None
                )
            except Exception:
                LOGGER.warning(
                    "[sea][prepare-context] Watermark resolution failed; "
                    "falling back to sliding window",
                    exc_info=True,
                )

        if history_depth == "full":
            if pinned_anchor_id is not None:
                # 起点の凍結 (2026-08-24 まはー裁定「一回の整理は一つの
                # 一貫した窓で最後まで走る」): resolve_metabolism_anchor
                # を呼ばず、呼び出し元 (run_metabolism) が実行頭に撮った
                # 窓の起点から組む。実行中に Chronicle が確定して最前線
                # が動いても、この組成は動かない — §14-2 (機構1) の前進
                # 判定ごと走らせない。組めなければ送出 (fail-closed、
                # ヘルパーの docstring 参照)。
                recent = _pinned_history_from_anchor(
                    runtime, persona, history_mgr, pinned_anchor_id,
                    model_key, required_line_roles, required_scopes,
                    pulse_id,
                )
                history_anchor_id = pinned_anchor_id
                used_anchor = True
                if context_meta is not None:
                    context_meta["prefix_anchor_id"] = pinned_anchor_id
                LOGGER.debug(
                    "[sea][prepare-context] Pinned-anchor retrieval: "
                    "%d messages from anchor %s (no anchor resolution)",
                    len(recent), pinned_anchor_id,
                )
            elif not metabolism_active:
                limit_value = _minimal_load_chars(runtime, persona, model_key)
                LOGGER.debug(
                    "[sea][prepare-context] Model has no watermarks; sliding window %d chars",
                    limit_value,
                )
            elif not preview_only:
                # Persistent anchor resolution with 3-level fallback。
                # 「自 model」は実行 model (model_key)。None なら
                # resolve 側が persona.model にフォールバックする。
                # persist_anchor_advance=False は「組成は本番と同一、
                # ただし §14-2 前進の永続化だけ行わない」— Beat ロックの
                # 外で組む keepalive 専用 (ロック外の行書き込みは並走
                # Beat の前進・fold 更新と競合する。Codex 6巡目
                # 2026-07-30)。preview_only との違いは自動想起の注入等が
                # 通常どおり走ること (温め直す prefix は本物と一致が命)。
                anchor_id, resolution = runtime.session_lifecycle.resolve_metabolism_anchor(
                    persona, model_key=model_key,
                    persist_advance=persist_anchor_advance,
                )
                history_anchor_id = anchor_id

                if anchor_id:
                    # Case 1 or 2: valid anchor found
                    recent_from_anchor = history_mgr.get_history_from_anchor(
                        anchor_id,
                        required_line_roles=required_line_roles,
                        required_scopes=required_scopes,
                        pulse_id=pulse_id,
                    )
                    # 提示コンテキストの途中で digest に畳まれた範囲は、元の時系列位置で
                    # あらすじ + 圧縮マークに置き換えて見せる
                    # (docs/intent/chronicle_eviction.md §6)。圧縮区間が無ければ素通り。
                    recent_from_anchor = runtime.session_lifecycle.apply_window_folds(
                        persona, model_key, recent_from_anchor,
                    )
                    if recent_from_anchor:
                        recent = recent_from_anchor
                        used_anchor = True
                        if context_meta is not None:
                            # call-local: 今回の prefix の anchor を呼び出し元へ返す
                            context_meta["prefix_anchor_id"] = anchor_id
                        LOGGER.debug(
                            "[sea][prepare-context] Anchor-based retrieval (%s): %d messages from anchor %s",
                            resolution, len(recent), anchor_id,
                        )
                        # Phase 4-e: anchor の updated_at touch は LLM 呼び出し成功後
                        # (`runtime_llm.py` の usage 確認位置) で行う。ここで先行 touch
                        # すると、LLM 失敗時に「実際は cache 切れてるのに TTL 内」と
                        # 誤判定して次回も長大コンテキストを送る不整合になるため。
                else:
                    # Case 3: 起点行が一つも無い (新規ペルソナ / 修復直後)
                    # のブートストラップ。残す量ぶんだけ読み、下の
                    # count-based 経路が新しい起点候補を立てる。
                    #
                    # 旧実装はここ (anchor TTL 失効時) で会話前の全量
                    # Chronicle 生成を行っていたが、arasuji_levels.md §13
                    # (2026-07-29) で撤去した — 起点失効は温度情報であって
                    # 提示範囲を変えず、編纂の自動発火は予算超過
                    # (maybe_run_metabolism) の一本。
                    limit_value = _minimal_load_chars(runtime, persona, model_key)
                    use_message_count = False
                    LOGGER.debug(
                        "[sea][prepare-context] Bootstrap minimal load (no anchor row): %d chars",
                        limit_value,
                    )

            else:
                # Preview mode: use anchor for retrieval but don't persist or generate Chronicle
                # (§14-2 機構1 の前進も永続化しない — 返る位置は本番と同じ)。
                # §15 の読み戻しも同じ型で反映する — 実際の読み戻しは次の
                # Pulse の応答前に走るため、素の窓のままだとプレビューが
                # 「話しかけた時に実際に見える窓」より薄い嘘になる。読みだけ
                # の計算 (仕上げの検算まで本番と同一・行は触らない) で組む。
                # 適用は標準の会話窓 (main_line / committed) のときだけ —
                # 読み戻しの文字勘定はその窓で定義されている。
                if (
                    required_line_roles == ["main_line"]
                    and required_scopes == ["committed"]
                    and pulse_id is None
                ):
                    refill_plan = runtime.session_lifecycle.preview_refilled_history(
                        persona, model_key,
                    )
                    if refill_plan:
                        # head のあらすじ枠 (weave) は capture 済み
                        # snapshot 由来で、読み戻し後の除外名簿を知らない
                        # — このままだと生に戻した範囲のあらすじが head
                        # に残り、プレビューだけ二重表示になる。読み戻し
                        # 後の名簿で weave を組み直して差し替える (本番は
                        # 書き込み後の再 capture が同じことをする)。
                        # 差し替えに失敗したら読み戻しプレビューごと
                        # 見送り、素の窓に落とす — 薄いが整合した表示は
                        # 二重表示より嘘が小さい (Codex 指摘 2026-07-30)。
                        if _swap_preview_weave_for_refill(
                            runtime, persona, messages, refill_plan,
                        ):
                            recent = refill_plan["presented"]
                            used_anchor = True
                if not used_anchor:
                    anchor_id, resolution = runtime.session_lifecycle.resolve_metabolism_anchor(
                        persona, model_key=model_key, persist_advance=False,
                    )
                    history_anchor_id = anchor_id
                    if anchor_id:
                        recent_from_anchor = history_mgr.get_history_from_anchor(
                            anchor_id,
                            required_line_roles=required_line_roles,
                            required_scopes=required_scopes,
                            pulse_id=pulse_id,
                        )
                        recent_from_anchor = runtime.session_lifecycle.apply_window_folds(
                            persona, model_key, recent_from_anchor,
                        )
                        if recent_from_anchor:
                            recent = recent_from_anchor
                            used_anchor = True
                if not used_anchor:
                    limit_value = _minimal_load_chars(runtime, persona, model_key)
                    use_message_count = False

        elif isinstance(history_depth, str) and history_depth.endswith("messages"):
            # Message count mode: "10messages", "20messages", etc.
            try:
                limit_value = int(history_depth[:-8])  # Remove "messages" suffix
                use_message_count = True
            except ValueError:
                limit_value = 10  # fallback for message count
                use_message_count = True
        else:
            try:
                limit_value = int(history_depth)
            except (ValueError, TypeError):
                limit_value = 2000  # fallback

        # Fetch history if not already retrieved via anchor
        if not used_anchor:
            LOGGER.debug(
                "[sea][prepare-context] Fetching history: limit=%d, mode=%s, pulse_id=%s, "
                "balanced=%s, line_roles=%s, scopes=%s",
                limit_value, "messages" if use_message_count else "chars", pulse_id,
                reqs.history_balanced, required_line_roles, required_scopes,
            )

            if use_message_count:
                # Message count mode - balanced not supported yet
                recent = history_mgr.get_recent_history_by_count(
                    limit_value,
                    required_line_roles=required_line_roles,
                    required_scopes=required_scopes,
                    pulse_id=pulse_id,
                )
            elif reqs.history_balanced:
                # Get conversation partners for balanced retrieval
                participant_ids = ["user"]
                occupants = runtime.manager.occupants.get(building_id, [])
                persona_id = getattr(persona, "persona_id", None)
                for oid in occupants:
                    if oid != persona_id:
                        participant_ids.append(oid)
                LOGGER.debug("[sea][prepare-context] Balancing across: %s", participant_ids)
                recent = history_mgr.get_recent_history_balanced(
                    limit_value,
                    participant_ids,
                    required_line_roles=required_line_roles,
                    required_scopes=required_scopes,
                    pulse_id=pulse_id,
                )
            else:
                recent = history_mgr.get_recent_history(
                    limit_value,
                    required_line_roles=required_line_roles,
                    required_scopes=required_scopes,
                    pulse_id=pulse_id,
                )

            # Set metabolism anchor on first count-based retrieval (skip in preview).
            # Phase 4-e: DB への永続化 (updated_at 書き込み) は LLM 呼び出し成功後に行う。
            # ここで anchor 候補を立てたまま LLM 失敗 → DB 未書き込み → 次回も Case 3
            # fallback → minimal load、という挙動になる。新規 anchor が永続化されない
            # こと自体は Case 3 を毎回繰り返すだけで致命的ではない (まはー確認済 2026-05-08)。
            # §3.2: 候補は persona 属性ではなく context_meta (call-local) で運ぶ。
            # 水位なし model (Metabolism を持たない) は候補も立てない —
            # LLM 成功時の touch で anchor 行が生まれてしまうため。
            if metabolism_active and recent and not preview_only:
                oldest_id = recent[0].get("id")
                if oldest_id:
                    if context_meta is not None:
                        context_meta["prefix_anchor_id"] = oldest_id
                    LOGGER.debug("[sea][prepare-context] Resolved count-based metabolism anchor %s (call-local); DB persist deferred to post-LLM-success", oldest_id)

        LOGGER.debug("[sea][prepare-context] Got %d history messages", len(recent))

        # ---- 未付記バッチの時刻順マージ (W14, perception_buffer.md §10.3) ----
        # 消費された知覚は messages に行を作らない (§10.1)。提示は生ログと
        # 未付記の消費バッチをここで時刻順にマージする。提示から下ろす
        # 唯一の手段は退場付記の印 (annexed_entry_id) — 付記されるまで
        # 消えない。legacy の event_message 行は生ログ側にそのまま居るので、
        # 混在期間も両方が自然に並ぶ。
        recent = _merge_consumed_perceptions(
            runtime, persona, recent, anchor_id=history_anchor_id,
            model_key=model_key,
        )

        # Enrich messages with attachment context
        enriched_recent = runtime._enrich_history_with_attachments(recent)

        # 時刻アンカー (v0.32, 2026-05-09): history 内の最古残存メッセージの直前に
        # 「以下、YYYY-MM-DD HH:MM:SS 以降のやり取りです」を揮発挿入する。
        # メッセージそのものに時刻メタを付ける副作用 (ペルソナが時刻を真似する) を
        # 避けるため、Metabolism 起点に 1 か所だけ。
        # (v0.32 には上部補完メッセージ用の「アンカー①」が別に居たが、
        #  補完機構ごと退役した — track_retirement.md 住人 11)
        if enriched_recent:
            oldest_msg = enriched_recent[0]
            oldest_ts = oldest_msg.get("created_at") or oldest_msg.get("timestamp")
            if oldest_ts:
                try:
                    from datetime import datetime as _dt
                    ts_int = int(oldest_ts)
                    ts_str = _dt.fromtimestamp(ts_int).strftime("%Y-%m-%d %H:%M:%S")
                    anchor_msg = {
                        "role": "user",
                        "content": f"<system>以下、{ts_str} 以降のやり取りです</system>",
                        # 記録に残らないが、同じ履歴からは毎ターン同じ
                        # 文面で組み直される (reread_recorded_history も同じ
                        # この関数で組み直す)。
                        "metadata": {HISTORY_TIME_ANCHOR_KEY: True},
                    }
                    messages.append(anchor_msg)
                    LOGGER.debug(
                        "[sea][prepare-context] Inserted timestamp anchor: %s",
                        ts_str,
                    )
                except (TypeError, ValueError, OSError):
                    LOGGER.debug(
                        "[sea][prepare-context] Skipping timestamp anchor (invalid ts=%r)",
                        oldest_ts,
                    )

        enriched_recent = _reframe_autonomous_messages(enriched_recent)
        # 継続キャッシュ (高速応答モード) の境目: ユーザーの発言への返事の
        # Pulse では、履歴の中の最後の AI の返事に内部印を付ける
        # (docs/intent/cache_lifecycle_control.md §3.5)。
        if pulse_type == "user" and not preview_only:
            enriched_recent = mark_cache_boundary(enriched_recent, pulse_id)
        messages.extend(enriched_recent)

        # 実入力の履歴 ID 列 (2026-08-19, sluice の見た集合の一次情報):
        # この呼び出しが**実際にプロンプトへ組み込んだ**履歴メッセージの
        # ID を out-param で呼び出し元へ返す (戻り値と既存キーは不変 —
        # prefix_anchor_id と同じ call-local の器)。履歴構築が失敗した場合は
        # このキー自体が書かれない — 読み手 (sea/sluice.py) はキー不在を
        # fail-closed (退場停止) に写像する。
        if context_meta is not None:
            presented_ids: List[str] = []
            for _msg in enriched_recent:
                _mid = _msg.get("id") if isinstance(_msg, dict) else None
                if _mid:
                    presented_ids.append(str(_mid))
            context_meta["presented_message_ids"] = presented_ids
        return True
    except PinnedAnchorUnavailableError:
        # 凍結起点の失敗は握らない — ここで warning に丸めると、呼び出し
        # 元 (sluice) は「presented_message_ids 不在」という遠い顔の
        # 失敗になる。実名で送出して退場停止に乗せる。
        raise
    except Exception as exc:
        LOGGER.exception("[sea][prepare-context] Failed to get history: %s", exc)
        return False


def prepare_context(runtime, persona: Any, building_id: str, user_input: Optional[str], requirements: Optional[Any] = None, pulse_id: Optional[str] = None, warnings: Optional[List[Dict[str, Any]]] = None, preview_only: bool = False, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None, cancellation_token: Optional[Any] = None, pulse_type: Optional[str] = None, model_key: Optional[str] = None, context_meta: Optional[Dict[str, Any]] = None, persona_voiced: bool = False, persist_anchor_advance: bool = True, pinned_anchor_id: Optional[str] = None) -> List[Dict[str, Any]]:
    # model_key: この context を届ける Session (persona, model) の実行 model
    # (beat_execution_context.md §3.1 — head は (persona, model) に一つ)。
    # ExecutionContext が届いている呼び出し元 (work_session / sluice /
    # keepalive / run_playbook の Pulse-root) が execution_context.model_key を
    # 渡す。None なら persona の標準 model にフォールバック (preview 等)。
    #
    # context_meta: 呼び出し元が渡す out-param dict (§3.2 の call-local anchor)。
    # この呼び出しで prefix に採用した anchor の ID を
    # ``context_meta["prefix_anchor_id"]`` に書き戻す (anchor を使わない組成では
    # 書かない)。履歴組成が成功したときは、実際にプロンプトへ組み込んだ履歴
    # メッセージの ID 列を ``context_meta["presented_message_ids"]`` に書き戻す
    # (sluice の「見た集合」の一次情報 — 履歴構築失敗時はキー不在)。呼び出し元は state["_prefix_anchor_id"] に載せ、LLM 成功後の
    # ``touch_anchor_after_llm_call(anchor_id=...)`` に渡す。旧実装の
    # ``history_manager.metabolism_anchor_message_id`` (persona 単一可変属性) は
    # 廃止した — TTL 失効後に旧 anchor を touch する事故 (記憶監査第 4 片) の根治。
    from sea.playbook_models import ContextRequirements

    # 「指定なし」の意味はここ一箇所で決まる (フィールド既定)。
    reqs = requirements if requirements else ContextRequirements()

    # 印 (persona_voiced) の関所: ペルソナ本人の発話・思考として記録される呼び出しは、
    # 会話履歴を外して走らせない。詳細は PersonaVoiceWithoutHistoryError。
    if persona_voiced and history_spec_is_empty(reqs.history_depth):
        raise PersonaVoiceWithoutHistoryError(
            f"persona_voiced=True の呼び出しで history_depth={reqs.history_depth!r} が"
            " 指定されました。ペルソナ名義の稼働から会話履歴を外すことはできません"
            " (人格を必要としない処理なら persona_voiced=False で呼んでください)。"
            f" persona={getattr(persona, 'persona_id', None)!r}"
        )

    # 起点の凍結 (pinned_anchor_id) は anchor 起点の提示組成 (history_depth
    # "full") でだけ意味を持つ。別の depth や履歴の無い persona で黙って
    # 無視すると、凍結したつもりの呼び出し元 (sluice) が通常解決の窓で走る —
    # ここで落とす (fail-closed)。
    if pinned_anchor_id is not None and (
        reqs.history_depth != "full"
        or getattr(persona, "history_manager", None) is None
    ):
        raise PinnedAnchorUnavailableError(
            f"pinned_anchor_id={pinned_anchor_id!r} requires history_depth='full' "
            f"and a history_manager (got depth={reqs.history_depth!r}, "
            f"persona={getattr(persona, 'persona_id', None)!r})"
        )

    messages: List[Dict[str, Any]] = []

    # ---- head: system prompt + Memory Weave + 自分の外見とインベントリ (self_view) ----
    # Cached Head Architecture (Phase 2-h) で section pipeline 経由に統一済み。
    # 旧 live state 直読み経路 (= section 群を毎回ここで組み立てる) は廃止。
    # snapshot 不在時は ensure_snapshot 経由で初回 capture が自動で走る。
    # 詳細: docs/intent/cached_head_architecture.md
    # head の章立ては呼び出し側から選べない (PERSONA_HEAD_SECTIONS の docstring)。
    enabled_sections: set[str] = set(PERSONA_HEAD_SECTIONS)

    if enabled_sections:
        from sea.head_pipeline import render_head_messages
        from sea.head_pipeline.types import HeadNotReadyError
        try:
            head_messages = render_head_messages(
                persona, runtime.manager, building_id,
                enabled_sections=enabled_sections,
                model_key=model_key,
            )
            if head_messages:
                messages.extend(head_messages)
                LOGGER.debug(
                    "[sea][prepare-context] Added %d head messages via pipeline",
                    len(head_messages),
                )
        except HeadNotReadyError:
            # fail-closed (W6 / SEA 監査 S6): required Section (人格) を欠いた
            # head で LLM を走らせない。そのまま伝播して Pulse を中断する —
            # 会話は呼び出し元へエラー、判断点/コマは実行台帳の failed 行になり
            # 再試行される。復旧後は次 Pulse の ensure_snapshot が自己修復する。
            raise
        except Exception as exc:
            # head pipeline 全体が死んだ場合も fail closed (旧実装は exception log
            # だけで LLM に進み、人格なしの応答を本人履歴へ確定し得た)。
            # head は常に人格を含む (PERSONA_HEAD_SECTIONS) ので degrade 経路は無い。
            raise HeadNotReadyError(
                getattr(persona, "persona_id", "") or "",
                str(model_key or ""),
                "pipeline",
                {"__pipeline__": f"head rendering failed: {exc!r}"},
            ) from exc

    # ---- history ----
    # 組み立ては _append_history (ターンの最初の送信の前に記録から読み直す
    # reread_recorded_history と同じ一枚)。
    history_start = len(messages)
    history_built = _append_history(
        runtime, persona, building_id, reqs, messages,
        pulse_id=pulse_id, preview_only=preview_only, model_key=model_key,
        context_meta=context_meta, persist_anchor_advance=persist_anchor_advance,
        pinned_anchor_id=pinned_anchor_id, pulse_type=pulse_type,
    )
    history_region = list(messages[history_start:])

    # ---- Recalled Memory Context — 廃止済み ----
    # recalled_ids はシステムプロンプトへの注入から廃止。
    # recall_entry / recall_navigate ツールが想起時に直接会話履歴へ内容を追記する。

    # ---- リアルタイム情報と自動想起を残すモデルか (モデルごとの設定) ----
    # docs/intent/persist_realtime_and_recall.md。ON のモデルへ送るときは、
    # そのターンのリアルタイム情報と自動想起を一つのまとまりにして送る列の
    # 一番後ろに置き (下)、生成が正常に完了した後に送った文面のまま
    # 残す (sea/runtime_llm._persist_turn_realtime_and_recall)。OFF のモデルは
    # 今までどおり (最新の発言の直前に置き、次のターンには消える)。
    # 判定は組むときのモデルで行い、呼び出し元へ返す (リアルタイムスペルが
    # まとまりを自前で作る経路が同じ判定に従うため — runtime_runner が state
    # に載せる)。
    persist_mode = persists_realtime_and_recall(persona, model_key)
    if context_meta is not None:
        context_meta["persist_realtime_and_recall"] = persist_mode
        # ターンの最初の LLM 呼び出しの直前に、送る列の「最後の記録済みの発言
        # (境界) より後ろ」を記録から読み直して揃えるための材料
        # (persist_realtime_and_recall.md §5)。最初の送信の前に記録に書かれる
        # もの (事前スペル・TOOL の結果・ツールの中の記録・MEMORIZE など) を
        # 差し込み口ごとに送る列へ同期する代わりに、記録そのものから並べ直す。
        # 読み直しはこの組み立てと同じ _append_history を、同じ建物・指定・
        # モデルで通す。使うのは run_playbook が残すパルスの state に移した
        # ときだけ (sea/runtime_llm._align_first_send_with_record)。
        context_meta.pop(FIRST_SEND_RECORD_SOURCE_META_KEY, None)
        if persist_mode and history_built and not preview_only:
            boundary_id = next(
                (
                    str(m["id"]) for m in reversed(history_region)
                    if isinstance(m, dict) and m.get("id")
                    and not is_injected_perception(m)
                ),
                None,
            )
            context_meta[FIRST_SEND_RECORD_SOURCE_META_KEY] = FirstSendRecordSource(
                reread=functools.partial(
                    reread_recorded_history, runtime, persona, building_id,
                    reqs, model_key,
                ),
                boundary_id=boundary_id,
                history_len=len(history_region),
            )
    persist_realtime_msg: Optional[Dict[str, Any]] = None

    # ---- Realtime Context ----
    # Time-sensitive info placed just BEFORE the last user message to improve LLM caching.
    # This ensures LLM responds to user input, not the realtime context.
    # (残すモデルでは差し込まずに取っておき、下で自動想起とまとめて一番後ろに置く。)
    if reqs.realtime_context:
        try:
            realtime_msg = runtime._build_realtime_context(persona, building_id, messages)
            if realtime_msg and persist_mode:
                persist_realtime_msg = realtime_msg
            elif realtime_msg:
                # Find the last user message and insert realtime context before it
                last_user_idx = None
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "user" and not messages[i].get("metadata", {}).get("__realtime_context__"):
                        last_user_idx = i
                        break

                if last_user_idx is not None:
                    # Insert before last user message
                    messages.insert(last_user_idx, realtime_msg)
                    LOGGER.debug("[sea][prepare-context] Added realtime context before last user message (idx=%d)", last_user_idx)
                else:
                    # No user message found, append at end
                    messages.append(realtime_msg)
                    LOGGER.debug("[sea][prepare-context] Added realtime context at end (no user message found)")
        except Exception as exc:
            LOGGER.debug("[sea][prepare-context] Failed to build realtime context: %s", exc)

    # ---- 自動想起 第0層 (ゾーン C) — 「浮かんだ記憶」の末尾注入 ----
    # 記憶アーキv2 §4。CONVERSATION アスペクト (user/schedule Pulse) のときのみ、
    # ローカル埋め込み検索で現在の話題に関連する記憶を末尾に一時注入する。
    # head 非混入・SAIMemory 非永続 (§10-2/§10-7)。LLM は呼ばない (§10-1。唯一の
    # 例外は既定 OFF の Jev 選別層 — ON のときだけ判定専用の外部 API へ 1 往復する。
    # docs/intent/auto_recall_jev_rerank.md)。
    # サブライン (line='sub') はそもそも _prepare_context を通らないので自然に除外。
    recall_result = None
    if not preview_only:
        try:
            recall_result = _maybe_inject_auto_recall(
                runtime, persona, messages,
                pulse_type=pulse_type,
                event_callback=event_callback,
                persist_mode=persist_mode,
            )
        except Exception:
            LOGGER.exception("[sea][prepare-context] auto_recall injection failed (non-fatal)")

    # ---- 残すモデル: そのターンのリアルタイム情報と自動想起を一番後ろへ ----
    # 位置は最新の発言と、そのターンの知覚のまとめの後ろ。次のターンにこの
    # まとまりが出る位置 (= 残した時点で最後にあった発言とそのターンの知覚の
    # 直後) と一致させるため (persist_realtime_and_recall.md §4.1)。出すものが
    # 何も無いターンはまとまり自体を作らない。
    if persist_mode:
        recall_injected = bool(
            recall_result is not None and recall_result.injected and recall_result.block
        )
        turn_msg = build_turn_realtime_and_recall_message(
            persist_realtime_msg,
            recall_result.block if recall_injected else None,
            recall_result.recalled_keys if recall_injected else None,
        )
        if turn_msg is not None:
            messages.append(turn_msg)
            LOGGER.debug(
                "[sea][prepare-context] appended this turn's realtime info / "
                "auto recall at the end (persist_realtime_and_recall, %d chars)",
                len(turn_msg["content"]),
            )
        # 残すモデルの列では、画像の枚数の上限を使わない (§4.2)。上限があると、
        # 新しい画像 (ユーザー・知覚・ツール・スペルの区別なく) が来るたびに一番
        # 古い画像が文字に置き換わり、前回の送信の中身が書き換わる — 「後ろに
        # 足すだけ」の約束と根本でぶつかる。印は列に一つ (先頭のメッセージ) で、
        # 各クライアントと送る量の見積もりが llm_clients.utils.
        # attachment_limit_is_off で読む。組んだときのモデルの判定に従う (§3) —
        # 子が写す列 (line='sub') にも、スペルの繰り返しで後ろに積む列にも残る。
        mark_attachment_limit_off(messages)

    # ---- Token budget check ----
    return messages


def _payload_epoch(msg: Dict[str, Any]) -> Optional[int]:
    """提示メッセージの時刻を epoch 秒で読む。

    adapter 経由の payload は ``created_at`` (epoch int) を持つ。それが無い行
    (テストのスタブ等) は ``timestamp`` (ISO 文字列) を tz-aware に解釈して
    フォールバックする。どちらも無ければ None (= 比較不能)。
    """
    created = msg.get("created_at")
    if created is not None:
        try:
            return int(created)
        except (TypeError, ValueError):
            pass
    ts = msg.get("timestamp")
    if ts:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(ts))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except (TypeError, ValueError):
            pass
    return None


def _chronicle_enabled_for(runtime: Any, persona: Any) -> bool:
    """Chronicle 編纂が有効か — generate_chronicle の入口と同じフラグを読む。

    _run_metabolism_locked が編纂可否に使う門 (ペルソナ単位トグル
    AI.CHRONICLE_ENABLED) をそのまま写す。判定できないときは True (= バッチを
    隠さない側) に倒す — 「編纂なしで忘れる」は明示的な選択のときだけ。

    かつては env ENABLE_MEMORY_WEAVE_CONTEXT との二段だったが、2026-09-01 に
    撤去した (v0.2 からのアップグレード組で記憶の整理が全停止する実害)。
    """
    try:
        lifecycle = getattr(runtime, "session_lifecycle", None)
        if lifecycle is None:
            return True
        return bool(lifecycle.is_chronicle_enabled_for_persona(persona))
    except Exception:
        return True


def _perception_block_text(rendered_text: str) -> str:
    """バッチの確定文面を提示ブロックの本文にする (組成の一点)。

    勘定 (:func:`sea.eviction_plan.message_chars`) が数えるのはこの文字列なので、
    合計上限の判定も同じ関数で測る — 「送る側と測る側が同じ一枚を見る」規則を
    知覚の上限にも通す。
    """
    return f"<system>{rendered_text}</system>"


#: 下ろした跡地に置く機構名義の一行の見出しは台帳側の一枚
#: (:data:`sai_memory.perception_buffer.PERCEPTION_OMISSION_HEADER`) —
#: 既存の知覚ブロックの見出し (``[システム通知]`` / ``[フィード]`` =
#: ``_KIND_HEADERS``) と同じ流儀で、提示の節約 (sai_memory/presented_reduction)
#: も同じ見出しで跡地を書く。

def _perception_omission_block(
    dropped: Sequence[Any], created_at: int, *, chronicle_enabled: bool,
    records: int,
) -> Dict[str, Any]:
    """下ろした区間の跡地に置く機構名義のブロック (連続区間を一つに束ねる)。

    黙って消さない — 「そこに何も無かった」という記録の嘘をつかないため
    (2026-09-04 まはー裁定 1)。あらすじの畳みが跡地に digest を置くのと同じ
    原則で、文面の役目も同じ二つ: ①ここに記録があった ②消えたのではない。

    ``records`` は下ろした区間に**実際に入っていた記録の件数**
    (:func:`~sai_memory.perception_buffer.count_batch_records` の合計) で、
    バッチ数ではない — 1 枚のバッチは Beat 頭に溜まっていた知覚を全部束ねるので、
    バッチ数で書くと「部屋の様子 3 件 + 通知 5 件」の 1 枚が「1 件」になる。
    台帳の行を引けなかったバッチは 1 と数えるので合計は**下限**であり、文面も
    「N 件以上」と下限で書く (数えられた分より少なく言うことはない)。

    Chronicle 無効のペルソナには②を書かない — 編纂が走らない設定なので、
    あらすじで読み返せるという約束が嘘になる。有効なペルソナにも「もう入って
    いる」とは書かない: この区間の編纂はまだ走っていないことがあり、断定すると
    未成立の約束になる (2026-09-05 四巡目 #3)。
    """
    body = (
        f"ここにあった {records} 件以上の記録 (部屋の様子・通知など) は、"
        "古くなったため表示から外しました。"
    )
    if chronicle_enabled:
        body += "消えたのではなく、記憶の整理の際にあらすじへ引き継がれます。"
    return {
        "role": "user",
        "content": (
            f"<system>{PERCEPTION_OMISSION_HEADER}\n{body}</system>"
        ),
        "created_at": int(created_at),
        "metadata": {
            "tags": ["internal", "event_message", "perception"],
            CONSUMED_PERCEPTION_KEY: True,
            "__perception_omitted__": int(records),
            "__perception_omitted_batches__": len(dropped),
        },
    }


def list_presented_perception_blocks(
    runtime: Any, persona: Any, recent: Sequence[Dict[str, Any]],
    *,
    anchor_id: Optional[str] = None,
    raise_on_error: bool = False,
    model_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """いま提示に差し込まれる知覚ブロックを組む (組成規則の一点管理)。

    「どのバッチをどんな文面で出すか」はここだけに書く。送る側
    (:func:`_merge_consumed_perceptions`) と**測る側**
    (sea/session_lifecycle.py の文字数勘定・退場計画) が同じ関数を呼ぶ —
    勘定が実送信より小さい数字を出していた欠陥
    (docs/issues/context_accounting_excludes_injected_rows.md) の再発は、
    組成規則の二枚目を作らないことでしか防げない。

    - バッチの ``rendered_text`` (消費時に確定したペルソナが見た文面) を
      ``<system>`` 包みでそのまま出す — 生の台帳項目からの再構成 (再 reduce /
      再 format) はしない (reduce で消えた中間状態の復活・秒精度の時刻衝突に
      よるグループ混線の根)。
    - **Metabolism が決めた縮み**は候補を取った直後に一度だけ適用する
      (:func:`sai_memory.presented_reduction.reduce_presented_batches`、
      docs/intent/presented_context_reduction.md 設計 1/2): 用の済んだ操作通知
      (head が描き直された後は重複) を下ろし、現在地でない部屋の様子を一行へ
      縮めて画像を外す。判断そのものは Metabolism の瞬間に確定済みで、ここでは
      読むだけ — だから提示は Metabolism 以外の瞬間に変わらない。跡地には
      どちらも機構名義の一行が出る (黙って消さない)。**操作通知を下ろした境界は
      ``model_key`` ごと** (head と同じ単位。部屋の縮めた印はペルソナ共通) なので、
      ``model_key`` はここで実の用途を持つ — 引数が無ければ head 側と同じ解決
      (:func:`sea.head_pipeline.integration.resolve_default_model_key`) で
      persona の標準 model に落とす。
    - **付記の印** (``annexed_entry_id``) が付いたバッチは提示から下りる — 付記
      されるまで消えないので、下限「退場したものは必ず編纂されている」が提示側
      でも常に成立する。履歴が空でも未付記バッチは提示される。
    - **下ろした境界より古いバッチは提示に出さない** (``perception_presentation``
      の一方向境界)。跡地には省略の印 (機構名義の一行) を置く — 黙って消さない。
      境界を進める引き金だった知覚の合計上限 (旧 §10.9 の二水位) は 2026-09-09 に
      廃止した (docs/intent/presented_context_reduction.md 設計 3) ので、この関数は
      境界を**読むだけ**で、書き込みは一切しない。既に進んでいる境界 (廃止前に
      下ろされた区間) はそのまま尊重する — 一度下ろしたものを戻すと、揺り戻しで
      プロンプトキャッシュの前方一致が割れる。
    - 例外は Chronicle 無効のペルソナ (「編纂なしで忘れる」を選んだ) のみ:
      提示窓 (anchor 境界、無ければ提示最古行) より古いバッチは会話と
      同じように忘れる。**履歴が空でも** anchor が立っていれば絞る — 生ログ窓が
      空の瞬間に過去バッチを全部再提示しない (2026-08-19 Codex 第二巡 #3)。
      anchor も履歴も無い完全ブートストラップだけは全提示 (隠さない側)。
    - **窓絞りで底が抜けた「部屋の様子」の差分は、提示時に全文へ開き直す**
      (:func:`sai_memory.room_state.reopen_lost_bases`)。Chronicle 無効の窓絞り
      は台帳に書ける事実ではない (窓はペルソナと model ごとに動く) ので、台帳側
      の回復 (付記・境界前進と同一 tx) はここまで届かない — トグルを有効から
      無効へ切り替えると、有効な間に積んだ差分が土台なしで提示に残る
      (2026-09-05 四巡目 #1)。開き直しは純関数で、同じ並びからは必ず同じ文面に
      なるので、提示が呼び出しごとに揺れることはない。台帳も確定文面も触らない。
      開き直した全文には束のメディアも添える (「絵がある」と読める全文に絵が
      付く — room_state_packages.md §5)。
    - **残したリアルタイム情報と自動想起の行** (同じ表の ``kind =
      'realtime_and_recall'`` — docs/intent/persist_realtime_and_recall.md
      §4.3) は、``model_key`` のモデルが残す設定のときだけ、位置を決める発言が
      ``recent`` にあるものだけを出す。文面は残したまま (縮み・開き直しを
      当てない)、並びは :func:`merge_perception_blocks` が発言の直後に決める。
      付記で下りるのは知覚のまとめと同じ。残す設定のモデルへ送る列では、
      知覚のまとめにも位置を決める発言を付ける
      (:func:`_resolve_perception_anchors_locked` — 同じ秒の前後が入れ替わら
      ないように)。境界で位置を決められない知覚のまとめ (と省略の印) には
      :data:`PLACE_BEFORE_SAME_SECOND_KEY` を付け、同じ秒の発言より前に置く。
      OFF のモデルの知覚は今までどおり秒の順。
    - **この関数は台帳へ一切書かない** (2026-09-09 以降)。読み取り専用の画面
      (context-status)・仮定の窓の下見 (読み戻し / 引き戻しのプレビュー)・
      コンテキストプレビューと、実際に送る側が同じ一枚を通る — 測る列と送る列が
      同じ内容になる規則は、書き込みが無くなったことで構造的に成り立つ
      (以前は ``advance_cutoff`` で書き込みだけを止め、進めた「つもり」の列を
      合成していた)。
    - 失敗は空リスト + WARN に倒す — 送る側は「マージなし (元の履歴のまま)」、
      測る側は「知覚ぶん 0 (従来値)」へ縮退する。履歴ゼロで走らせるよりも
      知覚欠けの方が被害が小さい。``raise_on_error=True`` は失敗を例外で
      伝える — 透明性の画面 (context-status) が内部失敗を正常なゼロとして
      表示しないための口 (preview_refilled_history の raise_on_error と同じ型。
      Codex 指摘 2026-09-02)。門・発火・送信の各経路は従来どおり fail-open。
    """
    sai_mem = getattr(persona, "sai_memory", None)
    if sai_mem is None or not getattr(sai_mem, "is_ready", lambda: False)():
        return []
    try:
        from sai_memory.perception_buffer import (
            count_batch_records,
            get_presentation_cutoff,
            list_unannexed_batches,
        )
        from sai_memory.perception_buffer import batch_in_window, resolve_window_key
        from sai_memory.presented_reduction import reduce_presented_batches
        from sai_memory.room_state import batch_is_room_reseat, reopen_lost_bases
        from sea.head_pipeline.integration import resolve_default_model_key
        chronicle_enabled = _chronicle_enabled_for(runtime, persona)
        # 残したリアルタイム情報と自動想起の行を送るのは、送り先のモデルが
        # 残す設定のときだけ (persist_realtime_and_recall.md §4.3)。
        persist_mode = persists_realtime_and_recall(persona, model_key)
        # 残した行の id → 位置を決める発言の id (この列にあるものだけ)。
        realtime_and_recall_anchors: Dict[int, str] = {}
        # 操作通知を下ろした境界は (persona, model) ごと — head と同じ単位で
        # 持つ (docs/intent/presented_context_reduction.md 設計 1)。だから
        # 同定キーは head と**同じ解決**を通す: 引数が無い呼び出し (プレビュー
        # 等) は head 側と同じく persona の標準 model へ落ちるので、測る列と
        # 送る列が別の境界を見ることはない。
        head_model_key = resolve_default_model_key(persona) if not model_key \
            else str(model_key)

        def _window_predicate_locked() -> Optional[Callable[[Any], bool]]:
            """このペルソナの提示窓の篩 (None = 窓なし・全部見える)。

            起点キーの解決 (anchor 行 → 引けなければ recent の最古 epoch の床)
            は :func:`sai_memory.perception_buffer.resolve_window_key` の一枚 —
            検知の読みと自己回復 (adapter.latest_room_snapshot /
            reseat_room_state) と同じ関数を通る。包含も :func:`batch_in_window` の一枚なので、床への
            フォールバック時も境界キー優先で判定される — 置き直しバッチ
            (consumed_at は最古端・境界キーは最新の message) が床の窓でも
            見える側に立つのはこの規則による (consumed_at の素比較だと、置き
            直した先から床の外に落ちて提示と検知が逆向きに割れる)。

            **呼び出し側が ``sai_mem._db_lock`` を保持している前提** (錠前を
            取る層は :func:`list_presented_perception_blocks` の一枚だけ)。
            """
            if chronicle_enabled:
                return None
            oldest: Optional[int] = None
            for msg in recent:
                epoch = _payload_epoch(msg)
                if epoch is not None:
                    oldest = epoch if oldest is None else min(oldest, epoch)
            window_key = resolve_window_key(
                sai_mem.conn, anchor_id, floor_epoch=oldest,
            )
            if window_key is None:
                return None
            return lambda b: batch_in_window(b, window_key)

        def _visible_candidates_locked() -> List[Any]:
            """このペルソナに見える余地のある未付記バッチ (窓絞りまで)。

            下ろした境界より古いものもここには入る — 省略の印の件数を数える
            のに要るため。境界での振り分けは呼び出し側。

            **Metabolism が決めた縮み**
            (:func:`sai_memory.presented_reduction.reduce_presented_batches` —
            用の済んだ操作通知を下ろし、現在地でない部屋の様子を一行へ縮める)
            はここで一度だけ適用する。以降の省略の印・ブロックの組み立ては
            縮んだ写しの上で動くので、**測る側と送る側が同じ一枚を見る**
            (組成規則の二枚目を作らない)。確定文面
            (``perception_batches.rendered_text``) も台帳も書き換えない —
            変わるのはこの写しだけ。

            **呼び出し側が ``sai_mem._db_lock`` を保持している前提** (錠前を
            取る層は下の一枚だけ)。
            """
            found = list_unannexed_batches(sai_mem.conn)
            if not found:
                return found
            found = _select_realtime_and_recall_locked(found)
            if not found:
                return found
            predicate = _window_predicate_locked()
            if predicate is not None:
                found = [b for b in found if predicate(b)]
            # 縮み (操作通知・部屋の様子) は知覚のまとめにだけ当てる。残した
            # リアルタイム情報と自動想起の行は送った文面のまま出す — 一字でも
            # 変わると前回の送信と先頭が一致しなくなる。
            perceptions = [b for b in found if not b.is_realtime_and_recall]
            try:
                reduced = reduce_presented_batches(
                    sai_mem.conn, perceptions, head_model_key,
                )
            except Exception:
                # 縮みの適用の失敗は「縮めない」へ倒す。ここで送出すると外側の
                # 受け (perception batch listing failed) が知覚を丸ごと空にする —
                # 節約の失敗が知覚の喪失に化ける向きは作らない
                # (ローカルレビュー指摘 2026-09-10)。
                LOGGER.warning(
                    "[sea][prepare-context] presentation reduction failed; "
                    "presenting the batches at full size this round",
                    exc_info=True,
                )
                reduced = perceptions
            reduced_iter = iter(reduced)
            return [
                b if b.is_realtime_and_recall else next(reduced_iter)
                for b in found
            ]

        def _select_realtime_and_recall_locked(found: List[Any]) -> List[Any]:
            """残した行のうち、この列で送るものだけを残す (知覚のまとめは素通し)。

            - 送り先のモデルが残す設定でなければ、残した行は一つも送らない。
            - 位置を決める発言 (``boundary_created_at`` / ``boundary_rowid``) が
              この列 (``recent``) に無ければ送らない — 別のスレッドで残した行、
              窓の外の行を出さない (persist_realtime_and_recall.md §4.3)。
              発言の id はここで台帳から引き、ブロックに載せて並べる側
              (:func:`merge_perception_blocks`) へ渡す。
            - 位置を決める発言を持たない (境界が NULL の) 行も送らない。境界の
              無いまとまりはそもそも残さない (§4.2 — adapter.
              record_realtime_and_recall) ので、ここに来るのはそれより前に
              書かれた行だけ。

            **呼び出し側が ``sai_mem._db_lock`` を保持している前提**。
            """
            if not any(b.is_realtime_and_recall for b in found):
                return found
            if not persist_mode:
                return [b for b in found if not b.is_realtime_and_recall]
            recent_ids = {
                str(m.get("id")) for m in recent
                if isinstance(m, dict) and m.get("id")
            }
            kept: List[Any] = []
            for b in found:
                if not b.is_realtime_and_recall:
                    kept.append(b)
                    continue
                if b.boundary_created_at is None or b.boundary_rowid is None:
                    continue
                row = sai_mem.conn.execute(
                    "SELECT id FROM messages WHERE rowid = ? AND created_at = ?",
                    (int(b.boundary_rowid), int(b.boundary_created_at)),
                ).fetchone()
                if row is None or str(row[0]) not in recent_ids:
                    continue
                realtime_and_recall_anchors[int(b.id)] = str(row[0])
                kept.append(b)
            return kept

        # 候補・境界・省略件数の数え上げは**一つのロック区間**で完結させる
        # (2026-09-05 Codex 第二巡 high)。ここを二区間に割ると、別スレッドの
        # 付記 (編纂) が隙間で境界を進めて全文を移管し、こちらは古い文面のまま
        # 新しい境界で振り分ける — 土台 (全文) だけが提示から下り、土台のない
        # 差分が送られる。区間を出たあとは、ここで確定した同一世代の
        # candidates と dropped_through だけから組成する。
        with sai_mem._db_lock:
            candidates = _visible_candidates_locked()
            if not candidates:
                return []
            dropped_through = get_presentation_cutoff(sai_mem.conn)
            presented = [b for b in candidates if b.id > dropped_through]
            dropped = [b for b in candidates if b.id <= dropped_through]
            # 省略の印に出す件数は**記録の数**であってバッチ数ではない。台帳を
            # 読むので、候補・境界と同じロック区間・同じ世代で数える。
            record_counts = (
                count_batch_records(sai_mem.conn, [b.id for b in dropped])
                if dropped else {}
            )
            # 残す設定のモデルへ送る列では、知覚のまとめも境界の発言で位置を
            # 決める (persist_realtime_and_recall.md §4.3)。秒の比較だと、発言・
            # 知覚・返事・次の発言が同じ秒に並んだとき、知覚が返事の後ろへ
            # 移って前回の送信と先頭が一致しなくなる。OFF のモデルは今までどおり
            # 秒の順。
            perception_anchors = (
                _resolve_perception_anchors_locked(sai_mem.conn, presented, recent)
                if persist_mode else {}
            )

        # 窓絞り (Chronicle 無効) の境界は台帳へ書けないので、そこで土台が抜けた
        # 差分はここで全文へ開き直す (純関数・決定論)。台帳側の回復 (付記と同一
        # tx の restore_room_state_bases) が済んでいる回は空。
        # 値は (開き直し後の文面, 添え直すメディア)。残した行は部屋の様子を
        # 持たないので渡さない。
        reopened = reopen_lost_bases(
            [b for b in presented if not b.is_realtime_and_recall]
        )

        blocks: List[Dict[str, Any]] = []
        if dropped:
            # 印の位置は「下ろした区間の末尾」= 最後に下ろしたバッチの時刻。
            # 提示に残る最古のブロックを追い越さないよう抑える (マージは
            # 時刻昇順の並びを前提にする)。
            mark_at = max(b.consumed_at for b in dropped)
            if presented:
                mark_at = min(mark_at, presented[0].consumed_at)
            omission = _perception_omission_block(
                dropped, mark_at, chronicle_enabled=chronicle_enabled,
                records=sum(record_counts.get(b.id, 1) for b in dropped),
            )
            if persist_mode:
                # 印は提示に残る最古の知覚のまとめと同じ秒に立ちうる。まとめ側だけ
                # 「同じ秒の発言より前」に並べると、印の後ろで秒の比較が止まって
                # まとめが印ごと発言の後ろへ押し出される — 印も同じ規則で並べる。
                omission["metadata"][PLACE_BEFORE_SAME_SECOND_KEY] = True
            blocks.append(omission)
        for batch in presented:
            if batch.is_realtime_and_recall:
                block = _realtime_and_recall_block(
                    batch, realtime_and_recall_anchors.get(int(batch.id)),
                )
                if block is not None:
                    blocks.append(block)
                continue
            metadata: Dict[str, Any] = {
                # 旧 flush の event_message 行と同型のタグ + マージ由来の目印。
                "tags": ["internal", "event_message", "perception"],
                CONSUMED_PERCEPTION_KEY: True,
            }
            if batch.id is not None:
                # 幻のブロック (測るだけの回の置き直しの下見) は実体化前で
                # id を持たない — キーごと載せない。読者は .get() で読む
                # (書き手はここ一枚、他の読み手は本目印を前提にしていない)。
                metadata["__perception_batch_id__"] = batch.id
                anchor = perception_anchors.get(int(batch.id))
                if anchor:
                    metadata[BOUNDARY_ANCHOR_ID_KEY] = anchor
                if persist_mode and not batch_is_room_reseat(batch.room_state_json):
                    # 境界で位置を決められないとき (境界が NULL・境界より前の
                    # 発言が送る列に無い・発言が組成と別の列に無い) は、同じ秒の
                    # 発言より前に置く (§4.3、Codex 九巡目)。秒の厳密な比較だと、
                    # 同じ秒の発言が増えるたびに後ろへ動いて前回の送信と先頭が
                    # 一致しなくなる。部屋の様子の置き直しは最古にした時刻で
                    # 先頭に立つ規則のまま (印を付けない)。
                    metadata[PLACE_BEFORE_SAME_SECOND_KEY] = True
            if batch.room_state_json or batch.id is None:
                # 部屋の記帳 (room_state_json) を持つバッチは「部屋の様子」。
                # 幻のブロック (id なし) は置き直しの下見そのもの — 実 INSERT
                # されれば記帳つきの置き直しバッチになるので、印も実物と同一に
                # する (測る列と送る列の同一性はバッジまで含めて保つ)。プレビュー
                # のバッジ用の印で、content には触れない (提示の描画が変わると
                # キャッシュの前方一致が割れる)。metadata は送信時に message
                # preparer が落とすので LLM へは渡らない (2026-09-06 バッジ裁定)。
                metadata["__room_state__"] = True
            media = batch.media_list()
            content_text = batch.rendered_text
            if batch.id in reopened:
                content_text, extra_media = reopened[batch.id]
                # 開き直しで全文に戻したパッケージのメディアを添え直す
                # (path で重複排除 — 土台と一緒に付いていた絵は二重にしない)。
                seen_paths = {
                    m.get("path") for m in media if isinstance(m, dict)
                }
                for m in extra_media:
                    if m.get("path") in seen_paths:
                        continue
                    seen_paths.add(m.get("path"))
                    media.append(dict(m))
            if media:
                metadata["media"] = media
            if not (content_text or "").strip() and not media:
                # 回収 (room_state_packages.md §11-2) で全項目が外れた消費の
                # 空バッチ — 見せるものが無いので空の <system></system> を
                # 作らない。送る側と測る側は同じこの一枚を通るので勘定は
                # ずれない。
                continue
            blocks.append({
                "role": "user",
                "content": _perception_block_text(content_text),
                "created_at": batch.consumed_at,
                "metadata": metadata,
            })
        return blocks
    except Exception:
        if raise_on_error:
            raise
        LOGGER.warning(
            "[sea][prepare-context] perception batch listing failed; "
            "presenting/counting history without perceptions", exc_info=True,
        )
        return []


def _resolve_perception_anchors_locked(
    conn: Any, presented: Sequence[Any], recent: Sequence[Dict[str, Any]],
) -> Dict[int, str]:
    """知覚のまとめの境界を、``recent`` の中の位置を決める発言の id に引く。

    残す設定のモデルへ送る列だけで使う (persist_realtime_and_recall.md §4.3)。
    知覚のまとめの境界 (``boundary_created_at`` / ``boundary_rowid``) は「バッチ
    確定時点で最後に保存済みだった発言」で、送る列に出ない行 (判断のパルスの
    記録・別のスレッドの行) のこともある。だから完全一致ではなく、``recent`` の
    発言のうち正典順序キー ``(created_at, rowid)`` が境界以下の最後のものを
    位置を決める発言にする (その発言の直後 = 確定時点で送る列の最後に居た発言の
    直後)。

    境界が NULL の古い行、境界より前に ``recent`` の発言が無い行は含めない
    (呼び出し側で秒の比較に落ちる — 残す設定のモデルの列では同じ秒の発言より
    前、:data:`PLACE_BEFORE_SAME_SECOND_KEY`)。返り値はバッチ id → 発言の id。

    部屋の様子の機構の置き直し (:func:`sai_memory.room_state.reseat_current_room`)
    も含めない — その境界は窓の判定 (可視性) のための記帳で、位置は意図的に
    最古にした ``consumed_at`` が決める (提示の先頭に置く)。境界で並べると、
    背景が最新の発言の直後へ移ってしまう (Codex 二巡目 #5)。送る側と測る側は
    この同じ関数を通るので、どちらでも先頭に残る。

    **呼び出し側が ``sai_mem._db_lock`` を保持している前提**。
    """
    from sai_memory.room_state import batch_is_room_reseat

    targets = [
        b for b in presented
        if not b.is_realtime_and_recall and b.id is not None
        and b.boundary_created_at is not None and b.boundary_rowid is not None
        and not batch_is_room_reseat(b.room_state_json)
    ]
    if not targets:
        return {}
    ids = [
        str(m.get("id")) for m in recent if isinstance(m, dict) and m.get("id")
    ]
    if not ids:
        return {}
    keyed: List[tuple] = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        placeholders = ",".join("?" for _ in chunk)
        for mid, created_at, rowid in conn.execute(
            f"SELECT id, created_at, rowid FROM messages WHERE id IN ({placeholders})",
            tuple(chunk),
        ).fetchall():
            if created_at is None or rowid is None:
                continue
            keyed.append((int(created_at), int(rowid), str(mid)))
    if not keyed:
        return {}
    keyed.sort()
    import bisect
    keys = [(k[0], k[1]) for k in keyed]
    anchors: Dict[int, str] = {}
    for b in targets:
        pos = bisect.bisect_right(
            keys, (int(b.boundary_created_at), int(b.boundary_rowid)),
        )
        if pos == 0:
            continue
        anchors[int(b.id)] = keyed[pos - 1][2]
    return anchors


def _realtime_and_recall_block(
    batch: Any, boundary_message_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """残したリアルタイム情報と自動想起の行を、送る列のブロックにする。

    文面は残した ``rendered_text`` を**包み直さずそのまま**出す — 送ったときの
    ``content`` そのものなので、一字でも変えると前回の送信と先頭が一致しなく
    なる (persist_realtime_and_recall.md 不変条件 1)。

    目印は知覚のブロックと同じ ``CONSUMED_PERCEPTION_KEY`` (勘定と退場計画には
    知覚と同じに参加する) に加えて、知覚とは別の
    :data:`PERSISTED_REALTIME_AND_RECALL_KEY` (自動想起のきっかけと反射判断の
    観察から外す — §4.5)。``__realtime_context__`` は付けない (リアルタイム
    スペルの結果の追記先にならないように)。位置を決める発言の id が無いものは
    出さない。
    """
    if not boundary_message_id:
        return None
    media = batch.media_list()
    content = batch.rendered_text or ""
    if not content.strip() and not media:
        return None
    metadata: Dict[str, Any] = {
        "tags": ["internal", "event_message", "realtime_and_recall"],
        CONSUMED_PERCEPTION_KEY: True,
        PERSISTED_REALTIME_AND_RECALL_KEY: True,
        "__perception_batch_id__": int(batch.id),
        BOUNDARY_ANCHOR_ID_KEY: str(boundary_message_id),
        REALTIME_AND_RECALL_RECALLED_KEY: [
            [st, sid] for st, sid in batch.recalled_keys()
        ],
    }
    if batch.pulse_id:
        metadata[REALTIME_AND_RECALL_PULSE_ID_KEY] = str(batch.pulse_id)
    if media:
        metadata["media"] = media
    return {
        "role": "user",
        "content": content,
        "created_at": batch.consumed_at,
        "metadata": metadata,
    }


def _block_batch_id(msg: Dict[str, Any]) -> int:
    """ブロックの台帳 id (省略の印など id を持たないものは 0)。"""
    meta = msg.get("metadata") if isinstance(msg, dict) else None
    try:
        return int((meta or {}).get("__perception_batch_id__") or 0)
    except (TypeError, ValueError):
        return 0


def _block_anchor_id(msg: Dict[str, Any]) -> str:
    """ブロックの位置を決める発言の id (持たなければ空文字)。"""
    meta = msg.get("metadata") if isinstance(msg, dict) else None
    return str((meta or {}).get(BOUNDARY_ANCHOR_ID_KEY) or "")


def _place_anchored_blocks(
    merged: List[Dict[str, Any]], anchored: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """ブロックを、位置を決める発言の直後に置く (merge_perception_blocks の後段)。

    位置は時刻の秒ではなく発言で決める (persist_realtime_and_recall.md §4.3 —
    最新の発言と返事のすき間という狭い位置に入るので、秒の丸めで前後が
    入れ替わる余地がある)。発言の直後に既に並んでいるブロックのうち、自分より
    台帳 id の小さいもの (その発言の後ろに並ぶ、自分より先に確定した知覚の
    まとめや残した行) の後ろに入る — 同じ発言の直後に並ぶもの同士は表の id の
    順。発言が列に無ければ置かない (送らない — 呼び出し側は、知覚のまとめを
    ここへ渡す前に、発言が列にあることを確かめておく)。
    """
    out = list(merged)
    for block in sorted(anchored, key=_block_batch_id):
        boundary_id = _block_anchor_id(block)
        if not boundary_id:
            continue
        idx = None
        for i, msg in enumerate(out):
            if is_injected_perception(msg):
                continue
            if str(msg.get("id") or "") == boundary_id:
                idx = i
                break
        if idx is None:
            continue
        own = _block_batch_id(block)
        pos = idx + 1
        while (
            pos < len(out)
            and is_injected_perception(out[pos])
            and _block_batch_id(out[pos]) < own
        ):
            pos += 1
        out.insert(pos, block)
    return out


def _goes_before(block: Dict[str, Any], epoch: int) -> bool:
    """時刻で並べるブロックが、時刻 ``epoch`` の生ログより前に入るか。

    既定は厳密な ``<`` (同じ秒なら生ログが先)。:data:`PLACE_BEFORE_SAME_SECOND_KEY`
    を持つブロック (残す設定のモデルへ送る列の、境界で位置を決められない知覚の
    まとめと省略の印) は ``<=`` — 同じ秒の生ログより前に入る。
    """
    created_at = block["created_at"]
    meta = block.get("metadata") if isinstance(block, dict) else None
    if (meta or {}).get(PLACE_BEFORE_SAME_SECOND_KEY):
        return created_at <= epoch
    return created_at < epoch


def merge_perception_blocks(
    recent: Sequence[Dict[str, Any]], blocks: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """生ログと知覚ブロックを時刻順にマージした列を返す (純関数)。

    ブロックは「自分の ``consumed_at`` 以下の生ログ」の直後に入る (同時刻なら
    生ログが先 — 消費は書き込みの後に起きた事実)。送る側と測る側が**同じ並び**
    を見るための一点。

    ただし :data:`PLACE_BEFORE_SAME_SECOND_KEY` を持つブロック (残す設定の
    モデルへ送る列で、境界で位置を決められない知覚のまとめと省略の印) は
    「自分の ``consumed_at`` 以上の時刻を持つ最初の生ログ」の直前に入る (同じ
    秒の生ログより前)。厳密な比較のままだと、後から同じ秒の発言が保存される
    たびにブロックが後ろへ動き、前回の送信と先頭が一致しなくなる
    (persist_realtime_and_recall.md §4.3、Codex 九巡目)。同じ位置に並ぶもの
    同士は渡された順 (台帳の ``consumed_at, id`` の順)。OFF のモデルの列の
    ブロックは印を持たないので並びは変わらない。

    位置を決める発言 (:data:`BOUNDARY_ANCHOR_ID_KEY`) を持つブロックは、時刻では
    なくその発言の直後に並べる (:func:`_place_anchored_blocks`)。持つのは
    残したリアルタイム情報と自動想起の行 (:data:`PERSISTED_REALTIME_AND_RECALL_KEY`)
    と、残す設定のモデルへ送るときの知覚のまとめ (組成側
    :func:`list_presented_perception_blocks` が付ける)。知覚のまとめの発言が
    ``recent`` に無ければ (組成と並べで別の列を渡した場合)、時刻の順に落とす
    (残す設定のモデルの知覚のまとめは印を持つので同じ秒の発言より前)
    — 知覚は消さない。残した行は発言が無ければ出さない (§4.3)。
    """
    recent_ids = {
        str(m.get("id")) for m in recent if isinstance(m, dict) and m.get("id")
    }
    epoch_blocks: List[Dict[str, Any]] = []
    anchored: List[Dict[str, Any]] = []
    for b in blocks:
        if is_persisted_realtime_and_recall(b):
            anchored.append(b)
        elif _block_anchor_id(b) and _block_anchor_id(b) in recent_ids:
            anchored.append(b)
        else:
            epoch_blocks.append(b)
    merged: List[Dict[str, Any]] = []
    bi = 0
    for msg in recent:
        epoch = _payload_epoch(msg)
        while (
            bi < len(epoch_blocks)
            and epoch is not None
            and _goes_before(epoch_blocks[bi], epoch)
        ):
            merged.append(epoch_blocks[bi])
            bi += 1
        merged.append(msg)
    merged.extend(epoch_blocks[bi:])
    if anchored:
        merged = _place_anchored_blocks(merged, anchored)
    return merged


def _merge_consumed_perceptions(
    runtime: Any, persona: Any, recent: List[Dict[str, Any]],
    *,
    anchor_id: Optional[str] = None,
    model_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """提示履歴に未付記の消費バッチを時刻順マージする (W14, §10.3)。

    組成は :func:`list_presented_perception_blocks`、並びは
    :func:`merge_perception_blocks` — ここは送信経路の入口として二つを繋ぐ
    だけで、規則そのものは持たない。``model_key`` はこの context を届ける
    Session の実行 model。組成は読み取り専用なので、プレビューの列と実際に
    送る列で呼び分けは要らない。
    """
    blocks = list_presented_perception_blocks(
        runtime, persona, recent, anchor_id=anchor_id, model_key=model_key,
    )
    if not blocks:
        return recent
    merged = merge_perception_blocks(recent, blocks)
    LOGGER.debug(
        "[sea][prepare-context] merged %d perception batch block(s) "
        "into history", len(blocks),
    )
    return merged


def _maybe_inject_auto_recall(
    runtime,
    persona: Any,
    messages: List[Dict[str, Any]],
    *,
    pulse_type: Optional[str],
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    persist_mode: bool = False,
) -> Optional[Any]:
    """自動想起ブロックを組み立て、履歴末尾 (最新 user 入力の直前) に注入する。

    記憶アーキv2 §4。スコープは CONVERSATION アスペクトのみ (§4.6)。注入発生時は
    event_callback で ``auto_recall`` イベントを流し、フロントで折りたたみ表示する (§4.5)。

    反射判断が時間内に答えず従来方式へ戻ったターンでは、同じ event_callback へ
    ``reflex_fallback`` イベントを 1 回流す。こちらは**画面に出すだけ**で、どこにも
    保存しない (docs/intent/reflex_judgment.md 経緯 2026-09-21)。

    ``persist_mode`` (リアルタイム情報と自動想起を残すモデル —
    docs/intent/persist_realtime_and_recall.md §4.4) のときは:

    - いま送る列にある残した行に入っている記憶を、候補を集める段階で外す
      (``run_auto_recall`` の ``exclude_keys``)。
    - 出すのは新しく浮かんだ記憶だけ (``new_only``)。粘着の台帳から出し直さない。
    - ここでは差し込まない。呼び出し元 (:func:`prepare_context`) がリアルタイム
      情報とまとめて送る列の一番後ろに置く。画面のイベントと state への持ち帰り
      は、差し込んだのと同じ「新しく浮かんだ分」で流す。

    Returns: 実行した回の ``AutoRecallResult`` (走らなかった回は None)。
    """
    from sea.pulse_context import Aspect, aspect_from_pulse_type

    aspect = aspect_from_pulse_type(pulse_type)
    if aspect is not Aspect.CONVERSATION:
        LOGGER.debug(
            "[sea][auto_recall] skip: aspect=%s (pulse_type=%s) is not CONVERSATION",
            aspect.value, pulse_type,
        )
        return None

    # ペルソナ単位 ON/OFF トグル (AUTO_RECALL_ENABLED)。OFF のときは注入せず、
    # 粘着台帳もリセットする (再度 ON にしたとき古い記憶を引きずらないため)。
    persona_id_for_gate = getattr(persona, "persona_id", None)
    if not runtime._is_auto_recall_enabled_for_persona(persona):
        LOGGER.debug(
            "[sea][auto_recall] skip: AUTO_RECALL_ENABLED=False for persona=%s",
            persona_id_for_gate,
        )
        if persona_id_for_gate:
            from sea.auto_recall import reset_ledger
            reset_ledger(persona_id_for_gate)
        return None

    sai_mem = getattr(persona, "sai_memory", None)
    conn = getattr(sai_mem, "conn", None) if sai_mem else None
    embedder = getattr(sai_mem, "embedder", None) if sai_mem else None
    persona_id = getattr(persona, "persona_id", None)
    if not persona_id or conn is None or embedder is None:
        LOGGER.debug(
            "[sea][auto_recall] skip: persona_id/conn/embedder unavailable (persona=%s)",
            persona_id,
        )
        return None

    # 粘着台帳のキー用 canonical thread_id (2026-07-12 監査 P1: thread 跨ぎ防止)。
    # メインライン履歴の読み書きと同じ解決 (_thread_id(None) = active thread または
    # 既定 persona thread) を adapter から取得して明示的に渡す。解決できなければ
    # 注入をスキップする (persona 単位に落として thread を混ぜるより安全側)。
    thread_id: Optional[str] = None
    thread_resolver = getattr(sai_mem, "_thread_id", None)
    if callable(thread_resolver):
        try:
            thread_id = thread_resolver(None)
        except Exception:
            LOGGER.warning(
                "[sea][auto_recall] failed to resolve canonical thread_id (persona=%s)",
                persona_id, exc_info=True,
            )
    if not thread_id:
        LOGGER.debug(
            "[sea][auto_recall] skip: canonical thread_id unavailable (persona=%s)",
            persona_id,
        )
        return None

    # ペルソナ単位の「自動想起を強化する」スイッチ (AUTO_RECALL_ENHANCED)。
    # run_auto_recall は persona_id 文字列しか受けないので、persona オブジェクトを
    # 持っているここで読んで旗として渡す (docs/intent/reflex_judgment.md §4)。
    # 読めなかった回は OFF に倒す — 費用の出る側を既定にしない。
    try:
        enhanced = bool(runtime._is_auto_recall_enhanced_for_persona(persona))
    except Exception:
        LOGGER.warning(
            "[sea][auto_recall] failed to read AUTO_RECALL_ENHANCED (persona=%s); "
            "treating it as off", persona_id, exc_info=True,
        )
        enhanced = False

    # ペルソナ個別の反射判断モデル (AI.REFLEX_JUDGMENT_MODEL)。ここも persona
    # オブジェクトを持っているこの場所で読んで渡す (docs/intent/reflex_judgment.md §1)。
    # None = 上書きなし = 世界の既定 (モデルの役割の割り当て) に従う。
    # 読むのはスイッチが ON のペルソナだけ — OFF では値がどこでも使われないので、
    # 全ペルソナの毎ターンに DB 読みを 1 回足さない (2026-09-20 のローカルレビューの指摘)。
    reflex_model_key = None
    if enhanced:
        try:
            reflex_model_key = runtime._get_reflex_model_for_persona(persona)
        except Exception:
            LOGGER.warning(
                "[sea][auto_recall] failed to read REFLEX_JUDGMENT_MODEL (persona=%s); "
                "falling back to the world default", persona_id, exc_info=True,
            )
            reflex_model_key = None

    from sea.auto_recall import run_auto_recall

    # 残すモデル: いま送る列にある残した行に入っている記憶を、候補を集める段階で
    # 外す (persist_realtime_and_recall.md §4.4)。OFF では引数を渡さない
    # (従来の呼び出しのまま)。
    persist_kwargs: Dict[str, Any] = {}
    if persist_mode:
        persist_kwargs = {
            "exclude_keys": _presented_recalled_keys(messages),
            "new_only": True,
        }

    result = run_auto_recall(
        conn, embedder, messages,
        persona_id=persona_id, thread_id=thread_id, enhanced=enhanced,
        reflex_model_key=reflex_model_key,
        **persist_kwargs,
    )

    # 反射判断が時間内に答えず従来方式へ戻ったターンは、そのことを画面に出す。
    # **表示専用**: 会話履歴にも建物の記録にもペルソナの記憶にも書かない (下の
    # auto_recall と違って、発言の metadata へ載せる経路も持たない)。注入が起きた
    # かどうかとは無関係に起こりうるので、下の早期 return より前に出す。
    # 出す先が無いターン (event_callback を持たない自律 Pulse) は静かに何もしない —
    # 集計 (saiverse/reflex_judgment.py の直近の記録) には既に数えられている。
    if result.reflex_deadline_fallback and event_callback:
        try:
            event_callback({
                "type": "reflex_fallback",
                "reason": "deadline",
                "persona_id": persona_id,
                "persona_name": getattr(persona, "persona_name", None),
                "building_id": event_building_id(runtime, persona),
            })
        except Exception:
            LOGGER.debug("[sea][auto_recall] reflex_fallback event_callback failed", exc_info=True)

    if not result.injected or not result.block:
        return result

    if persist_mode:
        # 差し込みは呼び出し元がリアルタイム情報とまとめて一番後ろに置く。
        LOGGER.debug(
            "[sea][auto_recall] newly surfaced %d memory(ies) for the persisted "
            "turn block (%d chars)", len(result.recalled_keys), result.char_count,
        )
    else:
        recall_msg = {
            "role": "user",
            "content": result.block,
            "metadata": {"__auto_recall__": True},
        }

        # realtime_context と同じ挿入面: 最新 user メッセージの直前に入れる
        # (cached_head_architecture C5 と同じ末尾注入面)。auto_recall メタ付きの
        # 自己メッセージは対象外にして冪等にする。
        last_user_idx = None
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if m.get("role") != "user":
                continue
            meta = m.get("metadata") or {}
            if meta.get("__auto_recall__") or meta.get("__realtime_context__"):
                continue
            last_user_idx = i
            break

        if last_user_idx is not None:
            messages.insert(last_user_idx, recall_msg)
        else:
            messages.append(recall_msg)

        LOGGER.debug(
            "[sea][auto_recall] injected block before last user (idx=%s, %d chars)",
            last_user_idx, result.char_count,
        )

    if event_callback:
        try:
            event_callback({
                "type": "auto_recall",
                "content": result.block,
                "persona_id": persona_id,
                "persona_name": getattr(persona, "persona_name", None),
                # 想起が起きた時点の部屋。名乗らないと、別の部屋で進んでいる
                # Beat の想起が閲覧中の部屋の吹き出しに混ざる
                # (docs/issues/pulse_beats_merge_into_single_record.md 契約 5)。
                "building_id": event_building_id(runtime, persona),
            })
        except Exception:
            LOGGER.debug("[sea][auto_recall] event_callback failed", exc_info=True)

    # 永続化用の受け渡し (reasoning と同じ「state 経由で speak/say ノードまで運ぶ」流儀)。
    # _prepare_context は純粋関数 (messages しか返さない) なので、run_playbook が
    # parent_state に転記できるよう persona の一時属性に置く (persona._current_pulse_type
    # と同じパターン。永続化するのは <system> タグを剥がした本文のみ — スペル実行結果と
    # 同様、履歴末尾への再注入とは別経路で ChatMessage.auto_recall に載る (api/routes/chat.py)。
    # LLM コンテキストには載らない (推論と同じ扱い。metadata は llm_clients 側で
    # 読まれるのは media 系キーのみ)。
    persona._pending_auto_recall_text = result.plain_text
    return result


def _presented_recalled_keys(messages: Sequence[Dict[str, Any]]) -> set:
    """送る列にある「残した行」に入っている自動想起の ``(source_type, source_id)``。

    残すモデルの自動想起が候補を集める段階で外す集合
    (docs/intent/persist_realtime_and_recall.md §4.4)。列に無い (Chronicle の
    付記で外れた・位置を決める発言が無い) 残した行の記憶は含まない — その
    記憶はペルソナの目に入っていないので、また浮かんでよい。
    """
    keys: set = set()
    for msg in messages:
        if not isinstance(msg, dict) or not is_persisted_realtime_and_recall(msg):
            continue
        recalled = (msg.get("metadata") or {}).get(REALTIME_AND_RECALL_RECALLED_KEY)
        for item in recalled or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                keys.add((str(item[0]), str(item[1])))
    return keys


def _expand_recalled_ids(
    runtime,
    persona: Any,
    recalled_ids: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
) -> None:
    """Expand recalled_ids into actual content and append as a system message.

    Resolves each recalled ID via URI resolver to get the actual content,
    then appends a single system message at the end of the messages list.
    """
    from saiverse.uri_resolver import UriResolver

    resolver = UriResolver(manager=runtime.manager)
    persona_id = getattr(persona, "persona_id", None)
    if not persona_id:
        return

    sections = []
    for item in recalled_ids:
        uri = item.get("uri", "")
        title = item.get("title", "")
        source_type = item.get("type", "")

        if not uri:
            continue

        try:
            resolved = resolver.resolve(uri, persona_id=persona_id)
            if resolved and resolved.content:
                content = resolved.content
                # Truncate per item to keep context manageable
                if len(content) > 1500:
                    content = content[:1500] + "..."
                label = "Chronicle" if source_type == "chronicle" else "Memopedia"
                sections.append(f"### [{label}] {title}\n{content}")
            else:
                LOGGER.debug(
                    "[sea][prepare-context] Could not resolve recalled URI: %s", uri,
                )
        except Exception as exc:
            LOGGER.debug(
                "[sea][prepare-context] Error resolving recalled URI %s: %s", uri, exc,
            )

    if sections:
        recalled_text = "## 想起した記憶\n以下はワーキングメモリに保持されている記憶です。\n\n" + "\n\n".join(sections)
        messages.append({
            "role": "system",
            "content": recalled_text,
            "metadata": {"__recalled_memory__": True},
        })
        LOGGER.info(
            "[sea][prepare-context] Expanded %d/%d recalled_ids into context (%d chars)",
            len(sections), len(recalled_ids), len(recalled_text),
        )
        LOGGER.debug(
            "[sea][prepare-context] Recalled memory content:\n%s", recalled_text,
        )


def _replace_weave_messages(
    messages: List[Dict[str, Any]], new_weave: List[Dict[str, Any]],
) -> None:
    """messages 内の Memory Weave メッセージ群を new_weave で置き換える (in place)。

    位置は既存 weave の先頭位置を保つ (head の並び順を崩さない)。既存 weave が
    無い場合は何もしない — weave 無効の設定を上書きしない。
    """
    indices = [
        i for i, m in enumerate(messages)
        if isinstance(m.get("metadata"), dict)
        and m["metadata"].get("__memory_weave_context__")
    ]
    if not indices:
        return
    insert_at = indices[0]
    for i in reversed(indices):
        del messages[i]
    messages[insert_at:insert_at] = list(new_weave)


def _swap_preview_weave_for_refill(
    runtime, persona: Any, messages: List[Dict[str, Any]],
    refill_plan: Dict[str, Any],
) -> bool:
    """preview 専用: head 由来の weave を読み戻し後の除外名簿で組み直して差し替える。

    head の weave は capture 済み snapshot に凍っており、読み戻しで生に開く
    範囲のあらすじを除外できていない (本番は書き込み後の再 capture が追随する
    — session_lifecycle.maybe_run_window_refill)。プレビューは行を触らない
    (§14-6-5) ので、同じ組み立て (get_memory_weave_context) を読み戻し後の
    名簿と窓の始点で読みだけ実行して差し替える。

    Returns:
        整合が取れたか。True = 差し替えた / weave がそもそも提示に無い。
        False = 組み直しに失敗 (messages は無変更) — 呼び出し側は読み戻し
        プレビューごと見送って素の窓に落とすこと。旧 weave のまま読み戻し後の
        履歴を出すと、生に開いた範囲のあらすじと生ログが同時に並ぶ二重表示に
        なる (Codex 指摘 2026-07-30)。
    """
    has_weave = any(
        isinstance(m.get("metadata"), dict)
        and m["metadata"].get("__memory_weave_context__")
        for m in messages
    )
    if not has_weave:
        return True  # weave 無し = 衝突する相手が居ない
    try:
        from builtin_data.tools.get_memory_weave_context import (
            get_memory_weave_context,
        )
        from tools.context import persona_context

        persona_id = getattr(persona, "persona_id", None)
        sai_mem = getattr(persona, "sai_memory", None)
        persona_dir_path = getattr(sai_mem, "persona_dir", None) if sai_mem else None
        persona_dir = str(persona_dir_path) if persona_dir_path else None
        exclude_ids = list(refill_plan.get("fold_entry_ids") or [])
        with persona_context(persona_id, persona_dir, runtime.manager):
            # raise_on_error: 既定の「失敗 → []」変換のままだと、読取失敗が
            # 「成功した空 weave」としてコミットされ、weave を黙って失った
            # プレビューになる (Codex 指摘 2026-07-30)。空が返るのは本当に
            # 空のときだけにする。
            new_weave = get_memory_weave_context(
                persona_id=persona_id,
                persona_dir=persona_dir,
                exclude_chronicle_entry_ids=exclude_ids or None,
                raise_on_error=True,
            )
        _replace_weave_messages(messages, new_weave or [])
        return True
    except Exception:
        LOGGER.warning(
            "[sea][prepare-context] preview weave swap for refill failed; "
            "falling back to the plain (pre-refill) window", exc_info=True,
        )
        return False


def _preview_head_items(
    persona: Any, manager: Any, building_id: str,
) -> List[Any]:
    """Pulse の頭の検知ぶんを、未消費の項目と同じ器 (PerceptionItem) に写す。

    中身を組むのは sea/head_pipeline の読み取り専用の検知
    (:func:`~sea.head_pipeline.integration.preview_head_perceptions`)。ここは
    その戻りを、reduce / 回収 / 消費時描画が扱える形に詰め替えるだけ。

    ``id`` は台帳に無い仮の項目なので**負の値**を到着順に振る — 実在の行
    (rowid は 1 以上) と衝突しないので、後段が id で照合しても取り違えない。
    """
    from sai_memory.perception_buffer import PerceptionItem
    from sea.head_pipeline import preview_head_perceptions

    entries = preview_head_perceptions(persona, manager, building_id)
    now = int(time.time())
    items: List[Any] = []
    for offset, entry in enumerate(entries):
        media = entry.get("media")
        items.append(PerceptionItem(
            id=offset - len(entries),
            kind=entry["kind"],
            content=entry["content"],
            reduce_key=None,
            salient=0,
            media=(
                json.dumps(media, ensure_ascii=False)
                if isinstance(media, list) and media else media
            ),
            metadata=entry.get("metadata"),
            created_at=now,
        ))
    return items


def _compose_pending_preview(
    persona: Any, manager: Any, building_id: str,
) -> List[Any]:
    """次の Pulse が読む知覚のプレビュー組成 — 実 flush と同じ並びの一枚 (読むだけ)。

    材料は二つで、実 Pulse の到着順に並べる:

    1. **実際に未消費の項目** — 移動通知など、既にバッファに溜まっている分。
    2. **Pulse の頭が積むはずの項目** — 世界状態の差分・部屋の様子・同席の
       想起。検知が Pulse の頭へ移った (2026-09-07) ことで、溜まっている物と
       「話しかけた瞬間にペルソナが読む物」の間にこの差が開いた。頭と同じ検知を
       **読み取り専用**で走らせて仮の未消費項目に写す (:func:`_preview_head_items`)。

    そのうえで実 flush (saiverse_memory/adapter.flush_perception_buffer_payload)
    と同じ reduce → 回収 (reclaim_pending_perceptions —
    room_state_packages.md §11-2) → 消費時描画 (render_pending_room_states —
    部屋の様子の差分/全文は §11-2 規則 2 でこの一回だけ描く) を通す。まはーが
    プレビューで見る文面と次の Pulse が実際に知覚する文面を一致させるための
    同順で、DB の行は書き換えない (描画はこの組成で使う写しだけ)。
    """
    from sai_memory.perception_buffer import list_pending, reduce_perceptions
    from sai_memory.room_state import (
        reclaim_pending_perceptions,
        render_pending_room_states,
    )
    sai_mem = getattr(persona, "sai_memory", None)
    if sai_mem is None:
        return []
    # 頭の検知は sai_mem の錠前の外で走らせる (world の読みを含み、内部で
    # 必要なぶんだけ自分で錠前を取る)。
    head_items = _preview_head_items(persona, manager, building_id)
    with sai_mem._db_lock:
        pending = list(list_pending(sai_mem.conn)) + head_items
        if not pending:
            return []
        return render_pending_room_states(
            sai_mem.conn,
            reclaim_pending_perceptions(reduce_perceptions(pending)),
        )


def preview_context(
    runtime,
    persona: Any,
    building_id: str,
    user_input: str,
    meta_playbook: Optional[str] = None,
    image_count: int = 0,
    document_count: int = 0,
) -> Dict[str, Any]:
    """Build the context that would be sent to the LLM, without executing anything.

    Returns a dict with messages, token estimates, cost estimates, and model info.
    Does NOT record the user message to history or call any LLM.
    """
    from saiverse.token_estimator import estimate_messages_tokens, estimate_image_tokens

    # Select playbook (same logic as run_meta_user)
    if meta_playbook:
        playbook = runtime._load_playbook_for(meta_playbook, persona, building_id)
        if playbook is None:
            playbook = runtime._choose_playbook(kind="user", persona=persona, building_id=building_id)
    else:
        playbook = runtime._choose_playbook(kind="user", persona=persona, building_id=building_id)

    # Build context messages (without recording user message to history)
    # requirements は渡さない = 既定 (履歴フル + 固定 head)。実際に sub_speak が
    # 見るものと一致する。2026-07-23 以前はここに全部入りの手書きコピーがあったが、
    # head の章立てが呼び出し側から選べなくなったので不要になった。
    context_warnings: List[Dict[str, Any]] = []
    messages = runtime._prepare_context(
        persona, building_id, user_input=None,
        warnings=context_warnings,
        preview_only=True,
    )

    # Append the user message manually (in real flow it comes from history)
    if user_input:
        messages.append({"role": "user", "content": user_input})

    # Classify each message into a section
    from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
    persona_model = getattr(persona, "model", None) or BUILTIN_DEFAULT_LITE_MODEL
    provider = get_model_provider(persona_model)

    section_order = [
        "system_prompt", "memory_weave_chronicle", "memory_weave_memopedia",
        "memory_weave", "visual_context",
        "history", "realtime_context", "perception_buffer", "user_message",
    ]
    section_labels = {
        "system_prompt": "System Prompt",
        "memory_weave_chronicle": "Memory Weave — Chronicle",
        "memory_weave_memopedia": "Memory Weave — Memopedia",
        "memory_weave": "Memory Weave",
        "visual_context": "Visual Context",
        "history": "Conversation History",
        "realtime_context": "Realtime Context",
        "perception_buffer": "知覚バッファ（未消費・次のPulseで反映）",
        "user_message": "Your Message",
        "attachments": "Attachments",
    }
    section_tokens: Dict[str, int] = {s: 0 for s in section_order}
    section_tokens["attachments"] = 0
    section_msg_counts: Dict[str, int] = {s: 0 for s in section_order}
    section_msg_counts["attachments"] = 0

    annotated_messages: List[Dict[str, Any]] = []
    for i, msg in enumerate(messages):
        meta = msg.get("metadata") or {}
        # Determine section
        if msg.get("role") == "system":
            section = "system_prompt"
        elif meta.get("__memory_weave_context__"):
            mw_type = meta.get("__memory_weave_type__", "")
            if mw_type == "chronicle":
                section = "memory_weave_chronicle"
            elif mw_type == "memopedia":
                section = "memory_weave_memopedia"
            else:
                section = "memory_weave"
        elif meta.get("__visual_context__"):
            section = "visual_context"
        elif meta.get("__realtime_context__"):
            section = "realtime_context"
        elif i == len(messages) - 1 and msg.get("role") == "user" and user_input and msg.get("content") == user_input:
            section = "user_message"
        else:
            section = "history"

        msg_tokens = estimate_messages_tokens([msg], provider)
        section_tokens[section] += msg_tokens
        section_msg_counts[section] += 1

        annotated: Dict[str, Any] = {
            "role": msg.get("role", "unknown"),
            "content": msg.get("content", ""),
            "section": section,
            "tokens": msg_tokens,
        }
        if meta.get(CONSUMED_PERCEPTION_KEY):
            # 提示済みの知覚バッチ (と省略の印) のバッジ (2026-09-06 まはー裁定:
            # 「単に見やすくなるだけ」に絞る)。節は history のまま動かさず、
            # 新しい節も集計の行も作らない — 知覚の量の勘定は水位管理が既に
            # 持っており、二冊目の帳簿を作らない。印のある行だけ真で載せる。
            annotated["perception_batch"] = True
            if meta.get("__room_state__"):
                annotated["room_state"] = True
        annotated_messages.append(annotated)

    # Add estimated attachment tokens
    attachment_tokens = 0
    if image_count > 0:
        attachment_tokens += image_count * estimate_image_tokens(provider)
    if document_count > 0:
        # Rough estimate: ~500 tokens per document (varies widely)
        attachment_tokens += document_count * 500
    section_tokens["attachments"] = attachment_tokens

    # 知覚バッファ (未消費) — 次の Pulse で flush されてプロンプトに入る予定の知覚を
    # プレビューに出す (docs/intent/perception_buffer.md Phase 3, 透明性 §6)。
    # 既に溜まっている未消費分に加えて、Pulse の頭が積むはずの検知ぶん (世界状態の
    # 差分・部屋の様子・同席の想起) も**読み取り専用**で合成する — 検知が Pulse の
    # 頭へ移った (2026-09-07) ので、溜まっている物だけでは「いま話しかけたら
    # ペルソナが読むもの」にならない。読み取り専用の意味は
    # _compose_pending_preview / head_pipeline.preview_head_perceptions を参照。
    try:
        sai_mem = getattr(persona, "sai_memory", None)
        if sai_mem is not None and getattr(sai_mem, "is_ready", lambda: False)():
            from sai_memory.perception_buffer import format_perception_message
            reduced_pending = _compose_pending_preview(
                persona, runtime.manager, building_id,
            )
            if reduced_pending:
                pb_text = format_perception_message(reduced_pending)
                pb_msg = {"role": "user", "content": f"<system>{pb_text}</system>"}
                pb_tokens = estimate_messages_tokens([pb_msg], provider)
                section_tokens["perception_buffer"] = pb_tokens
                section_msg_counts["perception_buffer"] = 1
                annotated_messages.append({
                    "role": "user",
                    "content": pb_msg["content"],
                    "section": "perception_buffer",
                    "tokens": pb_tokens,
                })
    except Exception:
        LOGGER.warning("[preview] perception buffer section failed", exc_info=True)

    total_input_tokens = sum(section_tokens.values())
    context_length = get_context_length(persona_model)
    pricing_at = datetime.now(timezone.utc)
    pricing = get_model_pricing(persona_model, at=pricing_at)

    # Cost range: best case (all cached) to worst case (all cache-write)
    cache_kwargs = runtime._get_cache_kwargs()
    cache_enabled = cache_kwargs.get("enable_cache", False)
    cache_ttl = cache_kwargs.get("cache_ttl", "5m")

    # Determine cache type (explicit for Anthropic, implicit for Gemini, etc.)
    from saiverse.model_configs import get_cache_config
    cache_config = get_cache_config(persona_model)
    cache_type = cache_config.get("type", "implicit")

    if cache_enabled and pricing and pricing.get("cached_input_per_1m_tokens") is not None:
        # Best case: everything is a cache hit
        cost_best = calculate_cost(
            persona_model, total_input_tokens, 0,
            cached_tokens=total_input_tokens, cache_write_tokens=0, at=pricing_at,
        )
        # Worst case: everything is a cache write
        cost_worst = calculate_cost(
            persona_model, total_input_tokens, 0,
            cached_tokens=0, cache_write_tokens=total_input_tokens,
            cache_ttl=cache_ttl, at=pricing_at,
        )
    else:
        # No cache: single estimate
        cost_best = calculate_cost(persona_model, total_input_tokens, 0, at=pricing_at)
        cost_worst = cost_best

    # Build sections summary
    all_sections = section_order + ["attachments"]
    sections_summary = []
    for s in all_sections:
        if section_tokens.get(s, 0) > 0 or section_msg_counts.get(s, 0) > 0:
            sections_summary.append({
                "name": s,
                "label": section_labels.get(s, s),
                "tokens": section_tokens.get(s, 0),
                "message_count": section_msg_counts.get(s, 0),
            })

    return {
        "persona_id": getattr(persona, "persona_id", "unknown"),
        "persona_name": getattr(persona, "persona_name", "Unknown"),
        "model": persona_model,
        "model_display_name": get_model_display_name(persona_model),
        "provider": provider,
        "context_length": context_length,
        "sections": sections_summary,
        "total_input_tokens": total_input_tokens,
        "estimated_cost_best_usd": round(cost_best, 6),
        "estimated_cost_worst_usd": round(cost_worst, 6),
        "cache_enabled": cache_enabled,
        "cache_ttl": cache_ttl if cache_enabled else None,
        "cache_type": cache_type if cache_enabled else None,
        "pricing": pricing or {},
        "messages": annotated_messages,
    }
