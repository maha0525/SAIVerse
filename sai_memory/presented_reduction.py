"""提示の節約 — 会話以外の内容を Metabolism の瞬間だけ縮める。

正典: docs/intent/presented_context_reduction.md の設計 1 (会話以外を機械的な
合図・条件で縮める) と設計 2 (タイミングは Metabolism の瞬間だけ)。

芯を専門用語なしで言うと二つ:

1. **操作通知は用が済んだら下ろす。** ペルソナがスペルやコア記憶を操作したときの
   お知らせは、head (プロンプトの前置き) が次の Metabolism まで凍結される間だけ
   変化を伝える「つなぎ」。Metabolism で head が今の状態に描き直された後は同じ
   ことを二度言っているだけなので、提示から下ろす。
2. **いない部屋の様子は縮める。** 現在地でない部屋の詳しい様子と画像は、離れて
   いる間だけ短い一行に縮める。**戻ってきたら全文と画像を見せ直す** (まはー裁定
   2026-09-09) — 縮めた全文を差分の土台のまま頼らせると、戻った回に「変わった
   ところだけ」しか見えない壊れ方をするため。

守っている不変条件 (intent の「不変条件」節):

- **ペルソナが見た記録は消さない。** ``perception_buffer`` の行も
  ``perception_batches.rendered_text`` (編纂の材料であり、下ろされた期間の
  読み口でもある確定文面) も書き換えない。縮みは**提示を組む一点**
  (:func:`reduce_presented_batches`) が確定文面の写しの上で行う。部屋の記帳
  (``room_state_json``) には縮めた印を**追加**して差分の土台の束を外すが、
  これは差分の組み方の帳簿への追記で、見た文面そのものは無傷。
- **縮めたら、そこで省略があったことを機構の名義で示す。** 黙って消すのは
  「そこに何も無かった」という記録の嘘 (2026-09-04 裁定 1)。
- **書き換えは Metabolism の瞬間以外に起きない。** 縮みの判断そのものは
  :func:`mark_presentation_reductions` が Metabolism の中だけで永続化し、以後の
  提示はその記録を読むだけ。移動しても発言しても提示の途中は変わらない
  (プロンプトキャッシュの前方一致の保護)。**Metabolism の入口の門で引き返す回**
  も同じ「Metabolism の瞬間」に数える (会話が畳めないまま合計だけが上限を超えて
  いるペルソナは本体へ入れないため。門は :func:`has_pending_reductions` が
  「縮めるものがある」と答えたときにだけ書く — 詳細は
  ``docs/intent/presented_context_reduction.md`` の「実装で確定したこと」6)。
- **一方向** — 一度縮めたものは戻らない。操作通知は前進しかしない境界
  (``perception_notice_presentation`` の model ごとの行) で、部屋は
  バッチの記帳に打つ外れない印で表す。揺り戻しでちらつかない。

**記録の持ち方を二つに分けた理由** (実装判断、2026-09-09): 操作通知は「その時点で
提示に出ていたもの全部」に一律に効くので、一方向の境界一本で表せる (既存の下ろし
境界と同じ器・同じ片道性)。部屋はバッチごとに答えが違う — 縮める条件が「その
Metabolism の時点の現在地でない部屋」で、境界一本で表すと、ペルソナが戻ってきた
回に同じ境界の再評価で印が外れて (縮めたものが戻って) 揺り戻す。だから部屋だけは
バッチの記帳 (``room_state_json``) に外れない印を打つ。

**境界の単位は model ごと・部屋の印はペルソナ共通** (2026-09-10 レビュー二巡目の
裁定)。一文で言うと「操作通知は、そのモデルの head が描き直されたときに、その
モデルの提示から下りる。部屋は head と無関係なのでペルソナ共通」。理由: head は
(persona, model) ごとに描き直される (``sea/head_pipeline`` の capture) ので、
model A の Metabolism で全 model の提示から通知を下ろすと、head が凍結された
ままの model B が「変化を伝えるつなぎ」を失う — この機構が塞いだはずの穴
(``sea/head_pipeline/notify.py`` 冒頭の「別 model の Session が変更を知る手段が
無い」) が開き直る。跡地の文面「いまの状態が改めて示されているため」も B では
嘘になる。部屋の様子は head に載らないので、この理屈が当てはまらない。

``model_key`` を渡せない呼び出し (model の分からない読み口・旧テスト) では
**通知を一つも下ろさない** = 全部見せる、の安全側に倒す。
"""
from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

