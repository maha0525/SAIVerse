"""部屋の様子 (room state) — 再訪は差分だけ積み、全文は移管で引き継ぐ。

移動のたびに「移動先の様子」(居合わせる相手・内装・全アイテムの説明つき全文) を
知覚台帳へ積むので、同じ部屋を行き来するだけで同じ全文が提示に何枚も並ぶ
(2026-09-04 まはー裁定の発端。実測では 1 枚が 1 万字規模)。ここはその重複を、
**提示列の途中を書き換えずに**消すための一式:

- **再訪は差分だけ**: 同じ部屋の直近のエントリがまだ提示に見えている
  (未消費の pending か、提示に出るバッチ) なら、今回積むのは前回全文との差分
  だけ。見えていなければ従来どおり全文を積む (初訪問・久しぶりの再訪)。
- **土台の回復**: 差分が土台にするのは「同部屋の**直前の**エントリの全文」で
  あって、提示に残っている最古の全文ではない。だから不変条件はこう書く —
  **提示に見えているどの差分も、自分が土台にした全文がその直前に見えている**。
  土台が提示から下りたら、その差分の提示文面をスナップショット (その時点の
  全文) へ差し替えて、連なりの切れ目を塞ぐ。
- **可視性が変わる四つの瞬間すべてで、この不変条件を通す**:

  1. 編纂の退場付記で下りるとき —
     :func:`sai_memory.perception_buffer.mark_batches_annexed` が付記と**同一
     トランザクション**で :func:`restore_room_state_bases` を呼ぶ。
  2. 知覚の合計上限で古い側をまとめて下ろすとき —
     :func:`sai_memory.perception_buffer.advance_presentation_cutoff` が境界の
     前進と同一トランザクションで同じ回復を呼ぶ (perception_buffer.md §10.9)。
  3. 消費で出るとき — :func:`ensure_room_state_base`。積んでから消費するまでの
     間に土台が見えなくなることがある (移動 → その Pulse 末の Metabolism →
     次の Beat 頭で消費)。この差分はまだバッチになっていないので回復の
     受け皿に入れず、レンダリングの写しを全文へ開き直す。
  4. Chronicle 無効のペルソナの窓絞りで下りるとき —
     :func:`reopen_lost_bases`。無効のペルソナは提示窓 (anchor) より古い
     バッチを付記なしで忘れる (perception_buffer.md §10.3 の例外) ので、
     台帳側の回復 (1・2) は「土台はまだ見えている」と読んで走らない。絞りは
     DB に書ける事実ではない (窓はペルソナと model ごとに動く) ので、ここだけ
     **提示時の開き直し** — 台帳も確定文面も触らず、その回の提示文面だけを
     差し替える。同じ並びからは必ず同じ文面になる決定論なので、提示が呼び
     出しごとに揺れることはない。

- **書き換えの時点を増やさない**: 回復は必ず上の書き込み点に相乗りする。提示が
  変わる瞬間は既にプロンプトキャッシュの前方一致が割れている場所なので、
  そこへ寄せる (2026-09-04 まはー裁定「最新だけ残す型の置き換えは禁止」の
  理由そのもの)。回復だけを単独で走らせてはいけない。
- **台帳は書き換えない**: 回復は「提示上の内容の引っ越し」で、元の差分の文面は
  ``perception_buffer`` の行にそのまま残る。書き換わるのは提示の正準
  (``perception_batches.rendered_text``) と、その相棒の
  ``perception_batches.room_state_json`` だけ。

``room_state_json`` はバッチ 1 件に含まれる部屋の様子エントリの並び (時刻順):

    [{"key": "building:b1", "is_diff": true,
      "block": "<rendered_text 中のこのエントリの文面>",
      "snapshot": "<その時点の部屋の全文>",
      "base_digest": "<土台にした全文の指紋 (差分エントリのみ)>"}]

``snapshot`` は差分エントリにも必ず入る (差分の土台であり、回復で差し込む本文)。
``base_digest`` は「この差分がどの全文の上に積まれたか」の指紋
(:func:`snapshot_digest`) で、直前のエントリが本当にその土台かを照合するために
持つ — 全文をもう一枚持つと 1 万字級の重複になるので、指紋だけを置く。

既知の境界 (2026-09-05 時点):

- **差分に添付メディアは載せない**。内装画像・相手の外見画像は、土台になって
  いる全文エントリと一緒にまだ提示されている。新しく現れたアイテムの画像は
  差分の本文に ``saiverse://item/N/image`` として名前だけ載り、実体は次の
  Metabolism の head (visual_context) で入る。再訪のたびに全画像を積み直すのが
  重複の最大の実費なので、ここは削る側に倒している。
- **Chronicle 無効のペルソナには差分を積まない** (呼び出し側が ``allow_diff``
  で渡す)。無効のペルソナは提示窓 (anchor) でバッチを忘れるので、土台の全文が
  付記なしで見えなくなりうる — 台帳側の回復は付記にしか相乗りできないため、
  土台を失った差分が残る。従来どおり毎回全文を積む。**トグルを有効から無効へ
  切り替えた後**は、有効だった間に積んだ差分が台帳に残っているので、この門
  だけでは足りない — 提示時の開き直し (:func:`reopen_lost_bases`) が窓絞りで
  底が抜けた差分を受け止める。
- **付記の取り消し** (Chronicle エントリ削除 →
  :func:`~sai_memory.perception_buffer.unmark_batches_annexed`) で戻ってきた
  全文バッチと、開き直し済みのバッチが同時に提示に並ぶことがある。不変条件は
  保たれ、失われるものも無い — 全文が二枚並ぶ冗長だけが残る。
- **``base_digest`` を持たない旧バッチ**は指紋で照合できないので、そこだけ旧
  規則 (同部屋のエントリが手前に見えていれば土台ありとみなす) で扱う
  (:func:`chain_is_intact`)。中間の一枚だけが下りた形は旧データでは検出でき
  ないが、旧規則のままなので退行はしない。新しく積む差分には必ず指紋が入る。
"""
from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import logging
import sqlite3
from typing import Any, Dict, List, Mapping, Optional, Sequence

