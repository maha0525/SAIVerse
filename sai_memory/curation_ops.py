"""編纂プランの永続化層＋実行部 — per-persona memory.db の curation_plans テーブル。

P4-a の三層（検知 → 裁定 → 実行）のうち「裁定から実行への橋渡し」と
「実行本体」を担う。

就寝判断（day_close）の finalize が approve された op_id を
``enqueue_plan`` でここに書き込み、背景スレッドが ``run_pending_plans``
で実行する。

実行関数（P4-a2 実装）:
    execute_merge(conn, survivor_page_id, absorbed_page_id, memopedia) -> dict
        完全決定論・LLM ゼロ。残す側本文 ＋ 区切り ＋ 吸収側本文の逐語連結。
    plan_split(conn, page_id, llm_client) -> dict
        split の前段（読み取り＋LLM のみ・書き込みなし）。LLM はブロック割当
        ラベルのみ。保存則の機械検証あり（番号集合＋文字レベル復元。違反は棄却）。
    apply_split(conn, memopedia, split_plan) -> dict
        split の後段（書き込みのみ・LLM なし）。
    execute_split(conn, page_id, memopedia, llm_client) -> dict
        plan_split → apply_split の一括実行（直接呼び出し用）。
    run_pending_plans(manager, persona_id) -> dict
        pending プランを全実行。個々の失敗は他を止めない。
        **1 プラン = 1 トランザクション**: 編纂経路には commit を保留する
        プロキシ conn を渡し、プラン成功時のみ commit / 失敗時は rollback
        する（途中失敗で部分変更が残らない）。

テーブル定義（冪等）:
    id          TEXT PRIMARY KEY         -- UUID
    created_at  INTEGER                  -- epoch 秒
    kind        TEXT                     -- "split" | "merge" | "fold"
    op_id       TEXT                     -- 検知層が付けた決定論の一意 ID
    refs_json   TEXT                     -- JSON 配列 [m:N, ...] ページ参照
    status      TEXT DEFAULT 'pending'   -- "pending"|"done"|"failed"|"rejected"
    result_json TEXT NULL                -- 実行後の結果 JSON
    executed_at INTEGER NULL             -- 実行完了 epoch 秒
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"


# ---------------------------------------------------------------------------
# テーブル初期化（冪等）
# ---------------------------------------------------------------------------


def init_curation_tables(conn: sqlite3.Connection) -> None:
    """curation_plans テーブルを冪等に初期化する。

    adapter init（saiverse_memory/adapter.py）から呼び出す。
    既にテーブルが存在する場合は何もしない。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS curation_plans (
            id          TEXT PRIMARY KEY,
            created_at  INTEGER NOT NULL,
            kind        TEXT NOT NULL,
            op_id       TEXT NOT NULL,
            refs_json   TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            result_json TEXT,
            executed_at INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_curation_plans_op_id"
        " ON curation_plans(op_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_curation_plans_status"
        " ON curation_plans(status)"
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 書き込み
# ---------------------------------------------------------------------------


def enqueue_plan(
    conn: sqlite3.Connection,
    kind: str,
    op_id: str,
    refs: List[str],
) -> str:
    """編纂プランを curation_plans に追加する。

    同じ ``op_id`` の pending プランが既に存在する場合は **重複挿入しない**
    （冪等。approve を二度押しされても行は 1 件のまま）。

    Args:
        conn:   per-persona memory.db の接続
        kind:   "split" | "merge" | "fold"
        op_id:  検知層が付けた決定論の一意 ID（例: "split:m:12"）
        refs:   操作対象ページの参照ラベル（例: ["m:12"]）

    Returns:
        既存 pending 行の id、または新規挿入した行の id。

    P4-a2 の領分（このモジュールでは実装しない）:
        - merge 本体（残す側に消える側を逐語結合、子ページ付け替え、soft-delete）
        - split 本体（段落ブロック割り当て + コード逐語移動 + 保存則機械検証）
        - status を "done"/"failed" に更新し result_json / executed_at を書く
    """
    # 既存 pending を検索
    cur = conn.execute(
        "SELECT id FROM curation_plans WHERE op_id = ? AND status = ?",
        (op_id, STATUS_PENDING),
    )
    existing = cur.fetchone()
    if existing:
        LOGGER.debug(
            "[curation_ops] op_id=%r already has a pending plan (%s); skipping",
            op_id, existing[0],
        )
        return existing[0]

    plan_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO curation_plans
            (id, created_at, kind, op_id, refs_json, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (plan_id, now, kind, op_id, json.dumps(refs, ensure_ascii=False), STATUS_PENDING),
    )
    conn.commit()
    LOGGER.info(
        "[curation_ops] enqueued plan id=%s kind=%s op_id=%r refs=%r",
        plan_id, kind, op_id, refs,
    )
    return plan_id


# ---------------------------------------------------------------------------
# 読み出し
# ---------------------------------------------------------------------------


def list_pending(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """pending 状態の編纂プランを古い順で返す。

    Returns:
        list of dict with keys: id, created_at, kind, op_id, refs
    """
    cur = conn.execute(
        """
        SELECT id, created_at, kind, op_id, refs_json
        FROM curation_plans
        WHERE status = ?
        ORDER BY created_at
        """,
        (STATUS_PENDING,),
    )
    rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "created_at": row[1],
            "kind": row[2],
            "op_id": row[3],
            "refs": json.loads(row[4] or "[]"),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# ステータス更新
# ---------------------------------------------------------------------------


def _update_plan_status(
    conn: sqlite3.Connection,
    plan_id: str,
    status: str,
    result: Optional[Dict[str, Any]] = None,
) -> None:
    """curation_plans の status / result_json / executed_at を更新する。"""
    now = int(time.time())
    conn.execute(
        """
        UPDATE curation_plans
        SET status = ?, result_json = ?, executed_at = ?
        WHERE id = ?
        """,
        (
            status,
            json.dumps(result, ensure_ascii=False) if result is not None else None,
            now,
            plan_id,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 段落ブロック分割ヘルパ（split の前処理）
# ---------------------------------------------------------------------------


def _split_into_blocks(content: str) -> List[str]:
    """本文を lossless な段落ブロックのリストに分割する（決定論）。

    **本文保存則の土台**: 各ブロックは元本文の連続スライスであり、
    全ブロックをこの順で連結すると元本文に文字レベルで完全一致する
    （``"".join(blocks) == content``）。先頭・末尾の空白、空白のみの行、
    3 個以上の連続改行も一切失わない。

    規則:
    - 2 個以上連続する改行をブロック境界とみなし、境界の改行列は
      **直前のブロックの末尾**に帰属させる。
    - 空白のみのブロックは前のブロックに併合する（前が無ければ次の
      ブロックの先頭に併合）——LLM に空ブロックを見せない。
    - 見出し行（# で始まる行）だけのブロックは次のブロックの先頭へ
      繰り越す——見出しがブロック末に孤立しないようにする。
      繰り越しはスライス境界の移動であり、文字は失わない。
    """
    if content == "":
        return []

    # 1. 区切り（連続改行）を保持したまま分割し、区切りを直前のチャンクの
    #    末尾に付ける（全文字がちょうど 1 つのチャンクに属する）。
    pieces = re.split(r"(\n{2,})", content)
    chunks: List[str] = []
    for i in range(0, len(pieces), 2):
        text = pieces[i]
        sep = pieces[i + 1] if i + 1 < len(pieces) else ""
        chunk = text + sep
        if chunk:
            chunks.append(chunk)

    # 2. 空白のみのチャンクは隣へ併合する（文字は必ずどこかのブロックに残す）
    merged: List[str] = []
    pending_prefix = ""
    for chunk in chunks:
        if not chunk.strip():
            if merged:
                merged[-1] += chunk
            else:
                pending_prefix += chunk
            continue
        merged.append(pending_prefix + chunk)
        pending_prefix = ""
    if pending_prefix:
        # 本文全体が空白のみ——保存則を優先して 1 ブロックとして返す
        merged.append(pending_prefix)

    # 3. 見出し行のみのブロックは次のブロックの先頭へ繰り越す
    result: List[str] = []
    pending_heading = ""
    for chunk in merged:
        if pending_heading:
            chunk = pending_heading + chunk
            pending_heading = ""
        non_empty = [ln for ln in chunk.split("\n") if ln.strip()]
        if len(non_empty) == 1 and non_empty[0].strip().startswith("#"):
            pending_heading = chunk
            continue
        result.append(chunk)
    # 最後に見出し行が余ったらそのままブロックとして追加
    if pending_heading:
        result.append(pending_heading)
    return result


# ---------------------------------------------------------------------------
# トランザクション制御 — 1 プラン = 1 トランザクション
# ---------------------------------------------------------------------------


class _NonCommittingConnection:
    """commit() を保留する sqlite3.Connection の薄いプロキシ。

    storage 層 (sai_memory/memopedia/storage.py) は各操作で conn.commit()
    する規約だが、編纂プランを 1 トランザクションで実行するため、
    編纂経路にはこのプロキシを渡して段階 commit を無効化する。
    トランザクション境界（commit / rollback）は呼び出し元
    (run_pending_plans) が実 conn で握る。storage 層の他の呼び出し元の
    挙動（即 commit）は変えない。
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def commit(self) -> None:
        """段階 commit を保留する（トランザクション終端でのみ実 conn を commit）。"""
        pass

    def rollback(self) -> None:
        """明示 rollback は設計外の呼び出しだが、意図を尊重して実 conn に委譲する。"""
        self._conn.rollback()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


@contextmanager
def _plan_transaction(conn: sqlite3.Connection):
    """1 プラン = 1 トランザクション。

    ブロック内の書き込みは _NonCommittingConnection 経由で行われる前提
    （storage 層の段階 commit が保留されている）。正常終了時のみ実 conn を
    commit し、例外時は rollback して DB をプラン実行前と同一の状態に戻す。
    呼び出し元は db_lock を保持したまま使うこと（トランザクション全体が
    ロック内にあることが必須）。
    """
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# 実行部（P4-a2）— 本文保存則が絶対
# ---------------------------------------------------------------------------


def execute_merge(
    conn: sqlite3.Connection,
    survivor_page_id: str,
    absorbed_page_id: str,
    memopedia: Any,
) -> Dict[str, Any]:
    """merge 実行（完全決定論・LLM ゼロ）。

    **本文保存則**: 残す側本文 ＋ 区切り見出し ＋ 吸収側 summary（あれば）
    ＋ 吸収側本文を逐語で連結して残す側に書き込む。
    LLM は呼ばない——「新しい文章を生成しない」が不変条件。

    処理の流れ:
    1. 残す側・吸収側のページを読む
    2. 吸収側の子ページを残す側へ付け替え（move_pages_to_parent）
    3. 残す側の本文を逐語連結で更新（Memopedia.update_page, edit_source="curation"）
    4. キーワードを和集合にして残す側に書き込む
    5. 吸収側を soft-delete（Memopedia.delete_page, edit_source="curation"）

    fold（過小ページを親へ統合）は survivor=親、absorbed=子として呼べばよい。

    Returns:
        dict: {
            "survivor_id": str,
            "absorbed_id": str,
            "absorbed_title": str,
            "merged_content_len": int,
            "children_moved": int,
        }

    Raises:
        ValueError: ページが見つからない、または同一ページへの操作
    """
    from sai_memory.memopedia.storage import get_page, get_children, move_pages_to_parent

    if survivor_page_id == absorbed_page_id:
        raise ValueError(
            f"execute_merge: survivor と absorbed が同じ ID です ({survivor_page_id})"
        )

    survivor = get_page(conn, survivor_page_id)
    if survivor is None:
        raise ValueError(f"execute_merge: survivor ページが見つかりません: {survivor_page_id}")

    absorbed = get_page(conn, absorbed_page_id)
    if absorbed is None:
        raise ValueError(f"execute_merge: absorbed ページが見つかりません: {absorbed_page_id}")

    # 1. 吸収側の子ページを残す側へ付け替え
    children = get_children(conn, absorbed_page_id)
    child_ids = [c.id for c in children]
    children_moved = 0
    if child_ids:
        children_moved = move_pages_to_parent(conn, child_ids, survivor_page_id)
        LOGGER.info(
            "[curation_ops] merge: moved %d children from %s to %s",
            children_moved, absorbed_page_id, survivor_page_id,
        )

    # 2. 本文の逐語連結（保存則: LLM は呼ばない）
    separator = f"\n\n## 統合: 旧「{absorbed.title}」より\n\n"
    absorbed_parts: List[str] = []
    if absorbed.summary and absorbed.summary.strip():
        absorbed_parts.append(absorbed.summary.strip())
    if absorbed.content and absorbed.content.strip():
        absorbed_parts.append(absorbed.content.strip())
    absorbed_body = "\n\n".join(absorbed_parts) if absorbed_parts else ""

    survivor_base = (survivor.content or "").rstrip()
    if absorbed_body:
        new_content = survivor_base + separator + absorbed_body
    else:
        new_content = survivor_base + separator + "（本文なし）"

    # 3. キーワードの和集合
    kw_survivor = set(survivor.keywords or [])
    kw_absorbed = set(absorbed.keywords or [])
    merged_keywords = sorted(kw_survivor | kw_absorbed)

    # 4. metadata の和集合（キー衝突は survivor 優先）
    # 吸収側が持つ persona_id 等のリンク情報を survivor に引き継ぐ。
    # 例: extractor 製ページ（metadata なし）と再会システム製ページ（persona_id 持ち）
    # の重複ペアで absorbed 側が persona_id を持っていた場合、和集合にしないと
    # 統合後に get_page_by_persona_id が個人ページを見失う。
    meta_absorbed = absorbed.metadata or {}
    meta_survivor = survivor.metadata or {}
    # absorbed のキーを下敷きにして survivor で上書き（survivor 優先）
    merged_metadata: Optional[Dict[str, Any]] = {**meta_absorbed, **meta_survivor} or None
    if not merged_metadata:
        merged_metadata = None

    # 5. 残す側を更新（Memopedia.update_page 経由で diff が刻まれる）
    memopedia.update_page(
        survivor_page_id,
        content=new_content,
        keywords=merged_keywords,
        edit_source="curation",
    )

    # metadata は Memopedia.update_page が受け取らないため、storage 層を直接呼ぶ。
    # 編集来歴の刻印は上の memopedia.update_page 呼び出しで担保されているため、
    # ここでは edit_source なしで純粋な値の更新のみ行う。
    if merged_metadata != (survivor.metadata or None):
        from sai_memory.memopedia.storage import update_page as _storage_update_page
        _storage_update_page(conn, survivor_page_id, metadata=merged_metadata)

    # 6. 吸収側を soft-delete
    memopedia.delete_page(absorbed_page_id, edit_source="curation")

    LOGGER.info(
        "[curation_ops] merge done: survivor=%s absorbed=%s (absorbed_title=%r) "
        "merged_content_len=%d children_moved=%d",
        survivor_page_id, absorbed_page_id, absorbed.title,
        len(new_content), children_moved,
    )
    return {
        "survivor_id": survivor_page_id,
        "absorbed_id": absorbed_page_id,
        "survivor_title": survivor.title,
        "absorbed_title": absorbed.title,
        "merged_content_len": len(new_content),
        "children_moved": children_moved,
    }


def plan_split(
    conn: sqlite3.Connection,
    page_id: str,
    llm_client: Any,
) -> Dict[str, Any]:
    """split の前段（読み取り＋LLM 呼び出しのみ。DB 書き込みなし）。

    **本文保存則**: LLM はブロックの「どの子に割り当てるか」だけを返し、
    本文テキストは一切出力させない。割当を受けて子ページ本文・親の残り本文を
    逐語で構成し、保存則を機械検証してから割当案を返す。

    保存則の機械検証（どちらか一方でも通らなければ ValueError で棄却。
    fail-safe、plan は "failed" になる）:
    1. 番号集合（補助）: 全ブロックがちょうど一度使われている
       （重複・漏れ・範囲外なし）
    2. 文字レベル復元（本則）: 全出力（子ページ本文＋親の残り本文）から
       元本文を機械的に復元して完全一致する

    Returns:
        dict: apply_split に渡す割当案 {
            "page_id": str,
            "title": str,
            "content": str,             # 割当時点の本文スナップショット（検証用）
            "sections": [{"title": str, "indices": [int], "content": str}],
            "remaining_indices": [int],
            "remaining_content": str,
            "total_blocks": int,
        }

    Raises:
        ValueError: ページが見つからない、本文が空、ブロックが 1 つ、
            LLM 応答不正、保存則違反
    """
    import json as _json
    from sai_memory.memopedia.storage import get_page

    page = get_page(conn, page_id)
    if page is None:
        raise ValueError(f"execute_split: ページが見つかりません: {page_id}")

    content = page.content or ""
    blocks = _split_into_blocks(content)
    total_blocks = len(blocks)

    if total_blocks == 0:
        raise ValueError(f"execute_split: ページの本文が空です: {page_id}")

    if total_blocks == 1:
        raise ValueError(
            f"execute_split: 段落ブロックが 1 つしかないため分割できません: {page_id}"
        )

    # --- LLM へのプロンプト（ブロック割当ラベルのみ要求。本文を出力させない） ---
    # 表示は strip して整える（プロンプト表示のみ。割当・移動は原文ブロック）。
    numbered_blocks = "\n\n".join(
        f"[ブロック{i}]\n{b.strip()}" for i, b in enumerate(blocks)
    )
    prompt = (
        f"以下のページ「{page.title}」の内容を、内容のまとまりに応じて"
        f"子ページへ分割してください。\n"
        f"各子ページのタイトルと、そのページに含めるブロックの番号リストを返してください。\n"
        f"全部で {total_blocks} 個のブロックがあります（番号は 0 始まり）。\n"
        f"本文テキストは出力しないでください。ブロック番号の割り当てのみ出力してください。\n\n"
        f"本文:\n{numbered_blocks}"
    )
    response_schema = {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "block_indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                    },
                    "required": ["title", "block_indices"],
                },
            },
            "remaining_block_indices": {
                "type": "array",
                "items": {"type": "integer"},
            },
        },
        "required": ["sections", "remaining_block_indices"],
    }

    try:
        raw_response = llm_client.generate(
            messages=[{"role": "user", "content": prompt}],
            response_schema=response_schema,
        )
        if isinstance(raw_response, str):
            parsed = _json.loads(raw_response)
        elif isinstance(raw_response, dict):
            parsed = raw_response
        else:
            raise ValueError(f"LLM の応答が不正な型: {type(raw_response)}")
    except Exception as exc:
        raise ValueError(f"execute_split: LLM 呼び出しに失敗: {exc}") from exc

    sections = parsed.get("sections") or []
    remaining_indices = [int(i) for i in (parsed.get("remaining_block_indices") or [])]

    if not sections and not remaining_indices:
        raise ValueError("execute_split: LLM の応答に sections も remaining も含まれていません")

    # --- 保存則の機械検証 (1): 番号集合（補助） ---
    all_assigned: List[int] = []
    for sec in sections:
        indices = [int(i) for i in (sec.get("block_indices") or [])]
        all_assigned.extend(indices)
    all_used = sorted(all_assigned + remaining_indices)
    expected = list(range(total_blocks))

    if all_used != expected:
        # 重複・漏れ・範囲外のいずれかがある → 棄却
        raise ValueError(
            f"execute_split: 保存則違反。元ブロック={expected} / 使用ブロック={all_used}。"
            "この分割を棄却します。"
        )

    # --- 逐語でブロックから子ページ本文・親の残り本文を構成 ---
    # ブロックは lossless（区切りの改行列を自身の末尾に含む）なので、
    # index 順の素の連結（"".join）で原文が保たれる。
    section_plans: List[Dict[str, Any]] = []
    for sec in sections:
        sec_title = str(sec.get("title") or "").strip() or "(無題)"
        indices = sorted(int(i) for i in (sec.get("block_indices") or []))
        if not indices:
            continue
        section_plans.append({
            "title": sec_title,
            "indices": indices,
            "content": "".join(blocks[i] for i in indices),
        })
    remaining_sorted = sorted(remaining_indices)
    remaining_content = "".join(blocks[i] for i in remaining_sorted)

    # --- 保存則の機械検証 (2): 文字レベルの復元（本則） ---
    # 復元規則: 各ブロックは割当先ページの本文中に index 昇順で連続して並ぶ。
    # よって元の index 順に、各出力本文を先頭から切り出して繋げば元本文に戻る。
    # 全出力が過不足なく消費され、復元結果が元本文と完全一致しなければ棄却。
    owner: Dict[int, int] = {}  # block index → section_plans の位置（-1 = 親の残り）
    for si, sp in enumerate(section_plans):
        for i in sp["indices"]:
            owner[i] = si
    for i in remaining_sorted:
        owner[i] = -1
    outputs: Dict[int, str] = {si: sp["content"] for si, sp in enumerate(section_plans)}
    outputs[-1] = remaining_content
    cursors: Dict[int, int] = {key: 0 for key in outputs}
    rebuilt_parts: List[str] = []
    for i in range(total_blocks):
        key = owner[i]
        start = cursors[key]
        end = start + len(blocks[i])
        rebuilt_parts.append(outputs[key][start:end])
        cursors[key] = end
    reconstructed = "".join(rebuilt_parts)
    fully_consumed = all(cursors[key] == len(outputs[key]) for key in outputs)
    if reconstructed != content or not fully_consumed:
        raise ValueError(
            "execute_split: 保存則違反。子ページ全本文＋親の残り本文から"
            "元本文を復元できません（文字レベル不一致）。この分割を棄却します。"
        )

    return {
        "page_id": page.id,
        "title": page.title,
        "content": content,
        "sections": section_plans,
        "remaining_indices": remaining_sorted,
        "remaining_content": remaining_content,
        "total_blocks": total_blocks,
    }


def apply_split(
    conn: sqlite3.Connection,
    memopedia: Any,
    split_plan: Dict[str, Any],
) -> Dict[str, Any]:
    """split の後段（DB 書き込みのみ。LLM なし）。

    plan_split が返した割当案を適用する——子ページを作成し、親を
    残りブロック＋子への導線に更新する。トランザクション境界とロックは
    呼び出し元（run_pending_plans）が握る。

    防御: 割当案の作成（LLM 呼び出し）と適用の間に本文が変わっていたら
    ValueError で棄却する（別スレッドの編集との競合で保存則が壊れるのを防ぐ）。

    Returns:
        dict: {
            "page_id": str,
            "sections": [{"title": str, "child_id": str, "block_count": int}],
            "remaining_block_count": int,
            "total_blocks": int,
        }

    Raises:
        ValueError: ページが見つからない、割当案作成後に本文が変更された
    """
    from sai_memory.memopedia.storage import get_page

    page_id = split_plan["page_id"]
    page = get_page(conn, page_id)
    if page is None:
        raise ValueError(f"execute_split: ページが見つかりません: {page_id}")
    if (page.content or "") != split_plan["content"]:
        raise ValueError(
            f"execute_split: 割当案の作成後に本文が変更されています: {page_id}。"
            "この分割を棄却します。"
        )

    # --- 逐語でブロックを移動して子ページを作成 ---
    created_sections: List[Dict[str, Any]] = []
    for sec in split_plan["sections"]:
        child_page = memopedia.create_page(
            parent_id=page_id,
            title=sec["title"],
            content=sec["content"],
            edit_source="curation",
        )
        created_sections.append({
            "title": sec["title"],
            "child_id": child_page.id,
            "block_count": len(sec["indices"]),
        })
        LOGGER.debug(
            "[curation_ops] split: created child page id=%s title=%r block_count=%d",
            child_page.id, sec["title"], len(sec["indices"]),
        )

    # --- 親は remaining ブロック ＋ 子への導線 ---
    guide_lines: List[str] = []
    for sec in created_sections:
        guide_lines.append(f"- [{sec['title']}]（子ページ）")
    if guide_lines:
        guide_section = "\n\n## 分割された節\n\n" + "\n".join(guide_lines)
    else:
        guide_section = ""
    new_parent_content = split_plan["remaining_content"] + guide_section

    memopedia.update_page(
        page_id,
        content=new_parent_content,
        edit_source="curation",
    )

    LOGGER.info(
        "[curation_ops] split done: page_id=%s total_blocks=%d "
        "sections=%d remaining_blocks=%d",
        page_id, split_plan["total_blocks"],
        len(created_sections), len(split_plan["remaining_indices"]),
    )
    return {
        "page_id": page_id,
        "sections": created_sections,
        "remaining_block_count": len(split_plan["remaining_indices"]),
        "total_blocks": split_plan["total_blocks"],
    }


def execute_split(
    conn: sqlite3.Connection,
    page_id: str,
    memopedia: Any,
    llm_client: Any,
) -> Dict[str, Any]:
    """split 実行（plan_split → apply_split の一括実行）。

    直接呼び出し用の後方互換 API。run_pending_plans は LLM 呼び出しを
    ロック・トランザクションの外に出すため、plan_split / apply_split を
    個別に呼ぶ（この関数は経由しない）。

    Raises:
        ValueError: ページが見つからない、保存則違反、LLM 応答不正
    """
    split_plan = plan_split(conn, page_id, llm_client)
    return apply_split(conn, memopedia, split_plan)


# ---------------------------------------------------------------------------
# pending プランの一括実行
# ---------------------------------------------------------------------------


def _resolve_page_id_from_ref(conn: sqlite3.Connection, ref: str) -> Optional[str]:
    """m:N 形式の参照ラベルを page_id (UUID) に解決する。

    検知層 (saiverse/curation.py) が生成するラベルは ``m:N`` (N = short_id) 形式。
    resolve_page_ref は ``memopedia:N`` を受け付けるため、``m:`` プレフィックスを
    ``memopedia:`` に正規化してから委譲する。
    """
    import re as _re
    from sai_memory.memopedia.storage import resolve_page_ref
    # m:N → memopedia:N に変換して resolve_page_ref に委譲する
    normalized = _re.sub(r"^m:(\d+)$", r"memopedia:\1", ref.strip())
    return resolve_page_ref(conn, normalized)


def run_pending_plans(manager: Any, persona_id: str) -> Dict[str, Any]:
    """pending の編纂プランを全実行する。

    - 各プランを順に実行し、status を done/failed に更新する。
    - **1 プラン = 1 トランザクション**: プラン内の全書き込み（ページ変更＋
      status=done）は成功時のみ commit され、途中失敗時は rollback されて
      DB はプラン実行前と同一の状態に戻る（翌朝報告の「ページは変更されて
      いません」が事実と一致する）。
    - split の LLM 呼び出し（plan_split）はロック・トランザクションの外で行う。
    - 個々のプランの失敗は他のプランを止めない（fail-safe）。
    - 実行後、翌朝のペルソナへの報告を event_message 形式で SAIMemory に書く。
    - desk 上の吸収側ページは既存の dropped_missing 機構が次の Metabolism
      snapshot で正直に下ろす（ここでは特別対応不要）。

    Returns:
        dict: {
            "done": [plan_id, ...],
            "failed": [plan_id, ...],
            "report_lines": [str, ...],  # event_message の本文行
        }
    """
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    if persona is None:
        LOGGER.warning(
            "[curation_ops] run_pending_plans: persona not found (persona=%s)", persona_id,
        )
        return {"done": [], "failed": [], "report_lines": []}

    adapter = getattr(persona, "sai_memory", None)
    mem_conn = getattr(adapter, "conn", None) if adapter is not None else None
    if mem_conn is None:
        LOGGER.warning(
            "[curation_ops] run_pending_plans: memory adapter not available (persona=%s)",
            persona_id,
        )
        return {"done": [], "failed": [], "report_lines": []}

    # 背景スレッドから走るため、メインスレッドの書き込み (adapter._db_lock 経由)
    # と同じロックを共有することが必須 — 別ロックの Memopedia を作ると
    # 同一 sqlite conn 上でトランザクションが交錯する。
    import threading as _threading
    db_lock = getattr(adapter, "_db_lock", None) or _threading.RLock()

    plans = list_pending(mem_conn)
    if not plans:
        LOGGER.debug(
            "[curation_ops] run_pending_plans: no pending plans (persona=%s)", persona_id,
        )
        return {"done": [], "failed": [], "report_lines": []}

    # 編纂は 1 プラン = 1 トランザクションで実行する。storage 層は各操作で
    # commit() する規約のため、編纂経路には commit を保留するプロキシ conn
    # （とそれに束ねた Memopedia）を渡し、_plan_transaction が成功時のみ
    # 実 conn を commit / 失敗時は rollback する。
    tx_conn = _NonCommittingConnection(mem_conn)
    from sai_memory.memopedia.core import Memopedia
    with db_lock:
        tx_memopedia = Memopedia(tx_conn, db_lock=db_lock)
        # Memopedia.__init__ の冪等初期化（DDL / seed）がプラン本体の
        # トランザクションに混ざらないよう、ここで確定しておく。
        mem_conn.commit()

    done_ids: List[str] = []
    failed_ids: List[str] = []
    report_lines: List[str] = []

    # LLM クライアント（split でのみ必要）
    _llm_client: Any = None

    def _get_llm_client() -> Any:
        nonlocal _llm_client
        if _llm_client is not None:
            return _llm_client
        try:
            # ペルソナの LIGHTWEIGHT_MODEL を優先
            lite_model = None
            persona_obj = (getattr(manager, "personas", None) or {}).get(persona_id)
            if persona_obj is not None:
                lite_model = getattr(persona_obj, "LIGHTWEIGHT_MODEL", None)
            if not lite_model:
                import os
                lite_model = os.environ.get("SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL")
            if not lite_model:
                from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
                lite_model = BUILTIN_DEFAULT_LITE_MODEL
            from saiverse.model_configs import find_model_config
            resolved_model_id, model_config = find_model_config(lite_model)
            if resolved_model_id is None:
                raise RuntimeError(f"モデル設定が見つかりません: {lite_model}")
            actual_model = model_config.get("model", resolved_model_id)
            context_length = model_config.get("context_length", 32768)
            provider = model_config.get("provider", "gemini")
            from llm_clients.factory import get_llm_client
            _llm_client = get_llm_client(actual_model, provider, context_length, config=model_config)
        except Exception as exc:
            LOGGER.warning(
                "[curation_ops] run_pending_plans: failed to init LLM client: %s", exc,
            )
        return _llm_client

    for plan in plans:
        plan_id = plan["id"]
        kind = plan["kind"]
        refs = plan.get("refs") or []
        op_id = plan["op_id"]

        LOGGER.info(
            "[curation_ops] run_pending_plans: executing plan_id=%s kind=%s op_id=%r",
            plan_id, kind, op_id,
        )

        try:
            if kind in ("merge", "fold"):
                if len(refs) < 2:
                    raise ValueError(f"merge/fold には 2 つの refs が必要 (got {refs})")
                survivor_ref, absorbed_ref = refs[0], refs[1]
                survivor_id = _resolve_page_id_from_ref(mem_conn, survivor_ref)
                absorbed_id = _resolve_page_id_from_ref(mem_conn, absorbed_ref)
                if survivor_id is None:
                    raise ValueError(f"survivor ページが見つかりません: {survivor_ref}")
                if absorbed_id is None:
                    raise ValueError(f"absorbed ページが見つかりません: {absorbed_ref}")
                with db_lock:
                    with _plan_transaction(mem_conn):
                        result = execute_merge(
                            tx_conn, survivor_id, absorbed_id, tx_memopedia,
                        )
                        # プラン状態の done も同一トランザクションで確定する
                        # （ページ変更だけ確定して状態が pending のまま残り、
                        # 次回バッチで二重実行される窓を無くす）。
                        _update_plan_status(tx_conn, plan_id, STATUS_DONE, result)
                report_lines.append(
                    f"- [{kind}] {refs[0]}「{result['survivor_title']}」に {refs[1]}「{result['absorbed_title']}」を"
                    f"統合しました（{result['merged_content_len']:,}字、"
                    f"子ページ {result['children_moved']} 件の付け替え）。"
                    "編集来歴から差し戻せます。"
                )
            elif kind == "split":
                if not refs:
                    raise ValueError(f"split には refs が必要 (got {refs})")
                page_ref = refs[0]
                page_id_resolved = _resolve_page_id_from_ref(mem_conn, page_ref)
                if page_id_resolved is None:
                    raise ValueError(f"分割対象ページが見つかりません: {page_ref}")
                llm = _get_llm_client()
                if llm is None:
                    raise RuntimeError("LLM クライアントの初期化に失敗しました")
                # LLM 呼び出し（plan_split）はロック・トランザクションの外。
                # ロックを LLM コール中に保持してはいけない。
                split_plan = plan_split(mem_conn, page_id_resolved, llm)
                with db_lock:
                    with _plan_transaction(mem_conn):
                        result = apply_split(tx_conn, tx_memopedia, split_plan)
                        _update_plan_status(tx_conn, plan_id, STATUS_DONE, result)
                section_names = [s["title"] for s in result.get("sections", [])]
                report_lines.append(
                    f"- [split] {page_ref} を {len(section_names)} 件の子ページに分割しました"
                    f"（{', '.join(section_names[:3])}{'…' if len(section_names) > 3 else ''}）。"
                    "編集来歴から差し戻せます。"
                )
            else:
                raise ValueError(f"未知の kind: {kind!r}")

            done_ids.append(plan_id)
            LOGGER.info(
                "[curation_ops] run_pending_plans: plan_id=%s done", plan_id,
            )

        except Exception as exc:
            LOGGER.warning(
                "[curation_ops] run_pending_plans: plan_id=%s failed: %s",
                plan_id, exc, exc_info=True,
            )
            with db_lock:
                # トランザクション内の失敗は _plan_transaction が rollback 済み。
                # ここでの rollback はトランザクション前段（refs 解決・LLM 等）の
                # 失敗時は no-op だが、「failed 状態の commit が部分変更を道連れに
                # 確定する」事故を構造的に塞ぐ保険として置く。
                try:
                    mem_conn.rollback()
                except Exception:
                    LOGGER.warning(
                        "[curation_ops] run_pending_plans: rollback failed "
                        "(plan_id=%s)", plan_id, exc_info=True,
                    )
                _update_plan_status(mem_conn, plan_id, STATUS_FAILED, {"error": str(exc)})
            failed_ids.append(plan_id)
            report_lines.append(
                f"- [{kind}] {', '.join(refs)} の編纂に失敗しました（{exc}）。"
                "ページは変更されていません。"
            )

    # --- 翌朝ペルソナへの event_message（翌朝届く報告） ---
    _write_curation_report(
        adapter=adapter,
        persona_id=persona_id,
        done_count=len(done_ids),
        failed_count=len(failed_ids),
        report_lines=report_lines,
    )

    LOGGER.info(
        "[curation_ops] run_pending_plans: persona=%s done=%d failed=%d",
        persona_id, len(done_ids), len(failed_ids),
    )
    return {
        "done": done_ids,
        "failed": failed_ids,
        "report_lines": report_lines,
    }


def _write_curation_report(
    adapter: Any,
    persona_id: str,
    done_count: int,
    failed_count: int,
    report_lines: List[str],
) -> None:
    """編纂完了報告を event_message 形式で SAIMemory に書く。

    翌朝のペルソナの文脈（tail）に届く。
    機構の名義（user ロール ＋ system タグ）で書く——ペルソナ名義で書かない。
    event_message タグ必須（タグ漏れでコンテキストに乗らない事故を防ぐ）。
    """
    if adapter is None:
        return
    append_fn = getattr(adapter, "append_persona_message", None)
    if not callable(append_fn):
        return

    if done_count == 0 and failed_count == 0:
        # 実行対象がなかった（実際には run_pending_plans がガードするが念のため）
        return

    header = "[システム通知: 夜の間に棚の整理が行われました]"
    body_lines: List[str] = [header, ""]
    if report_lines:
        body_lines.extend(report_lines)
    else:
        body_lines.append("（操作の詳細が取得できませんでした）")
    body_lines.append("")
    if done_count > 0:
        body_lines.append(f"完了: {done_count} 件")
    if failed_count > 0:
        body_lines.append(f"失敗: {failed_count} 件（ページは変更されていません）")
    body_lines.append("")
    body_lines.append("※ 変更は編集来歴（メモリタブ > 来歴）から差し戻せます。")

    message_content = "<system>" + "\n".join(body_lines) + "</system>"

    try:
        append_fn({
            "role": "user",
            "content": message_content,
            "metadata": {"tags": ["internal", "event_message", "curation"]},
        })
        LOGGER.info(
            "[curation_ops] curation report written to SAIMemory (persona=%s "
            "done=%d failed=%d)",
            persona_id, done_count, failed_count,
        )
    except Exception:
        LOGGER.warning(
            "[curation_ops] failed to write curation report (persona=%s)",
            persona_id, exc_info=True,
        )