LOGGER = logging.getLogger(__name__)

#: 提示から下ろす操作通知の型 (``perception_buffer.kind``)。
#:
#: - ``head_mutation``: sea/head_pipeline/notify.py が発行する head 操作の通知。
#:   本文は head に入るのと**同一の描画**を同梱する設計なので、head が描き直された
#:   時点で完全な重複になる (コア記憶・机・Memopedia 目次の操作がこれ)。
NOTICE_KINDS = frozenset({"head_mutation"})

#: 提示から下ろす ``world_state`` 通知のラベル型
#: (:data:`sai_memory.room_state.LABEL_KIND_META_KEY` に載る値)。
#:
#: スペル一覧の増減だけを対象にする — head のスペル一覧セクションが今の一覧を
#: そのまま見せるので、Metabolism 後は重複になる (付与通知はスキーマ込みで数千字
#: 級になりうる、2026-09-04 裁定の実装)。
#:
#: **対象外にしたもの**と理由 (2026-09-09 に全 Section の diff_to_notifications を
#: 読んで確定): 移動 (``building_changed``)・入退室 (``occupant_*``)・机からの
#: 押し出し (``desk_*``) は head の状態ではなく**出来事**で、head を見ても
#: 「何が起きたか」は分からない。Building 名・説明・共通プロンプト・施設・
#: ペルソナ名の変更 (``building_renamed`` / ``building_system_prompt_changed`` /
#: ``common_prompt_changed`` / ``facilities_changed`` / ``persona_renamed`` /
#: ``persona_system_prompt_changed``) と Playbook の増減 (``playbook_*``) は
#: 構造としてはスペル一覧と同じ形だが、intent が名指ししていないので今回は
#: 触らない (どれも一行から数行で、縮めても得るものが小さい)。
NOTICE_LABEL_KINDS = frozenset({
    "spell_added",
    "spell_removed",
    "spell_system_enabled",
    "spell_system_disabled",
})

#: 部屋の記帳に打つ「縮めた」印と、その相棒のフィールド。
SHRUNK_FLAG = "shrunk"
SHRUNK_BLOCK_FIELD = "shrunk_block"
DROPPED_MEDIA_FIELD = "dropped_media"


def is_droppable_notice(kind: Optional[str], metadata: Optional[str]) -> bool:
    """この台帳の行が「用が済んだら下ろす操作通知」か。

    ``metadata`` は台帳の JSON 文字列 (無ければ None)。``world_state`` は
    ラベルの型 (``label_kind``) で選り分ける — 型が無い通知 (旧世代の行・
    型付けしていない Section) は対象外 = 触らない。
    """
    if not kind:
        return False
    if kind in NOTICE_KINDS:
        return True
    if kind != "world_state" or not metadata:
        return False
    from sai_memory.room_state import LABEL_KIND_META_KEY

    try:
        meta = json.loads(metadata)
    except (TypeError, ValueError):
        return False
    if not isinstance(meta, dict):
        return False
    return str(meta.get(LABEL_KIND_META_KEY) or "") in NOTICE_LABEL_KINDS


# ---------------------------------------------------------------------------
# 操作通知の一方向境界 (model ごと — perception_notice_presentation)
# ---------------------------------------------------------------------------

def _is_missing_schema_error(exc: sqlite3.OperationalError) -> bool:
    """テーブル / 列がまだ無い DB か (縮みの仕組みより古い世代)。"""
    from sai_memory.arasuji.storage import is_missing_table_error

    if is_missing_table_error(exc):
        return True
    return "no such column" in str(exc).lower()