LOGGER = logging.getLogger(__name__)

#: 部屋の様子を積む知覚の型 (``perception_buffer.kind``)。見出しは付かない
#: (``_KIND_HEADERS`` で "" — 本文が ``# 「X」の様子`` から始まり自己完結する)。
ROOM_STATE_KIND = "surroundings"

#: 知覚台帳の ``metadata`` に載せるキー。
ROOM_STATE_META_KEY = "room_state"

_DIFF_TITLE_SUFFIX = " (前回見たときからの変化)"
_NO_CHANGE_LINE = "前回見たときから変わっていません。"
_ADDED_HEADING = "## 増えた・変わったもの"
_GONE_HEADING = "## 見当たらなくなったもの"
_TAIL_LINE = "これ以外は前回と同じです。"
_FALLBACK_TITLE = "# 部屋の様子"


def room_key(building_id: str) -> str:
    """同じ部屋かどうかの判定キー。

    Building が部屋の同一性の単位 (移動先の様子は Building 単位で作られる)。
    将来 world_state の他の型へ広げるときのために接頭辞を付けておく。
    """
    return f"building:{building_id}"


# ---------------------------------------------------------------------------
# 差分の組み立て
# ---------------------------------------------------------------------------

def _split_blocks(text: str) -> List[str]:
    """空行で区切られた「かたまり」の list にする。

    ``get_visual_context(for_perception=True)`` の出力はアイテム 1 件・見出し
    1 つがそれぞれ空行で区切られるので、かたまり単位の比較がそのまま
    「変わった項目だけ」になる。
    """
    blocks: List[str] = []
    current: List[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(line)
        elif current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def _first_line(block: str) -> str:
    for line in block.splitlines():
        if line.strip():
            return line.strip()
    return block.strip()


def _title_line(full_text: str) -> str:
    """全文の見出し行 (``# 「工房」の様子``) を取り出す。無ければ汎用見出し。"""
    for line in full_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped if stripped.startswith("#") else _FALLBACK_TITLE
    return _FALLBACK_TITLE


def render_room_diff(old_full: str, new_full: str) -> str:
    """前回の全文と今回の全文から、積む差分の本文を作る (決定論)。

    かたまり単位で比較し、増えた/変わったものは**全文のまま**、見当たらなく
    なったものは見出し行だけを出す (消えたものの説明はもう要らない)。変化が
    無ければ「変わっていません」の一行に畳む — 積まないのではなく最小限を積む
    のは、土台が付記で下りたときに移管の受け皿が残るようにするため。
    """
    old_blocks = [b for b in _split_blocks(old_full) if b.strip() != "---"]
    new_blocks = [b for b in _split_blocks(new_full) if b.strip() != "---"]

    matcher = difflib.SequenceMatcher(a=old_blocks, b=new_blocks, autojunk=False)
    removed: List[str] = []
    added: List[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("delete", "replace"):
            removed.extend(old_blocks[i1:i2])
        if tag in ("insert", "replace"):
            added.extend(new_blocks[j1:j2])

    # 見出し行が一致する組は「同じものが書き変わった」— 新しい姿だけを出し、
    # 「見当たらなくなった」側には出さない (同じ名前が消えて増えた、に見せない)。
    added_heads: Dict[str, int] = {}
    for block in added:
        head = _first_line(block)
        added_heads[head] = added_heads.get(head, 0) + 1
    gone: List[str] = []
    for block in removed:
        head = _first_line(block)
        if added_heads.get(head):
            added_heads[head] -= 1
            continue
        gone.append(head)

    title = _title_line(new_full)
    if not added and not gone:
        return f"{title}\n{_NO_CHANGE_LINE}"

    parts: List[str] = [title + _DIFF_TITLE_SUFFIX, ""]
    if added:
        parts.append(_ADDED_HEADING)
        parts.append("")
        for block in added:
            parts.append(block)
            parts.append("")
    if gone:
        parts.append(_GONE_HEADING)
        for head in gone:
            parts.append(f"- {head}")
        parts.append("")
    parts.append(_TAIL_LINE)
    return "\n".join(parts)


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


def snapshot_digest(full_text: str) -> str:
    """部屋の全文の指紋 (差分がどの土台の上に積まれたかの照合用)。

    照合にしか使わないので中身は要らない — 全文をもう一枚記帳すると 1 万字級の
    重複になる。衝突耐性のある短い固定長で足りる。
    """
    return hashlib.sha256(full_text.encode("utf-8")).hexdigest()


def chain_is_intact(
    entry: Mapping[str, Any], previous: Optional[Mapping[str, Any]],
) -> bool:
    """差分 ``entry`` の土台が ``previous`` として今も見えているか。

    差分は「同部屋の**直前**のエントリの全文」から作る (提示に残っている最古の
    全文からではない)。だから連なりの検査は、積むときに記帳した土台の指紋
    (``base_digest``) と、直前に見えているエントリの ``snapshot`` の指紋が一致
    するかで行う — 中間の一枚だけが提示から下りた形もこれで捕まる。

    ``previous`` が None (この提示でこの部屋の最初のエントリ) なら常に False。
    土台が一枚も見えていないので全文へ開き直す。

    **``base_digest`` を持たない旧エントリ**は照合できないので、そこだけ旧規則
    (同部屋のエントリが手前に見えていれば土台ありとみなす) へ倒す。旧データで
    中間欠落を検出できないのは既知の境界で、旧規則のままなので退行はしない。
    ``previous`` が ``snapshot`` を持たない壊れた記帳は照合不能 = False (全文へ
    開き直す側に倒す — 余分な全文一枚は無害、土台の無い差分は復元不能)。
    """
    if previous is None:
        return False
    base = entry.get("base_digest")
    if not base:
        return True  # 旧データ: 手前に同部屋エントリが見えている = 旧規則で土台あり
    snapshot = previous.get("snapshot")
    if not snapshot:
        return False
    return snapshot_digest(str(snapshot)) == str(base)


def batch_room_states(room_state_json: Optional[str]) -> List[Dict[str, Any]]:
    """バッチの ``room_state_json`` を list に復元する。壊れていれば空 list。"""
    if not room_state_json:
        return []
    try:
        data = json.loads(room_state_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("key")]


def latest_visible_snapshot(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """いま提示に見えている (or 次の消費で見える) 同部屋エントリの最新スナップショット。

    見つからなければ None = 「土台が無いので全文を積む」。順序は
    「未消費 (pending) → 提示に出るバッチの新しい順」— pending は必ず最後の消費
    より後に積まれたので、あればそれが最新。バッチ側で見るのは**提示に出る**
    ものだけ (未付記かつ知覚の合計上限で下ろした境界より新しい) — 下ろされた
    バッチはもう見えないので、土台にすると差分が宙に浮く。

    読み取りに失敗したら None (= 全文を積む) に倒す。**pending の読み取りが
    落ちたらバッチ側へ進まない** — pending の方が新しいので、そこを空と見なして
    バッチ側の古いスナップショットを土台にすると、実際とは違う土台に対する
    差分を積むことになる (「読み取り失敗を 0 件に化かす」型)。全文を積み直す
    冗長は無害だが、間違った土台の差分は復元不能。
    """
    from sai_memory.perception_buffer import list_presented_batches

    try:
        rows = conn.execute(
            "SELECT metadata FROM perception_buffer "
            "WHERE consumed_at IS NULL AND kind = ? "
            "ORDER BY created_at DESC, id DESC",
            (ROOM_STATE_KIND,),
        ).fetchall()
    except sqlite3.OperationalError:
        LOGGER.warning(
            "[room_state] could not read pending perceptions while looking for "
            "a diff base (key=%s); pushing the full room text instead", key,
            exc_info=True,
        )
        return None
    for (metadata,) in rows:
        state = _parse_item_state(metadata)
        if state and state.get("key") == key:
            snapshot = state.get("snapshot")
            return str(snapshot) if snapshot else None

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
        for entry in reversed(batch_room_states(batch.room_state_json)):
            if entry.get("key") == key:
                snapshot = entry.get("snapshot")
                return str(snapshot) if snapshot else None
    return None


# ---------------------------------------------------------------------------
# 積む側 (移動の入室フック)
# ---------------------------------------------------------------------------

def build_room_state_push(
    conn: sqlite3.Connection,
    building_id: str,
    full_text: str,
    *,
    media: Optional[list] = None,
    allow_diff: bool = True,
) -> Dict[str, Any]:
    """入室時に積む「部屋の様子」の本文・メディア・記帳を決める。

    Returns:
        ``{"content", "media", "metadata"}`` — そのまま
        :func:`sai_memory.perception_buffer.push_perception` に渡せる形。
        ``metadata`` は :data:`ROOM_STATE_META_KEY` の記帳を含む JSON 文字列で、
        消費時に :func:`collect_batch_room_states` がバッチ側へ写す。
    """
    key = room_key(building_id)
    base = latest_visible_snapshot(conn, key) if allow_diff else None
    state: Dict[str, Any] = {"key": key, "is_diff": False, "snapshot": full_text}
    if base is None:
        content = full_text
        out_media = media
    else:
        content = render_room_diff(base, full_text)
        state["is_diff"] = True
        # どの全文の上に積んだか (指紋)。土台がまだ直前に見えているかは、この
        # 指紋と直前エントリの snapshot を突き合わせて判定する。
        state["base_digest"] = snapshot_digest(base)
        # 差分にメディアは載せない (モジュール docstring「既知の境界」)。
        out_media = None
    metadata = json.dumps({ROOM_STATE_META_KEY: state}, ensure_ascii=False)
    return {"content": content, "media": out_media, "metadata": metadata}


# ---------------------------------------------------------------------------
# 消費側 (バッチ確定時の記帳)
# ---------------------------------------------------------------------------

def _visible_chain_tail(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """部屋ごとの「提示に出るバッチで最後に見えているエントリ」。

    次に消費される差分の土台は、この末尾のエントリでなければならない
    (:func:`chain_is_intact` が指紋で照合する)。キーが見えているかどうかだけを
    見ていた頃は、土台が中間で下りていても「土台あり」と読んでいた。

    読み取りに失敗したら「土台なし」(空 dict) に倒す — 呼び出し側
    (:func:`ensure_room_state_base`) はこの消費の差分を全文へ開き直すので、
    全文が二枚並ぶ冗長は出るが失われるものは無い。逆に「土台あり」へ倒すと、
    土台の無い差分がそのまま確定して復元不能になる。
    """
    from sai_memory.perception_buffer import list_presented_batches

    try:
        batches = list_presented_batches(conn)
    except sqlite3.OperationalError:
        LOGGER.warning(
            "[room_state] could not read the presented batches while checking "
            "for visible bases; reopening this consumption's diffs to their "
            "full room text", exc_info=True,
        )
        return {}
    tail: Dict[str, Dict[str, Any]] = {}
    for batch in batches:
        for entry in batch_room_states(batch.room_state_json):
            tail[str(entry["key"])] = entry
    return tail


def ensure_room_state_base(
    conn: sqlite3.Connection, items: Sequence[Any],
) -> List[Any]:
    """土台を失った差分を、提示が確定する前に全文へ開き直す。

    積んだ時点では土台 (同部屋の直前の全文) が見えていても、消費されるまでの
    間に編纂の付記でそれが提示から下りることがある — 移動でこの部屋の差分を
    積む → その Pulse の末尾で Metabolism が走り、古い方のバッチを付記する →
    次の Beat 頭でこの差分が消費される、の順。回復は付記の時点に居る未付記
    バッチしか受け皿にできないので、まだ台帳で待っていたこの差分は宙に浮く。
    ここでスナップショット (その時点の全文) へ戻す。

    判定はバッチ側と同じ :func:`chain_is_intact` — 提示に出るバッチの末尾
    エントリ (:func:`_visible_chain_tail`) から連なりを続け、この消費の中の
    エントリを順に土台として繋いでいく。同部屋のキーが見えているかどうかだけ
    を見ると、土台が中間で下りた形を「土台あり」と読んでしまう。

    書き換えるのは**この消費でレンダリングに使う写しだけ** — 台帳の行
    (``perception_buffer``) は積んだときの差分のまま残る。返るのは ``items`` と
    同じ並び・同じ長さの list。
    """
    room_positions = [
        (index, state)
        for index, item in enumerate(items)
        if (state := _parse_item_state(getattr(item, "metadata", None)))
        and state.get("key")
    ]
    if not room_positions:
        return list(items)

    previous_by_key = _visible_chain_tail(conn)
    out = list(items)
    for index, state in room_positions:
        key = str(state["key"])
        previous = previous_by_key.get(key)
        previous_by_key[key] = state
        if not state.get("is_diff"):
            continue  # 全文はそれ自体が土台 = 連なりはここから始め直す
        if chain_is_intact(state, previous):
            continue
        snapshot = state.get("snapshot")
        if not snapshot:
            LOGGER.warning(
                "[room_state] a diff lost its base before consumption and has "
                "no snapshot to reopen (key=%s); presenting it as it is", key,
            )
            continue
        reopened = dict(state)
        reopened["is_diff"] = False
        reopened["reopened"] = True
        out[index] = dataclasses.replace(
            out[index],
            content=str(snapshot),
            metadata=json.dumps(
                {ROOM_STATE_META_KEY: reopened}, ensure_ascii=False,
            ),
        )
        LOGGER.info(
            "[room_state] the base of a pending room diff was annexed before "
            "consumption (key=%s); presenting the full room text instead", key,
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

    差分エントリには ``base_digest`` (どの全文の上に積んだかの指紋) も写す —
    土台がまだ直前に見えているかの照合に要る。全文へ開き直したエントリには
    載せない (もう誰かの上に積まれてはいない)。
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
        snapshot = state.get("snapshot") or block
        is_diff = bool(state.get("is_diff"))
        entry: Dict[str, Any] = {
            "key": str(state["key"]),
            "is_diff": is_diff,
            "block": block,
            "snapshot": str(snapshot),
        }
        if is_diff and state.get("base_digest"):
            entry["base_digest"] = str(state["base_digest"])
        if state.get("reopened"):
            # 消費の直前に土台を失って全文へ開き直した印 (診断用)。
            entry["reopened"] = True
        entries.append(entry)
    if not entries:
        return None
    return json.dumps(entries, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 土台の回復 (付記・境界前進と同一トランザクション)
# ---------------------------------------------------------------------------

def _reopen_lost_bases(batches: Sequence[Any]) -> Dict[int, tuple]:
    """土台を失った差分を全文へ開き直した ``(文面, 記帳)`` をバッチ id ごとに返す。

    **純関数** — DB を読み書きせず、``batches`` の並びだけから答えが決まる。
    同じ並びを二度渡せば必ず同じ結果になる (提示時の開き直しがこの決定論に
    寄りかかっている: 呼び出しのたびに提示文面が揺れると、プロンプトキャッシュ
    の前方一致が新着なしで割れる)。

    走査は部屋ごとの連なり: 古い順に辿り、差分エントリの土台
    (:func:`chain_is_intact`) が直前に見えていなければ、その位置の文面を
    スナップショット (その時点の全文) へ差し替える。差し替えても
    ``snapshot`` は変わらないので、次のエントリの土台判定は同じエントリを
    そのまま指してよい。

    返るのは**変わったバッチだけ**の ``{batch.id: (rendered_text, entries)}``。
    ``entries`` は差し替え済みの記帳 (``block`` が全文へ、``is_diff`` が False、
    ``transferred`` の印つき) で、台帳へ書き戻す呼び出し
    (:func:`restore_room_state_bases`) だけが使う。
    """
    previous_by_key: Dict[str, Dict[str, Any]] = {}
    out: Dict[int, tuple] = {}
    for batch in batches:
        entries = batch_room_states(batch.room_state_json)
        if not entries:
            continue
        rendered = batch.rendered_text or ""
        changed = False
        for entry in entries:
            key = str(entry["key"])
            previous = previous_by_key.get(key)
            # 差し替えても snapshot は変わらないので、次のエントリの土台判定は
            # この entry (同じ dict) をそのまま指してよい。
            previous_by_key[key] = entry
            if not entry.get("is_diff"):
                continue  # 全文はそれ自体が土台 = 連なりはここから始め直す
            if chain_is_intact(entry, previous):
                continue
            block = entry.get("block") or ""
            snapshot = entry.get("snapshot") or ""
            if not block or not snapshot or block not in rendered:
                LOGGER.warning(
                    "[room_state] cannot reopen the full room text in "
                    "batch %s (key=%s): the recorded block is not in the "
                    "rendered text", batch.id, key,
                )
                continue
            rendered = rendered.replace(block, snapshot, 1)
            entry["block"] = snapshot
            entry["is_diff"] = False
            entry["transferred"] = True
            changed = True
        if changed:
            out[int(batch.id)] = (rendered, entries)
    return out


def reopen_lost_bases(batches: Sequence[Any]) -> Dict[int, str]:
    """この並びを**そのまま提示する**ときの、開き直し後の文面 (変わった分だけ)。

    台帳も確定文面も書き換えない — 返るのは ``{batch.id: rendered_text}`` で、
    呼び出し側 (:func:`sea.runtime_context.list_presented_perception_blocks`)
    がブロックを組むときに差し替える。

    要るのは、**可視性が DB に書けない形で狭まる**ときのため: Chronicle 無効の
    ペルソナは提示窓 (anchor) より古いバッチを付記なしで忘れるので、台帳側の
    回復 (:func:`restore_room_state_bases`) は「土台はまだ見えている」と読んで
    走らない。窓はペルソナと model ごとに動くので、その絞りを台帳へ書き戻す
    ことはできない。同じ理由で、**下ろし境界を進めずに測るだけの呼び出し**
    (context-status などの読み取り専用の画面) も、進めた**つもり**の並びを
    ここへ通して勘定を実送信と一致させる。

    Chronicle 有効で境界の前進も済んだ並びは台帳の
    :func:`sai_memory.perception_buffer.list_presented_batches` と一致するので、
    ここは空 dict を返す (台帳側の回復が既に不変条件を保っている)。台帳側の
    回復がまだ一度も届いていない並び (旧データの取り込み直後など) では、ここが
    安全網として同じ開き直しを提示にだけ効かせる — 下ろし量の見積もり
    (:func:`sea.runtime_context._perception_suffix_totals`) は元からその膨らみを
    織り込んでいるので、これで勘定と提示が揃う。
    """
    return {
        batch_id: rendered
        for batch_id, (rendered, _entries) in _reopen_lost_bases(batches).items()
    }


def restore_room_state_bases(conn: sqlite3.Connection) -> int:
    """不変条件「どの差分も自分の土台が直前に見えている」を回復する。**commit しない**。

    提示に出るバッチを古い順に走査し、部屋ごとに連なりを辿る。差分エントリの
    土台 (:func:`chain_is_intact`) が直前に見えていなければ、その位置の文面を
    スナップショット (その時点の全文) へ差し替える。

    **最古だけを見ない**のがここの要点 (2026-09-05 Codex 三巡 #1): 差分は直前の
    エントリに依存するので、「A の全文 → B 追加 → C 追加」の**中間の B だけ**が
    提示から下りると、最古 (A) は全文のままなのに C の土台 (B 時点の全文) が
    失われる。付記は期間指定なので中間区間だけを下ろせるし、境界の前進も
    (A が既に下りていれば) 同じ形を作れる。連なりの検査は部屋ごとに一本ずつ
    前へ進み、切れた位置をその場で全文へ開き直す。

    呼ぶのは**可視性が変わる二つの書き込み点**だけで、単独では走らせない
    (提示の書き換え時点を増やさないため):

    - :func:`sai_memory.perception_buffer.mark_batches_annexed` — 付記が 1 行
      でも立った tx の中 (編纂で全文バッチが下りる瞬間)。
    - :func:`sai_memory.perception_buffer.advance_presentation_cutoff` — 知覚の
      合計上限で古い側をまとめて下ろした tx の中。境界が全文バッチを越えて
      進むと、残った差分が土台を失うため (§10.9)。

    **読み取りに失敗したら例外をそのまま送出する** (「回復対象なし」に化かさない)。
    ここを 0 件で返すと、呼び出し側は回復が済んだ場合と区別できないまま付記や
    境界の前進を commit してしまい、「土台の全文バッチだけが提示から下り、
    残った差分が宙に浮く」壊れ方が確定する。呼び出し側は例外を受けたら tx ごと
    rollback して、付記も境界前進も見送る (次の機会に全体をやり直す)。

    Returns:
        文面を差し替えたバッチの件数。
    """
    from sai_memory.perception_buffer import list_presented_batches

    repaired = _reopen_lost_bases(list_presented_batches(conn))
    for batch_id, (rendered, entries) in repaired.items():
        conn.execute(
            "UPDATE perception_batches SET rendered_text = ?, "
            "room_state_json = ? WHERE id = ?",
            (rendered, json.dumps(entries, ensure_ascii=False), batch_id),
        )
        LOGGER.info(
            "[room_state] reopened a room diff to its full text in batch %s "
            "(its base is no longer presented right before it)", batch_id,
        )
    return len(repaired)
