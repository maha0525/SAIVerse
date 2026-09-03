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
    """チャンク実行の失敗 (保存失敗など LLMError 以外の失敗)。

    LLM の空応答はここには来ない — generator.generate_text_with_empty_retry が
    規定回数試し直し、それでも空なら EmptyResponseError (LLMError) で上がる。
    """


# ---------------------------------------------------------------------------
# 知覚の消費バッチの材料化 (W14 知覚レンダリング → 2026-08-29 まはー裁定で改設計)
#
# fold (退場) が範囲を畳むとき、その期間の**未付記の消費バッチ** (知覚台帳) を
# 【知覚】ラベルの材料として編纂 LLM のプロンプトへ時刻順に差し込み、バッチには
# digest 確定と**同一トランザクション**で付記印 (annexed_entry_id =
# 「このバッチはこの entry の材料として消費済み」) を打つ。提示
# (runtime_context) はこの印だけを見てバッチを下ろす — 「付記済みか」を時刻の
# 区間演算から再構成しない。これで「退場したものは必ず編纂されている」の下限
# (experience_structure §4-1) が知覚に通る。tx が rollback すれば印も戻り、
# バッチは未付記 = 提示に残る (fail-open)。
#
# 旧設計の「digest 本文への機械的な付記 (annex ブロック連結)」は廃止 —
# あらすじ本文は次のレベルの畳みで LLM の手本になるため、機構の定型ブロックを
# 埋め込むと LLM がその形式を模倣して偽の機械記録を書き始める危険がある。
# legacy event_message 行の別収集も廃止 — 除外タグの解除 (storage.
# chronicle_eligibility_filter) で event_message 行は普通の材料として入る。
# 長さ規則 (500 字超は決定論の一行) は generator.MECHANISM_TEXT_MAX_CHARS。
# ---------------------------------------------------------------------------


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
    recover_before: Optional[int] = None,
) -> tuple:
    """[lo, hi) の期間の材料化対象 (未付記の消費バッチ) を時刻順で集める。

    対象は**未付記の消費バッチ** (``annexed_entry_id IS NULL`` で
    ``consumed_at`` が期間内)。``recover_before`` 指定時 (計画の先頭チャンク)
    は、それ以前に consumed_at を持つ未付記バッチも**一括で引き取る** —
    チャンク skip・印付け失敗・fold 関節の取り残しの回収路。材料には発生時刻が
    付くので、少し後の digest の材料に載っても時系列の嘘にはならない。

    戻りは ``(items, batch_ids)``。items は ``{"at", "text"}`` の list
    (at 昇順) — generator._format_messages_for_prompt の ``extra_items`` 形。
    batch_ids は材料にしたバッチの id (digest 確定 tx で付記印を打つ)。
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
            "at": int(batch.consumed_at),
            "text": batch.rendered_text,
        })
        batch_ids.append(batch.id)
    return items, batch_ids


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
    perception_items: Optional[List[dict]] = None,
) -> str:
    """llm_batch チャンクの圧縮プロンプト (現行 Lv1 General 版の後継)。

    ``perception_items`` (知覚の消費バッチ、``{"at", "text"}`` の list) は
    【知覚】/【想起 (過去の再提示)】ラベルの材料として時刻順の正しい位置に
    差し込まれる (generator._format_messages_for_prompt)。
    """
    from sai_memory.arasuji.context import get_episode_context_for_timerange
    from sai_memory.arasuji.generator import (
        RECALL_PROMPT_NOTE,
        _format_messages_for_prompt,
        has_recall_material,
    )

    start_time = min(m.created_at for m in chunk.messages)
    end_time = max(m.created_at for m in chunk.messages)
    context = get_episode_context_for_timerange(
        conn, start_time=start_time, end_time=end_time, max_entries=20,
    )
    conversation = _format_messages_for_prompt(
        chunk.messages, include_timestamp=include_timestamp,
        extra_items=perception_items,
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
    ])
    if has_recall_material(chunk.messages, perception_items):
        parts.append(RECALL_PROMPT_NOTE)
    parts.extend([
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
    perception_items: Optional[List[dict]] = None,
) -> str:
    """チャンクの content を得る (常に LLM 圧縮 — 小さくても要約する)。"""
    prompt = _build_batch_prompt(
        chunk, conn,
        include_timestamp=include_timestamp,
        memopedia_context=memopedia_context,
        perception_items=perception_items,
    )
    from llm_clients.exceptions import LLMError
    from sai_memory.arasuji.generator import generate_text_with_empty_retry
    try:
        # 空応答 (推論モデルが reasoning_content だけで閉じる) は helper が
        # 規定回数まで試し直す。使い切ったら EmptyResponseError が上がってくる。
        # usage は試行ごとに helper 側で記録する。
        return generate_text_with_empty_retry(
            client,
            [{"role": "user", "content": prompt}],
            purpose="chronicle_level1 chunk",
            persona_id=persona_id,
            usage_node_type="chronicle_level1",
        )
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
    after_chunk: Optional[Callable[[int, int], None]] = None,
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
        after_chunk: callback(chunks_done, total_chunks)。チャンクが **確定
            (commit) された直後**に呼ぶ (重複スキップでは呼ばない)。呼び出し元
            (generate_chronicle) はここで上位あらすじの束ね (bands.
            run_band_overflow) を挟む — 各チャンクのプロンプトに載る
            「これまでの流れ」は確定済みあらすじを新しい側から辿り、近い過去は
            レベル1、遠い過去はレベル2 以上で読む設計なので、束ねを走行の最後に
            一度だけ行うと、大量編纂の後半チャンクは直前 20 件のレベル1 しか
            見えず、それより前の流れを失う (2026-09-03)。callback の例外は
            編纂を止めない (記録して続行)。

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

        # 知覚バッチは編纂 LLM の**材料**なので、収集は LLM 呼び出しの前
        # (tx 外・決定論)。印は digest 確定と同一 tx で「収集した集合そのもの」
        # に打つ — mark_batches_annexed は未付記の行にしか印を打たないので、
        # 収集〜印付けの間に別の編纂が同じバッチを消費していれば行数が
        # 食い違い、tx ごと破棄してチャンクをやり直す (材料が変わるので LLM も
        # 呼び直し)。1 回目は再収集つきで再試行 — 新しい収集が競合相手の印を
        # 見て除外する。2 回目も食い違うならバッチなしの材料で確定 — バッチは
        # 未付記のまま提示に残り、次の編纂の一括回収が引き取る (fail-open)。
        skipped_in_tx = False
        for material_mode in ("perception", "perception-retry", "no-perception"):
            annex_conflict = False
            # 1. 材料化するバッチの収集 (tx 外・決定論)。失敗しても編纂は
            # 止めない — バッチなしで進み、未付記のまま提示に残る (fail-open)。
            perception_items: List[dict] = []
            annex_batch_ids: List[int] = []
            if material_mode != "no-perception":
                try:
                    lo, hi = annex_spans[chunk_index]
                    perception_items, annex_batch_ids = collect_annex_items(
                        conn, lo, hi,
                        recover_before=(
                            recover_before
                            if chunk_index == recover_first_idx
                            else None
                        ),
                    )
                except Exception:
                    perception_items, annex_batch_ids = [], []
                    LOGGER.warning(
                        "[executor] perception collection failed for chunk "
                        "(kind=%s); compiling without perception material",
                        chunk.kind, exc_info=True,
                    )
            # 2. LLM (tx 外)。
            content = _chunk_content(
                chunk, client, conn,
                persona_id=persona_id,
                include_timestamp=include_timestamp,
                memopedia_context=memopedia_context,
                perception_items=perception_items,
            )
            # 3. チャンク単一 tx で確定。
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
                        entry = create_entry(
                            conn,
                            level=1,
                            content=content,
                            source_ids=chunk.message_ids,
                            start_time=min(m.created_at for m in chunk.messages),
                            end_time=max(m.created_at for m in chunk.messages),
                            source_count=len(chunk.messages),
                            message_count=len(chunk.messages),
                            extra_metadata={
                                "digest_origin": chunk.kind,
                                "coverage_chars": chunk.coverage_chars,
                                "episode_refs": chunk.episode_refs,
                            },
                            commit=False,
                        )
                        # 付記印 (「この entry の材料として消費済み」) は digest
                        # 確定と同一 tx (§10.4)。rollback すれば印も戻り、
                        # バッチは未付記 = 提示に残る。
                        if annex_batch_ids:
                            from sai_memory.perception_buffer import (
                                mark_batches_annexed,
                            )
                            stamped = mark_batches_annexed(
                                conn, annex_batch_ids, entry.id,
                            )
                            if stamped != len(annex_batch_ids):
                                # 材料と印の食い違い = 材料に使ったバッチの一部が
                                # 別 entry へ消費済み。commit すると同じ知覚が
                                # 複数 entry の材料になるので、tx ごと破棄して
                                # チャンクをやり直す。
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
                "[executor] perception stamp count mismatch for chunk (kind=%s, "
                "mode=%s); rolling back and re-running the chunk %s",
                chunk.kind, material_mode,
                "with a fresh collection" if material_mode == "perception"
                else "without perception material",
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

        if after_chunk:
            try:
                after_chunk(len(result.created), len(plan.chunks))
            except Exception:
                LOGGER.exception("[executor] after_chunk hook failed; continuing")

    if progress_callback and not result.cancelled:
        progress_callback(processed, total)
    return result