def get_notice_cutoff(
    conn: sqlite3.Connection, model_key: Optional[str] = None,
) -> int:
    """この model が操作通知を下ろした境界 (この id までは通知を提示しない)。

    まだ一度も下ろしていない / この仕組みより古い DB なら 0 = 全部出る。
    ``model_key`` が無い呼び出しも 0 — どの model の提示を組んでいるのか
    決まらない以上、下ろす根拠 (「その model の head が描き直された」) が
    立たないので、全部見せる安全側に倒す。

    テーブルの不在**以外**の失敗は raise する — 0 を返すと「一度も下ろして
    いない」と同じ顔になり、下ろしたはずの通知が提示へ戻る
    (:func:`~sai_memory.perception_buffer.get_presentation_cutoff` と同じ契約)。
    """
    if not model_key:
        return 0
    try:
        row = conn.execute(
            "SELECT dropped_through_batch_id FROM perception_notice_presentation "
            "WHERE model_key = ?", (str(model_key),),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _is_missing_schema_error(exc):
            return 0
        raise
    return int(row[0]) if row and row[0] is not None else 0


def advance_notice_cutoff(
    conn: sqlite3.Connection, batch_id: int, model_key: Optional[str] = None,
) -> int:
    """この model の操作通知の境界を ``batch_id`` まで進める。**commit しない**。

    **一方向にしか進まない** — 既にそれ以上まで進んでいれば何もしない。後退は
    UPSERT の条件でも弾く。下ろし境界
    (:func:`~sai_memory.perception_buffer.advance_presentation_cutoff`) と違い、
    部屋の様子の土台の連なりには影響しない (通知は連なりに参加しない) ので、
    置き直し・回復の hook は持たない。

    ``model_key`` が無い呼び出しは**何も書かずに 0 を返す** — 誰の head が
    描き直されたのか言えないまま境界を進めると、head が凍結されたままの model の
    提示から通知だけが消える。

    Returns: 進めた後の実境界 (書き込み後に行を読み直した値)。
    """
    if not model_key:
        return 0
    current = get_notice_cutoff(conn, model_key)
    target = int(batch_id)
    if target <= current:
        return current
    conn.execute(
        "INSERT INTO perception_notice_presentation "
        "(model_key, dropped_through_batch_id, updated_at) "
        "VALUES (?, ?, strftime('%s','now')) "
        "ON CONFLICT(model_key) DO UPDATE SET "
        "dropped_through_batch_id = excluded.dropped_through_batch_id, "
        "updated_at = excluded.updated_at "
        "WHERE excluded.dropped_through_batch_id > "
        "perception_notice_presentation.dropped_through_batch_id",
        (str(model_key), target),
    )
    advanced = get_notice_cutoff(conn, model_key)
    if advanced < target:
        raise RuntimeError(
            f"perception notice cutoff did not advance for model {model_key!r}: "
            f"wrote {target} but the row reads {advanced} (the guard only loses "
            "to a larger value, so this is a failed write, not a race)"
        )
    return advanced


# ---------------------------------------------------------------------------
# 縮めた跡地に置く機構名義の文面
# ---------------------------------------------------------------------------

def shrunk_room_block(name: str) -> str:
    """縮めた部屋の様子の跡地に置く一行 (§10.9 の省略の印と同じ流儀)。"""
    from sai_memory.perception_buffer import PERCEPTION_OMISSION_HEADER

    # 文面は「今の状態」ではなく**規則**を言う — この一行は縮めたあともその
    # まま歴史上の位置に残るので、戻ってきたペルソナが読んでも嘘にならない形に
    # する (「いまこの部屋にいないため」だと、戻った回に自分の現在地と食い違う)。
    return (
        f"# 「{name}」の様子\n"
        f"{PERCEPTION_OMISSION_HEADER} この部屋にいない間は、詳しい様子と画像を"
        "表示から外しています。戻れば改めて全部見えます。"
    )


def notice_omission_block(count: int) -> str:
    """下ろした操作通知の跡地に置く一行。"""
    from sai_memory.perception_buffer import PERCEPTION_OMISSION_HEADER

    return (
        f"{PERCEPTION_OMISSION_HEADER}\n"
        f"操作のお知らせ {count} 件 (スペルの増減・コア記憶などの操作) は、"
        "いまの状態が改めて示されているため表示から外しました。"
    )


# ---------------------------------------------------------------------------
# Metabolism の瞬間の書き込み
# ---------------------------------------------------------------------------

def _raw_room_entries(room_state_json: Optional[str]) -> Optional[List[Any]]:
    """記帳の JSON を**篩わずに**生の list として読む (壊れていれば None)。

    :func:`~sai_memory.room_state.batch_room_states` は読む側の便宜で「dict で
    key を持つ要素」だけに絞るが、書き戻しにその結果を使うと、篩で落ちた未知の
    要素が黙って消える。ここは記帳への書き込み点なので生の並びを保ち、印を打つ
    エントリだけを差し替える (「記録は追加だけ」— ローカルレビュー指摘
    2026-09-10)。
    """
    if not room_state_json:
        return None
    try:
        data = json.loads(room_state_json)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _room_shrink_targets(
    batch_id: Any, room_state_json: Optional[str], current_key: str,
) -> Tuple[Optional[List[Any]], List[Dict[str, Any]], Set[str]]:
    """このバッチで縮められる部屋のエントリを選ぶ (**何も書かない**)。

    :func:`mark_presentation_reductions` (書く側) と
    :func:`has_pending_reductions` (門の前提条件) が**同じ一枚**を通るための
    切り出し。二枚に分かれると、門が「縮めるものがある」と言った回に書く側が
    何もしない (= 節目のたびに head を無駄に描き直す) 食い違いが生まれる。

    Returns:
        ``(記帳の生の並び, 縮める対象, 提示に残る側のメディア path)``。生の並びは
        篩っていない (未知の要素もそのまま入っている) ので、書く側は対象の dict
        を書き換えてこの並びをそのまま書き戻せる。読めない記帳は生の並びが
        ``None``。
    """
    from sai_memory.room_state import bundle_media, is_legacy_entry

    raw = _raw_room_entries(room_state_json)
    if not raw:
        return raw, [], set()
    entries = [e for e in raw if isinstance(e, dict) and e.get("key")]
    if not entries:
        return raw, [], set()
    targets: List[Dict[str, Any]] = []
    survivor_paths: Set[str] = set()
    survivor_blocks: Set[str] = set()
    for entry in entries:
        if entry.get(SHRUNK_FLAG):
            continue  # 既に縮めた — 触らない (一方向)。
        if is_legacy_entry(entry):
            # 束として読めない (旧形式) — 縮めないが、確定文面には
            # 全文のまま残るので照合の相手には数える。
            survivor_blocks.add(str(entry.get("block") or ""))
            continue
        if str(entry.get("key") or "") == current_key:
            survivor_paths.update(
                str(m["path"]) for m in bundle_media(entry["snapshot"])
                if m.get("path")
            )
            survivor_blocks.add(str(entry.get("block") or ""))
            continue
        targets.append(entry)
    # 縮める側と残る側のブロック文面が同一なら見送る — 差し替えは
    # 確定文面の中の**文字列一致**で位置を決めるので (_replace_block)、
    # 同名・同内容の部屋が同じバッチに二つあると、残すべき方 (現在地)
    # のブロックを縮めうる。曖昧なら触らない、の保守側
    # (ローカルレビュー指摘 2026-09-10)。
    if survivor_blocks:
        kept_targets = []
        for entry in targets:
            if str(entry.get("block") or "") in survivor_blocks:
                LOGGER.debug(
                    "[presented_reduction] skipping the shrink of room %s in "
                    "batch %s: its block text is identical to a room that stays "
                    "in the presentation", entry.get("key"), batch_id,
                )
                continue
            kept_targets.append(entry)
        targets = kept_targets
    return raw, targets, survivor_paths


def _has_droppable_notices(
    conn: sqlite3.Connection, batch_ids: Sequence[int],
) -> bool:
    """これらのバッチに「下ろす対象の操作通知」の台帳の行があるか。

    読むのは ``kind`` と ``metadata`` だけ — 本文 (``content``) は下ろされた
    バッチだと 10 万字規模になりうるので触らない。読み取り失敗は送出する
    (呼び出し側の門が「見送り」に倒す)。
    """
    ids = [int(b) for b in batch_ids]
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT kind, metadata FROM perception_buffer "
            f"WHERE consumed_batch_id IN ({placeholders})",
            tuple(chunk),
        ).fetchall()
        for kind, metadata in rows:
            if is_droppable_notice(kind, metadata):
                return True
    return False


