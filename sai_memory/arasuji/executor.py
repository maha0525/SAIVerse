"""Chronicle chunk executor (arasuji_levels.md — レベル0 の畳みの実行).

alignment.plan_alignment が作った AlignmentPlan を実行し、一次あらすじを
チャンク単位の単一トランザクションで確定する。設計正典は
docs/intent/arasuji_levels.md (旧: 2026-07-21 W4 handoff D4)。

原子性の契約 (M2 の解の生成側):

- チャンク 1 個 = 単一 tx (重複再検査 → INSERT → commit)。途中失敗しても
  確定済みチャンクはそのまま残り、source_ids の重複検査と processed_ids の
  再計算により再試行で二重生成されない。
- LLM 呼び出しは tx の外 (応答を得てから書く)。
- 失敗はチャンク境界で例外として上げる — 呼び出し側 (generate_chronicle)
  が status="failed" に写像し、anchor は据え置かれる (S2 ゲート)。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from sai_memory.arasuji.alignment import (
    AlignmentPlan,
    PlannedChunk,
)
from sai_memory.arasuji.storage import ArasujiEntry, create_entry

LOGGER = logging.getLogger(__name__)


class ChunkExecutionError(RuntimeError):
    """チャンク実行の失敗 (LLM 応答空・保存失敗など LLMError 以外の失敗)。"""


# ---------------------------------------------------------------------------
# 知覚・通知の退場付記 (W14 知覚レンダリング, perception_buffer.md §10.4)
#
# fold (退場) が範囲を畳むとき、その期間の**未付記の消費バッチ** (知覚台帳) と
# legacy の event_message 行を、digest テキストへ**決定論で** (LLM なしで) 添え、
# バッチには digest 確定と**同一トランザクション**で付記印 (annexed_entry_id) を
# 打つ。提示 (runtime_context) はこの印だけを見てバッチを下ろす — 「付記済みか」
# を時刻の区間演算から再構成しない。これで「退場したものは必ず編纂されている」
# の下限 (experience_structure §4-1) が知覚に通る。編纂 LLM の材料には混ぜない —
# LLM 応答を得た後に転写する。tx が rollback すれば印も戻り、バッチは未付記 =
# 提示に残る (fail-open)。
# ---------------------------------------------------------------------------

#: 恒等転写の上限 (合計文字数)。これ以下なら全文をそのまま転写し (圧縮しない —
#: 恒等圧縮の原則「小さいものは潰さない」。前例は Track Chronicle の 1000 字
#: スキップ = experience_structure.md §4-3/§9)、超えたら kind 別の件数集約
#: (day_report の「先頭数件 + ほか N 件」型) に落とす。
ANNEX_IDENTITY_MAX_CHARS = 1000

#: 集約時に kind ごとへ逐語で残す先頭件数と、1 件あたりの行数・行の冒頭字数。
_ANNEX_HEAD_ITEMS = 3
_ANNEX_HEAD_LINES = 3
_ANNEX_HEAD_CHARS = 80


def _known_kind_headers() -> set:
    """flush 整形が本文に挿す機構見出しの正準集合。

    perception_buffer._KIND_HEADERS (+ 既定見出し) が唯一の供給源 — 見出しの
    判定をここで別リストに写すと、kind が増えた瞬間にズレる。
    """
    from sai_memory.perception_buffer import _DEFAULT_HEADER, _KIND_HEADERS
    headers = {h for h in _KIND_HEADERS.values() if h}
    headers.add(_DEFAULT_HEADER)
    return headers


def _annex_meaningful_lines(text: str) -> List[str]:
    """転写対象の**意味のある行**を返す (集約時の冒頭抜粋用)。

    機構見出し行 (flush 整形の kind 見出し — 正準は
    perception_buffer._KIND_HEADERS) と空行は除く — 見出しだけを冒頭に採ると、
    集約された digest に本文が一文字も残らない (2026-08-19 Codex 第三巡 #2)。
    除外は**既知の機構見出しの完全一致に限定**する — 素の ``[...]`` 形の行は
    本文でありうる (全行が角括弧形式のバッチが先頭 1 行に潰れる, 同 第四巡 #4)。
    意味行がゼロ (全部が機構見出し) のときは元の非空行の先頭数行へ
    フォールバックする。
    """
    known_headers = _known_kind_headers()
    lines: List[str] = []
    nonempty: List[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        nonempty.append(s)
        if s in known_headers:
            continue  # 機構見出し ([フィード] / [システム通知] 等) のみ除外
        lines.append(s)
    if not lines:
        return nonempty[:_ANNEX_HEAD_LINES]
    return lines


def _strip_system_wrap(text: str) -> str:
    """legacy event_message 行の ``<system>…</system>`` 包みを剥がす (表示用)。"""
    out = (text or "").strip()
    if out.startswith("<system>"):
        out = out[len("<system>"):]
    if out.endswith("</system>"):
        out = out[: -len("</system>")]
    return out.strip()


def _annex_time_spans(chunks) -> List[tuple]:
    """各チャンクが付記を引き受ける時刻範囲 [lo, hi) を返す (chunks と同順)。

    範囲はチャンクのメッセージ時刻スパンを基に、**同一 fold 群 (group_key) の
    中だけで**開始時刻順に隙間なく敷き詰める: 知覚の消費 (Beat 頭) はメッセージ
    書き込みの合間に起きるので、チャンクの素のスパン [min, max] だけだと、fold
    内の切れ目 (pulse 関節) に消費された知覚がどのチャンクにも属さない。

    - 群内で並べ替えた k 番目のチャンク: lo = 自分の開始, hi = 群内の次の
      チャンクの開始。
    - 群の最後のチャンク: hi = 自分の末尾 + 1。**群を跨いで敷き詰めない** —
      群と群の間には生きた提示中の範囲が挟まりうるので、跨ぐと提示中の期間の
      バッチを先取りで digest へ畳んでしまう (2026-08-18 Codex #5)。群の間に
      落ちたバッチは未付記のまま提示に残り、その土地が編纂される回に引き取ら
      れる (§10.4)。
    """
    if not chunks:
        return []
    starts = [min(m.created_at for m in c.messages) for c in chunks]
    ends = [max(m.created_at for m in c.messages) for c in chunks]
    by_group: dict = {}
    for i, chunk in enumerate(chunks):
        by_group.setdefault(getattr(chunk, "group_key", None), []).append(i)
    spans: List[Optional[tuple]] = [None] * len(chunks)
    for indices in by_group.values():
        order = sorted(indices, key=lambda i: (starts[i], ends[i]))
        for pos, idx in enumerate(order):
            lo = starts[idx]
            if pos + 1 < len(order):
                hi = starts[order[pos + 1]]
            else:
                hi = ends[idx] + 1
            spans[idx] = (lo, max(lo, hi))
    return spans  # type: ignore[return-value]


def _previous_compiled_end(conn: sqlite3.Connection, before_epoch: int) -> Optional[int]:
    """既存の一次あらすじのうち ``before_epoch`` 以前に終わるものの末尾時刻。"""
    try:
        row = conn.execute(
            "SELECT MAX(end_time) FROM arasuji_entries "
            "WHERE level = 1 AND end_time <= ?",
            (int(before_epoch),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row and row[0] is not None else None


def collect_annex_items(
    conn: sqlite3.Connection,
    lo: int,
    hi: int,
    *,
    thread_id: Optional[str],
    recover_before: Optional[int] = None,
) -> tuple:
    """[lo, hi) の期間の付記対象を時刻順で集める。

    対象は二種 (perception_buffer.md §10.4):

    - **未付記の消費バッチ** (``annexed_entry_id IS NULL`` で ``consumed_at`` が
      期間内)。``recover_before`` 指定時 (計画の先頭チャンク) は、それ以前に
      consumed_at を持つ未付記バッチも**一括で引き取る** — チャンク skip・
      付記失敗・fold 関節の取り残しの回収路。転写には発生時刻が付くので、
      少し後の digest に載っても時系列の嘘にはならない。
    - legacy の event_message 行 (``metadata.tags`` に event_message) — 直挿し
      時代の行。行自体は削除しない。Chronicle 編纂対象からは除外されている
      (get_messages_for_chronicle) ので、ここで拾わなければ従来どおり digest に
      入らず退場する (付記印を持てないため、転写機会は期間一致の一度きり —
      既知の残欠, §10.4)。

    戻りは ``(items, batch_ids)``。items は ``{"kind", "at", "text"}`` の list
    (at 昇順)、batch_ids は転写したバッチの id (digest 確定 tx で付記印を打つ)。
    """
    items: List[dict] = []
    batch_ids: List[int] = []
    try:
        from sai_memory.perception_buffer import list_unannexed_batches
        batches = list_unannexed_batches(conn, since=int(lo), before=int(hi))
        if recover_before is not None:
            recovered = list_unannexed_batches(
                conn, before=int(recover_before) + 1,
            )
            seen = {b.id for b in batches}
            batches = sorted(
                batches + [b for b in recovered if b.id not in seen],
                key=lambda b: (b.consumed_at, b.id),
            )
    except sqlite3.OperationalError:
        batches = []  # 台帳の無い DB (旧テスト等)
    for batch in batches:
        items.append({
            "kind": "perception",
            "at": int(batch.consumed_at),
            "text": batch.rendered_text,
        })
        batch_ids.append(batch.id)
    items.extend(collect_legacy_event_items(conn, lo, hi, thread_id=thread_id))
    items.sort(key=lambda d: d["at"])
    return items, batch_ids


def _legacy_rows_to_items(rows) -> List[dict]:
    """(id, content, created_at, metadata) 行列を付記 items 形へ (タグ検証込み)。

    LIKE の粗い絞りを JSON parse で確定する共通部。items は ``id`` を運ぶ —
    entry metadata への構造化保存 (annexed_legacy_ids) の材料。
    """
    import json as _json
    items: List[dict] = []
    for mid, content, at, metadata in rows:
        try:
            meta = _json.loads(metadata) if metadata else None
        except (TypeError, ValueError):
            continue
        tags = meta.get("tags") if isinstance(meta, dict) else None
        if not (isinstance(tags, list) and "event_message" in tags):
            continue
        items.append({
            "kind": "event_message",
            "at": int(at),
            "text": _strip_system_wrap(str(content or "")),
            "id": str(mid),
        })
    return items


def collect_legacy_event_items(
    conn: sqlite3.Connection,
    lo: int,
    hi: int,
    *,
    thread_id: Optional[str],
) -> List[dict]:
    """[lo, hi) の legacy event_message 行を付記 items 形で集める。

    直挿し時代の行 (``metadata.tags`` に event_message)。編纂の付記
    (:func:`collect_annex_items`) が使い、転写した行の id は entry metadata の
    ``annexed_legacy_ids`` に保存される — 再生成の継承はその集合を読む
    (2026-08-19 Codex 第六巡 #3 / 第七巡 #2)。
    """
    try:
        params: list = [int(lo), int(hi)]
        thread_clause = ""
        if thread_id:
            thread_clause = "AND thread_id = ? "
            params.append(thread_id)
        rows = conn.execute(
            "SELECT id, content, created_at, metadata FROM messages "
            "WHERE created_at >= ? AND created_at < ? "
            f"{thread_clause}"
            "AND metadata LIKE '%event_message%' "
            "ORDER BY created_at ASC, id ASC",
            tuple(params),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return _legacy_rows_to_items(rows)


def legacy_event_items_by_ids(
    conn: sqlite3.Connection, message_ids: List[str],
) -> List[dict]:
    """entry metadata の ``annexed_legacy_ids`` から legacy items を復元する。

    再生成の継承の**正**の経路 (第七巡 #2): 付記時に確定した id 集合を読むので、
    時刻範囲の再収集と違い、同秒の隣接 entry の legacy を誤収集しない。
    """
    if not message_ids:
        return []
    placeholders = ",".join("?" for _ in message_ids)
    try:
        rows = conn.execute(
            f"SELECT id, content, created_at, metadata FROM messages "
            f"WHERE id IN ({placeholders}) "
            "ORDER BY created_at ASC, id ASC",
            tuple(str(m) for m in message_ids),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return _legacy_rows_to_items(rows)


def collect_legacy_event_items_in_key_range(
    conn: sqlite3.Connection,
    lo_key: tuple,
    hi_key: tuple,
    *,
    thread_id: Optional[str],
) -> List[dict]:
    """正典順序キー (created_at, rowid) の閉区間で legacy を集める。

    再生成の継承の**フォールバック** (metadata に ``annexed_legacy_ids`` を
    持たない旧 entry 用)。epoch の ``end_time+1`` 区間は同秒の隣接 entry の
    legacy を誤収集する (第七巡 #2) — source message 両端の (created_at, rowid)
    で区切れば、隣接 entry の source は互いに素なので取り違えない。
    """
    lo_ca, lo_rid = int(lo_key[0]), int(lo_key[1])
    hi_ca, hi_rid = int(hi_key[0]), int(hi_key[1])
    try:
        params: list = [lo_ca, lo_ca, lo_rid, hi_ca, hi_ca, hi_rid]
        thread_clause = ""
        if thread_id:
            thread_clause = "AND thread_id = ? "
            params.append(thread_id)
        rows = conn.execute(
            "SELECT id, content, created_at, metadata FROM messages "
            "WHERE (created_at > ? OR (created_at = ? AND rowid >= ?)) "
            "AND (created_at < ? OR (created_at = ? AND rowid <= ?)) "
            f"{thread_clause}"
            "AND metadata LIKE '%event_message%' "
            "ORDER BY created_at ASC, rowid ASC",
            tuple(params),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return _legacy_rows_to_items(rows)


def format_perception_annex(items: List[dict]) -> str:
    """付記対象を決定論で本文化する (LLM なし)。空なら空文字列。

    少量 (合計 ``ANNEX_IDENTITY_MAX_CHARS`` 以下) は恒等転写 — 全文をそのまま
    置く。大量なら kind 別に「先頭数件の冒頭 + ほか N 件」(day_report の型,
    saiverse/day_report.py) へ落とす。
    """
    if not items:
        return ""
    from datetime import datetime

    def _stamp(epoch: int) -> str:
        try:
            return datetime.fromtimestamp(int(epoch)).strftime("%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            return "?"

    header = "（この期間に届いた通知・知覚の記録 — 機械的な転写）"
    total = sum(len(d["text"]) for d in items)
    lines: List[str] = [header]
    if total <= ANNEX_IDENTITY_MAX_CHARS:
        for d in items:
            lines.append(f"- ({_stamp(d['at'])}) {d['text']}")
        return "\n".join(lines)

    # kind 別集約 (出現順)。
    by_kind: dict = {}
    kind_order: List[str] = []
    for d in items:
        if d["kind"] not in by_kind:
            by_kind[d["kind"]] = []
            kind_order.append(d["kind"])
        by_kind[d["kind"]].append(d)
    for kind in kind_order:
        group = by_kind[kind]
        lines.append(f"- {kind}: {len(group)} 件")
        for d in group[:_ANNEX_HEAD_ITEMS]:
            # 見出し行・空行を除いた「意味のある行」の先頭数行を残す —
            # 大量の単一バッチでも中身の冒頭が必ず digest に残る。
            heads = _annex_meaningful_lines(d["text"])[:_ANNEX_HEAD_LINES]
            capped = [
                (h[:_ANNEX_HEAD_CHARS] + "…") if len(h) > _ANNEX_HEAD_CHARS else h
                for h in heads
            ]
            body = " / ".join(capped) if capped else ""
            lines.append(f"  ・({_stamp(d['at'])}) {body}")
        if len(group) > _ANNEX_HEAD_ITEMS:
            lines.append(f"  ・ほか {len(group) - _ANNEX_HEAD_ITEMS} 件")
    return "\n".join(lines)


@dataclass
class ExecutionResult:
    """execute_plan の結果。"""

    created: List[ArasujiEntry] = field(default_factory=list)
    skipped_duplicates: int = 0
    cancelled: bool = False
    #: 抽出 (batch_callback) が失敗したチャンクの Chronicle entry id。
    #: チャンク自体は確定済みで、再実行では source_ids の冪等スキップにより
    #: batch_callback が再発火しない。失敗は付箋 (entity_extraction_backlog)
    #: にも記録され、次の Metabolism の頭で拾い直される。このリストは
    #: 呼び出し元への当回ぶんの報告用 (握り潰さない)
    #: (docs/issues/memopedia_writers_bypass_adapter_lock.md)。
    extraction_failures: List[str] = field(default_factory=list)
    #: そのうち**付箋にも残せなかった**もの。これらは拾い直しの対象にならない
    #: —— 「次回の記憶の整理でやり直します」と報告してはいけない相手
    #: (Codex 五巡 #1)。分けて持たないと、画面の約束が嘘になる。
    extraction_failures_unrecorded: List[str] = field(default_factory=list)

    @property
    def created_count(self) -> int:
        return len(self.created)


def _record_llm_usage(client, persona_id: Optional[str], node_type: str) -> None:
    """LLM usage 記録 (generator._record_llm_usage を共用 — 失敗しても止めない)。"""
    from sai_memory.arasuji.generator import _record_llm_usage as _impl
    _impl(client, persona_id, node_type)


def _is_already_compiled(conn: sqlite3.Connection, first_source_id: str) -> bool:
    """チャンク先頭 message が既に一次あらすじの source に含まれているか (重複再検査)。

    M1 claim (プロセス間) と adapter._db_lock (プロセス内) の背後の三段目 —
    台帳の無い degrade 環境で同一 plan が並走したときの防御 (W4 D4)。plan は
    同一入力から決定論に作られるため、再実行のチャンク境界は一致し、先頭
    id の一致検査で過不足なく skip できる。
    """
    row = conn.execute(
        "SELECT 1 FROM arasuji_entries, json_each(source_ids_json) "
        "WHERE level = 1 AND json_each.value = ? LIMIT 1",
        (first_source_id,),
    ).fetchone()
    return row is not None


def _build_batch_prompt(
    chunk: PlannedChunk,
    conn: sqlite3.Connection,
    *,
    include_timestamp: bool,
    memopedia_context: Optional[str],
) -> str:
    """llm_batch チャンクの圧縮プロンプト (現行 Lv1 General 版の後継)。"""
    from sai_memory.arasuji.context import get_episode_context_for_timerange
    from sai_memory.arasuji.generator import _format_messages_for_prompt

    start_time = min(m.created_at for m in chunk.messages)
    end_time = max(m.created_at for m in chunk.messages)
    context = get_episode_context_for_timerange(
        conn, start_time=start_time, end_time=end_time, max_entries=20,
    )
    conversation = _format_messages_for_prompt(
        chunk.messages, include_timestamp=include_timestamp,
    )

    parts = [
        "あなたは記憶の記録者です。以下の会話から、出来事のあらすじを書いてください。",
        "",
    ]
    if context:
        parts.extend(["## これまでの流れ（参考）", context, ""])
    if memopedia_context:
        parts.extend(["## 意味記憶（人物・用語の背景情報）", memopedia_context, ""])
    parts.extend([
        "## 今回記録する会話",
        conversation,
        "",
        "## 指示",
        "- 3〜5文程度で、何が起きたか、誰と何を話したかを要約",
        "- 時系列の流れがわかるように書く",
        "- 固有名詞や重要な詳細は保持する",
        "- 感情や雰囲気も含める",
        "- 「〜について話した」のような抽象的な記述は避け、具体的に書く",
        "- [作業のまとめ] と印の付いた項目は、既に要約された作業の記録です。"
        "発言として引用せず、出来事の流れの一部として織り込む",
        "- **日時情報（【2025-01-07 23:56 ~】など）は書かないでください**（自動で付与されます）",
        "- **「あらすじ」などの見出しは書かないでください**（本文のみ出力）",
        "",
        "あらすじを日本語で書いてください。",
    ])
    return "\n".join(parts)


def _chunk_content(
    chunk: PlannedChunk,
    client,
    conn: sqlite3.Connection,
    *,
    persona_id: Optional[str],
    include_timestamp: bool,
    memopedia_context: Optional[str],
) -> str:
    """チャンクの content を得る (常に LLM 圧縮 — 小さくても要約する)。"""
    prompt = _build_batch_prompt(
        chunk, conn,
        include_timestamp=include_timestamp,
        memopedia_context=memopedia_context,
    )
    from llm_clients.exceptions import LLMError
    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        _record_llm_usage(client, persona_id, "chronicle_level1")
    except LLMError as exc:
        # 現行 generator と同じ契約: LLMError は文脈を付けて propagate
        # (frontend がバッチナビゲーションに使う)。
        exc.user_message = (
            f"メッセージ {len(chunk.messages)} 件のチャンク処理中: "
            f"{exc.user_message}"
        )
        exc.batch_meta = {
            "message_ids": chunk.message_ids,
            "start_time": min(m.created_at for m in chunk.messages),
            "end_time": max(m.created_at for m in chunk.messages),
        }
        raise
    content = (response or "").strip()
    if not content:
        raise ChunkExecutionError(
            f"empty LLM response for chunk ({len(chunk.messages)} messages)"
        )
    return content


def execute_plan(
    plan: AlignmentPlan,
    client,
    conn: sqlite3.Connection,
    *,
    persona_id: Optional[str] = None,
    include_timestamp: bool = True,
    memopedia_context: Optional[str] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    batch_callback: Optional[Callable[[List, Optional[str]], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    db_lock: Optional[Any] = None,
) -> ExecutionResult:
    """整列計画を実行して一次あらすじを確定する。

    Args:
        plan: plan_alignment の出力。
        client: LLM client (llm_batch チャンクでのみ使用)。
        conn: persona memory.db の connection。
        progress_callback: callback(processed_messages, total_messages)。
        batch_callback: callback(messages, entry_id)。Fragment 抽出
            (entity_extractor) 用。全チャンクで呼ぶ (現設計は全チャンクが
            LLM 圧縮 — 恒等圧縮の廃止で抽出の発火点はここに一本化された)。
        cancel_check: True を返すと以降のチャンクを中断 (確定済みは残る)。
        db_lock: SAIMemoryAdapter の ``_db_lock``。抽出失敗の付箋を書くときに
            使う —— ``conn`` が adapter と共有なら、ロック外の commit は他所の
            開いたトランザクションを途中で確定させる (Codex 四巡 #2)。

    Returns:
        ExecutionResult (created は確定順)。

    Raises:
        LLMError / ChunkExecutionError: チャンク失敗。確定済みチャンクは
        残る (再試行は processed_ids 再計算と重複再検査で冪等)。
    """
    result = ExecutionResult()
    total = plan.total_unprocessed
    processed = 0

    # 退場付記 (§10.4) の時刻範囲。同一 fold 群の中で開始時刻順に敷き詰める。
    # 計画の先頭チャンク (全体で最古) には「既存の編纂被覆の末尾以前に
    # consumed_at を持つ未付記バッチ」の一括回収を載せる — チャンク skip・
    # 付記失敗の取り残しは、既に編纂済みの土地に消費印を持つバッチとして残る
    # ので、次の編纂の先頭が必ず引き取る (全期間の遡り走査はしない)。
    annex_spans = _annex_time_spans(plan.chunks)
    recover_first_idx: Optional[int] = None
    recover_before: Optional[int] = None
    if annex_spans:
        recover_first_idx = min(
            range(len(annex_spans)), key=lambda i: annex_spans[i][0],
        )
        recover_before = _previous_compiled_end(
            conn, annex_spans[recover_first_idx][0],
        )

    for chunk_index, chunk in enumerate(plan.chunks):
        if cancel_check and cancel_check():
            LOGGER.info(
                "[executor] chunk execution cancelled (%d/%d chunks done)",
                len(result.created), len(plan.chunks),
            )
            result.cancelled = True
            break

        if progress_callback:
            progress_callback(processed, total)

        if not chunk.messages:
            continue

        # 事前の重複検査 (LLM コストの節約 — 整合性の防御は下の tx 内検査)。
        if _is_already_compiled(conn, chunk.messages[0].id):
            LOGGER.warning(
                "[executor] chunk skipped: first source already compiled "
                "(first_id=%s kind=%s size=%d)",
                chunk.messages[0].id, chunk.kind, len(chunk.messages),
            )
            result.skipped_duplicates += 1
            processed += len(chunk.messages)
            continue

        # LLM (tx 外) → チャンク単一 tx で確定。
        content = _chunk_content(
            chunk, client, conn,
            persona_id=persona_id,
            include_timestamp=include_timestamp,
            memopedia_context=memopedia_context,
        )
        skipped_in_tx = False
        annex_conflict = False
        # 付記の収集〜印付けは **BEGIN IMMEDIATE の内側** (2026-08-19 Codex
        # 第二巡 #2): 収集を tx の外でやると、収集と印付けの間に別の編纂が
        # 同じバッチを付記した場合、印は 0 行でも古い収集結果ごと commit され
        # 同じ知覚が複数 entry に転写される。付記は決定論 (LLM なし) なので
        # tx 内で組める。印の行数が収集数と食い違ったら rollback して
        # チャンクごとやり直す (1 回目は付記ありで再試行 — 新しい tx 内の
        # 再収集が競合相手の印を見て除外する。2 回目も食い違うなら付記なしで
        # 確定 — バッチは未付記のまま提示に残り、一括回収が引き取る)。
        # 「差分だけ本文から除いて組み直す」形にしなかったのは、除外→再整形が
        # 再収集と同じ計算になるため — 同じコードパスを回す方が単純で等価。
        for annex_mode in ("annex", "annex-retry", "no-annex"):
            annex_conflict = False
            try:
                # tx 内再検査 (Codex W4 #1 / 二巡 #1 / 三巡 #1): BEGIN IMMEDIATE
                # で write ロックを先に取ってから検査する — sqlite3 は SELECT
                # では暗黙 BEGIN を張らないため、明示的にロックを取らないと
                # 検査と INSERT の間に別コネクションが commit できてしまう。
                # 既に tx 内 (呼び出し元が開いた tx) なら参加する。それ以外の
                # 失敗 (database is locked = busy_timeout 超過) は握り潰さず
                # raise — ロック無しで検査に進むと原子化が無効になる。
                #
                # BEGIN IMMEDIATE は **DB の**書き込みロック。同じ接続を共有
                # する別スレッド (Pulse / API) の commit までは止められないので、
                # 検査〜commit の区間は adapter の錠前 (db_lock) の内側で走らせる
                # (Codex 七巡 #4)。LLM 呼び出し (_chunk_content) は錠の外のまま。
                with (db_lock or nullcontext()):
                    if not conn.in_transaction:
                        conn.execute("BEGIN IMMEDIATE")
                    if _is_already_compiled(conn, chunk.messages[0].id):
                        conn.rollback()
                        LOGGER.warning(
                            "[executor] chunk skipped in-tx: first source "
                            "compiled concurrently (first_id=%s kind=%s)",
                            chunk.messages[0].id, chunk.kind,
                        )
                        skipped_in_tx = True
                    else:
                        # 退場付記 (W14 §10.4): この期間の未付記バッチ・legacy
                        # event_message を決定論で転写して digest に添える。
                        # 失敗しても編纂は止めない — 付記なしで確定し、バッチは
                        # 未付記のまま提示に残る (fail-open)。
                        annex_batch_ids: List[int] = []
                        annex_legacy_ids: Optional[List[str]] = None
                        chunk_content = content
                        if annex_mode != "no-annex":
                            try:
                                lo, hi = annex_spans[chunk_index]
                                annex_items, annex_batch_ids = collect_annex_items(
                                    conn, lo, hi,
                                    thread_id=getattr(
                                        chunk.messages[0], "thread_id", None,
                                    ),
                                    recover_before=(
                                        recover_before
                                        if chunk_index == recover_first_idx
                                        else None
                                    ),
                                )
                                # 転写した legacy 行の id を構造化保存する
                                # (空リストも「付記は走った」の確定情報)。
                                # 再生成の継承がこの集合を読む — 時刻範囲の
                                # 再収集より正確で、同秒の隣接 entry からの
                                # 誤収集が構造的に起きない (第七巡 #2)。
                                annex_legacy_ids = [
                                    d["id"] for d in annex_items
                                    if d.get("kind") == "event_message"
                                    and d.get("id")
                                ]
                                annex_text = format_perception_annex(annex_items)
                                if annex_text:
                                    chunk_content = f"{content}\n\n{annex_text}"
                            except Exception:
                                annex_batch_ids = []
                                annex_legacy_ids = None
                                chunk_content = content
                                LOGGER.warning(
                                    "[executor] perception annex failed for "
                                    "chunk (kind=%s); committing digest without "
                                    "the annex", chunk.kind, exc_info=True,
                                )
                        entry_metadata: dict = {
                            "digest_origin": chunk.kind,
                            "coverage_chars": chunk.coverage_chars,
                            "episode_refs": chunk.episode_refs,
                        }
                        if annex_legacy_ids is not None:
                            entry_metadata["annexed_legacy_ids"] = annex_legacy_ids
                        entry = create_entry(
                            conn,
                            level=1,
                            content=chunk_content,
                            source_ids=chunk.message_ids,
                            start_time=min(m.created_at for m in chunk.messages),
                            end_time=max(m.created_at for m in chunk.messages),
                            source_count=len(chunk.messages),
                            message_count=len(chunk.messages),
                            extra_metadata=entry_metadata,
                            commit=False,
                        )
                        # 付記印は digest 確定と同一 tx (§10.4)。rollback
                        # すれば印も戻り、バッチは未付記 = 提示に残る。
                        if annex_batch_ids:
                            from sai_memory.perception_buffer import (
                                mark_batches_annexed,
                            )
                            stamped = mark_batches_annexed(
                                conn, annex_batch_ids, entry.id,
                            )
                            if stamped != len(annex_batch_ids):
                                # 収集と印の食い違い = 転写と印が別のバッチ
                                # 集合になる。commit すると印の無い転写 (二重
                                # 転写の口) が残るので、tx ごと破棄してやり直す。
                                conn.rollback()
                                annex_conflict = True
                        if not annex_conflict:
                            conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            if not annex_conflict:
                break
            LOGGER.warning(
                "[executor] annex stamp count mismatch for chunk (kind=%s, "
                "mode=%s); rolling back and retrying %s",
                chunk.kind, annex_mode,
                "with a fresh collection" if annex_mode == "annex"
                else "without the annex",
            )

        if skipped_in_tx:
            result.skipped_duplicates += 1
            processed += len(chunk.messages)
            continue

        result.created.append(entry)
        processed += len(chunk.messages)
        LOGGER.info(
            "[executor] chunk committed: kind=%s messages=%d coverage=%d "
            "episodes=%s entry=%s",
            chunk.kind, len(chunk.messages), chunk.coverage_chars,
            ",".join(chunk.episode_refs) or "-", entry.id[:8],
        )

        if batch_callback:
            try:
                batch_callback(chunk.messages, entry.id)
            except Exception:
                # Chronicle のチャンクは確定済みなので生成は続ける。ただし失敗を
                # ここで消さない —— 確定済みチャンクは再実行で冪等スキップされる
                # ため、この抽出は放っておくと回収されない。結果に載せて呼び出し元へ
                # 伝え、付箋 (backlog) に貼って次の Metabolism が拾い直す
                # (docs/issues/memopedia_writers_bypass_adapter_lock.md)。
                result.extraction_failures.append(entry.id)
                try:
                    from sai_memory.memory.entity_extractor import (
                        record_extraction_failure,
                    )
                    record_extraction_failure(conn, entry.id, db_lock=db_lock)
                except Exception:
                    # 付箋に残せなければ、この抽出には二度と番が回らない
                    # (確定済みチャンクは再実行で冪等スキップされる)。
                    # warning に畳むと「拾い直す」という約束が黙って破れる
                    result.extraction_failures_unrecorded.append(entry.id)
                    LOGGER.error(
                        "[executor] 抽出失敗の付箋を残せませんでした "
                        "(entry=%s) — この範囲の知識は自動では拾い直されません",
                        entry.id[:8], exc_info=True,
                    )
                LOGGER.exception(
                    "[executor] batch_callback failed (entry=%s); continuing",
                    entry.id[:8],
                )

    if progress_callback and not result.cancelled:
        progress_callback(processed, total)
    return result