def has_pending_reductions(
    conn: sqlite3.Connection, model_key: Optional[str] = None,
) -> bool:
    """いま :func:`mark_presentation_reductions` を呼んだら何か縮むか。

    **安い読みだけ**で答える (書き込みなし・LLM なし。読むのはバッチの id と
    部屋の記帳、台帳の ``kind`` / ``metadata`` だけで、確定文面には触らない)。
    使うのは Metabolism の縮みの入口 — 本体へ入れなかった回・入口の門で
    引き返す回に縮みを試す前の前提条件で、ここが False なら head の描き直し
    ごと見送る。見送りが要るのは、超過が続く限り毎ターンこの判定を通るから:
    縮めるものが無いのに head を描き直すと、プロンプトの前置きが毎回変わって
    キャッシュの前方一致が無駄に割れ続ける。

    True になるのは次のどちらか:

    1. **この model の**操作通知の境界より新しい提示中のバッチに、下ろす対象の
       通知の行がある (``model_key`` が無い呼び出しはこの条件を見ない —
       下ろす根拠が立たないため)
    2. 現在地でない部屋の、まだ縮めていないエントリが提示中のバッチにある
       (部屋の印はペルソナ共通なので model に依らない)

    **二度目は False になる** (冪等): 書く側は同じ ``model_key`` の境界を提示中の
    最大 id まで必ず進め、対象の部屋のエントリには必ず印を打つので、直後に
    呼び直すとどちらの条件も落ちる。

    既知の甘さ: 1 は台帳に行があることまでしか見ない — その通知が畳まれて
    確定文面に出ていない回は、書く側が「外すものなし」で終わる。ただし境界は
    その回に進むので、無駄な描き直しは**多くても一度**で止まる。

    読み取りの失敗は送出する (「縮めるものが無い」の False に化かさない —
    呼び出し側が見送りとして記録する)。
    """
    from sai_memory.perception_buffer import list_presented_batch_room_states
    from sai_memory.room_state import find_current_room_key

    presented = list_presented_batch_room_states(conn)
    if not presented:
        return False
    if model_key:
        cutoff = get_notice_cutoff(conn, model_key)
        fresh = [batch_id for batch_id, _json in presented if batch_id > cutoff]
        if fresh and _has_droppable_notices(conn, fresh):
            return True
    current_key = find_current_room_key(conn)
    if not current_key:
        # 何と比べて「現在地でない」と言うのかが決まらない — 書く側も部屋を
        # 一つも縮めないので、ここも縮めるものなしに倒す。
        return False
    for batch_id, room_state_json in presented:
        _raw, targets, _survivor_paths = _room_shrink_targets(
            batch_id, room_state_json, current_key,
        )
        if targets:
            return True
    return False


def mark_presentation_reductions(
    conn: sqlite3.Connection, model_key: Optional[str] = None,
    *, drop_notices: bool = True,
) -> Dict[str, int]:
    """Metabolism の瞬間に「ここから先は縮める」を永続化する。**commit しない**。

    やることは二つだけで、どちらも記録の**追加**:

    1. **この model の**操作通知の境界を、いま提示に出ている最大のバッチ id まで
       進める。``model_key`` が無い / ``drop_notices=False`` (= その model の
       head を描き直せなかった回) なら進めない。
    2. いま提示に出ているバッチの部屋の記帳のうち、**現在地でない部屋**の
       エントリに「縮めた」印を打つ (:data:`SHRUNK_FLAG`)。印と一緒に、跡地に
       出す一行 (:data:`SHRUNK_BLOCK_FIELD`) と、提示から外すメディアの path
       (:data:`DROPPED_MEDIA_FIELD`) を確定させ、**束 (``snapshot``) を落とす**。
       部屋は head と無関係なので、head を描き直せなかった回でも縮める。

    束を落とすのが「戻ってきたら全文を見せ直す」の実装そのもの: 束の無い記帳は
    連なりの外 (:func:`sai_memory.room_state.is_legacy_entry`) になるので、土台
    探し (``_visible_chain_tail`` / ``latest_visible_snapshot``) も開き直し
    (``_reopen_lost_bases``) もこのエントリを見なくなり、その部屋へ戻った回の
    消費は**土台なし = 全文 + 画像**を積む。連なりの読み手を一枚も書き換えずに
    済むのは、旧形式 (文字列 snapshot) の扱いと同じ道に合流させたため。

    記帳の書き戻しは**読んだ生の並びの上**で行う (篩った結果で上書きしない) —
    未知の要素が黙って消えるのは「記録は追加だけ」に反する。

    現在地が読めない / 台帳に部屋の記録が無い回は部屋を一つも縮めない (何と
    比べて「現在地でない」と言うのかが決まらない — 縮めない側に倒す)。同じ
    バッチに文面が**寸分違わず同じ**部屋が二つあり、片方が提示に残る側 (現在地
    または旧形式) のときも縮めない — 提示の差し替えは確定文面の文字列一致で
    位置を決めるので、残すべき方を縮める形を作らない。読み取り失敗は例外の
    まま送出する: 呼び出し側 (Metabolism) が丸ごと見送る。

    Returns: ``{"notices_through": 進めた境界, "rooms_shrunk": 縮めた部屋の数,
        "batches": 記帳を書き換えたバッチの数}``。
    """
    from sai_memory.perception_buffer import list_presented_batch_room_states
    from sai_memory.room_state import bundle_media, find_current_room_key

    presented = list_presented_batch_room_states(conn)
    if not presented:
        return {"notices_through": get_notice_cutoff(conn, model_key),
                "rooms_shrunk": 0, "batches": 0}

    current_key = find_current_room_key(conn)
    rooms_shrunk = 0
    batches_touched = 0
    if current_key:
        for batch_id, room_state_json in presented:
            raw, targets, survivor_paths = _room_shrink_targets(
                batch_id, room_state_json, current_key,
            )
            if not targets or raw is None:
                continue
            for entry in targets:
                snapshot = entry["snapshot"]
                name = str(
                    snapshot.get("building_name")
                    or snapshot.get("building_id")
                    or entry.get("key") or "?"
                )
                paths = [
                    str(m["path"]) for m in bundle_media(snapshot) if m.get("path")
                ]
                entry[SHRUNK_FLAG] = True
                entry[SHRUNK_BLOCK_FIELD] = shrunk_room_block(name)
                # 同じバッチに残る部屋が同じ絵を持っているなら外さない
                # (絵の持ち主はパッケージで、バッチのメディアは合流した一枚)。
                entry[DROPPED_MEDIA_FIELD] = [
                    p for p in paths if p not in survivor_paths
                ]
                entry.pop("snapshot", None)
            conn.execute(
                "UPDATE perception_batches SET room_state_json = ? WHERE id = ?",
                (json.dumps(raw, ensure_ascii=False), int(batch_id)),
            )
            rooms_shrunk += len(targets)
            batches_touched += 1
    else:
        LOGGER.debug(
            "[presented_reduction] no current room in the ledger; leaving every "
            "room state at full size this metabolism",
        )

    if drop_notices and model_key:
        notices_through = advance_notice_cutoff(
            conn, max(batch_id for batch_id, _json in presented), model_key,
        )
    else:
        notices_through = get_notice_cutoff(conn, model_key)
        LOGGER.debug(
            "[presented_reduction] leaving the operation notices in the "
            "presentation (model=%s, head redrawn=%s); the boundary stays at %d",
            model_key, drop_notices, notices_through,
        )
    if rooms_shrunk or notices_through:
        LOGGER.info(
            "[presented_reduction] metabolism reduction (model=%s): notices "
            "dropped through batch %d, %d room state(s) shrunk in %d batch(es) "
            "(current room=%s)",
            model_key, notices_through, rooms_shrunk, batches_touched,
            current_key,
        )
    return {
        "notices_through": notices_through,
        "rooms_shrunk": rooms_shrunk,
        "batches": batches_touched,
    }


# ---------------------------------------------------------------------------
# 提示を組むときの適用 (確定文面の写しの上だけ)
# ---------------------------------------------------------------------------

def _block_boundaries(text: str, block: str) -> Optional[int]:
    """``block`` がブロックの区切り (先頭 or 直前が空行) に立つ位置を返す。

    確定文面は「見出し + 本文」のブロックを ``\\n\\n`` で繋いだもの
    (:func:`sai_memory.perception_buffer.format_perception_message`)。区切りを
    確かめてから外すことで、たまたま別のブロックの中に同じ文字列が現れた回に
    途中を切り取ってしまう形を作らない。
    """
    start = 0
    while True:
        found = text.find(block, start)
        if found < 0:
            return None
        if found == 0 or text[found - 2:found] == "\n\n":
            return found
        start = found + 1


def _remove_block(text: str, block: str) -> Optional[str]:
    """確定文面から 1 ブロックを区切りごと外す (見つからなければ None)。"""
    found = _block_boundaries(text, block)
    if found is None:
        return None
    end = found + len(block)
    if text[end:end + 2] == "\n\n":
        end += 2
    elif found >= 2:
        found -= 2
    return text[:found] + text[end:]


def _replace_block(text: str, block: str, replacement: str) -> Optional[str]:
    """確定文面の 1 ブロックを別の文面へ差し替える (見つからなければ None)。"""
    found = _block_boundaries(text, block)
    if found is None:
        return None
    return text[:found] + replacement + text[found + len(block):]


def _notice_blocks_by_batch(
    conn: sqlite3.Connection, batch_ids: Sequence[int],
) -> Dict[int, List[str]]:
    """バッチごとの「下ろす操作通知の確定文面中のブロック」を発生順で返す。

    台帳の行 (``kind`` / ``content``) から、消費のときと**同じ組み立て**
    (:func:`sai_memory.perception_buffer.perception_block_text`) でブロックを
    復元する — 確定文面を区切りで割って型を推測する形は採らない (文字列の解析で
    差分を組んで v0.3.9 の出荷を止めた欠陥と同じ道)。reduce で畳まれて文面に
    出なかった行は、後段の照合 (ブロックが確定文面に無い) で自然に外れる。

    読み取りに失敗した回は WARN + そのバッチは「外すものなし」に倒す — 提示が
    一時的に縮まないだけで、送るものが欠けることはない。
    """
    from sai_memory.perception_buffer import perception_block_text

    out: Dict[int, List[str]] = {}
    ids = [int(b) for b in batch_ids]
    if not ids:
        return out
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                "SELECT consumed_batch_id, kind, content, metadata "
                f"FROM perception_buffer WHERE consumed_batch_id IN ({placeholders}) "
                "ORDER BY created_at ASC, id ASC",
                tuple(chunk),
            ).fetchall()
        except sqlite3.Error:
            # OperationalError に限らず DB エラーの族ごと受ける — ここで漏らすと
            # 呼び出し元の広い受け (list_presented_perception_blocks) が知覚を
            # 丸ごと空にする。節約の失敗が知覚の喪失に化ける向きは作らない
            # (ローカルレビュー指摘 2026-09-10)。
            LOGGER.warning(
                "[presented_reduction] could not read the ledger rows behind the "
                "presented batches; leaving their operation notices in the "
                "presentation this round", exc_info=True,
            )
            rows = []
        for batch_id, kind, content, metadata in rows:
            if batch_id is None or not content:
                continue
            if not is_droppable_notice(kind, metadata):
                continue
            out.setdefault(int(batch_id), []).append(
                perception_block_text(str(kind), str(content))
            )
    return out


def _reduce_rooms(
    batch: Any, text: str, media: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]], bool]:
    """このバッチの「縮めた」印が付いた部屋を、短い一行と画像なしへ写す。

    **文字と絵ははぐれない** (room_state_packages §1): 画像を提示から外すのは
    本文の差し替えに成功したエントリだけ。差し替えに失敗した回 (確定文面に
    そのブロックが見つからない) に画像だけ外すと、本文には部屋の様子が全文で
    残ったまま絵が消え、省略の表示も出ない — 「そこに絵は無かった」という
    記録の嘘になる (ローカルレビュー指摘 2026-09-10)。
    """
    from sai_memory.room_state import batch_room_states

    changed = False
    for entry in batch_room_states(batch.room_state_json):
        if not entry.get(SHRUNK_FLAG):
            continue
        block = entry.get("block") or ""
        replacement = entry.get(SHRUNK_BLOCK_FIELD) or ""
        replaced = (
            _replace_block(text, block, replacement)
            if block and replacement else None
        )
        if replaced is None:
            LOGGER.warning(
                "[presented_reduction] the shrunk room block is not in the "
                "rendered text of batch %s (key=%s); leaving the room at full "
                "size this round (its media stays too)",
                batch.id, entry.get("key"),
            )
            continue
        text = replaced
        changed = True
        dropped = {str(p) for p in (entry.get(DROPPED_MEDIA_FIELD) or []) if p}
        if dropped and media:
            kept = [
                m for m in media
                if not (isinstance(m, dict) and str(m.get("path") or "") in dropped)
            ]
            if len(kept) != len(media):
                media = kept
    return text, media, changed


def reduce_presented_batches(
    conn: sqlite3.Connection, batches: Sequence[Any],
    model_key: Optional[str] = None,
) -> List[Any]:
    """提示に出るバッチへ、Metabolism が決めた縮みを適用した写しを返す。

    **確定文面 (``perception_batches.rendered_text``) も台帳も書き換えない** —
    返るのは ``dataclasses.replace`` した写しで、送る側 (prepare_context) と
    測る側 (水位の勘定・退場計画) は同じこの一枚を通る
    (sea/runtime_context.list_presented_perception_blocks の候補取得の直後)。
    確定文面をそのままにするのは、下ろされた期間の編纂の材料と読み口がそこを
    読むため (intent 不変条件 1「記録は消さない」)。

    縮み方は二つ:

    - **操作通知**: ``model_key`` の境界 (:func:`get_notice_cutoff`) 以下の
      バッチから、対象の通知ブロックを外し、跡地に機構名義の一行を置く。
      **境界は model ごと** — 別の model の Metabolism では下りない (その model の
      head はまだ凍結されていて、通知が唯一の情報源だから)。``model_key`` の
      無い呼び出しは通知を一つも下ろさない (全部見せる安全側)。
    - **部屋の様子**: 「縮めた」印の付いたエントリを短い一行へ差し替え、その
      部屋の画像を提示のメディアから外す。印はペルソナ共通なので model に依らない
      (部屋の様子は head に載らないため)。

    どちらも判断は Metabolism が既に確定させたもので、ここでは**読むだけ** —
    だから提示は Metabolism 以外の瞬間に変わらない。

    既知の境界: 台帳の行が読めない回は、そのバッチの操作通知が一時的に提示へ
    戻る (:func:`_notice_blocks_by_batch` の WARN)。読めないまま知覚を丸ごと
    落とす (組成の fail-open) よりは、通知が一枚多い方が軽い。
    """
    if not batches:
        return list(batches)
    cutoff = get_notice_cutoff(conn, model_key)
    notice_ids = [
        int(b.id) for b in batches
        if getattr(b, "id", None) is not None and int(b.id) <= cutoff
    ]
    notices = _notice_blocks_by_batch(conn, notice_ids) if notice_ids else {}

    out: List[Any] = []
    for batch in batches:
        text = batch.rendered_text or ""
        media = [dict(m) for m in batch.media_list()]
        text, media, changed = _reduce_rooms(batch, text, media)

        blocks = notices.get(int(batch.id)) if getattr(batch, "id", None) else None
        if blocks:
            present = [b for b in blocks if _block_boundaries(text, b) is not None]
            if present:
                mark = notice_omission_block(len(present))
                replaced = _replace_block(text, present[0], mark)
                if replaced is not None:
                    text = replaced
                    for block in present[1:]:
                        removed = _remove_block(text, block)
                        if removed is not None:
                            text = removed
                    changed = True

        if not changed:
            out.append(batch)
            continue
        out.append(dataclasses.replace(
            batch,
            rendered_text=text,
            media=json.dumps(media, ensure_ascii=False) if media else None,
        ))
    return out
