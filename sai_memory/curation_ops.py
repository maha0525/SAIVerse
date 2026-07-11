"""編纂プランの永続化層＋実行部 — per-persona memory.db の curation_plans テーブル。

P4-a の三層（検知 → 裁定 → 実行）のうち「裁定から実行への橋渡し」と
「実行本体」を担う。

就寝判断（day_close）の finalize が approve された op_id を
``enqueue_plan`` でここに書き込み、背景スレッドが ``run_pending_plans``
で実行する。

実行関数（P4-a2 実装）:
    execute_merge(conn, survivor_page_id, absorbed_page_id, memopedia) -> dict
        完全決定論・LLM ゼロ。残す側本文 ＋ 区切り ＋ 吸収側本文の逐語連結。
    execute_split(conn, page_id, memopedia, llm_client) -> dict
        LLM はブロック割当ラベルのみ。保存則の機械検証あり（違反は棄却）。
    run_pending_plans(manager, persona_id) -> dict
        pending プランを全実行。個々の失敗は他を止めない。

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
    """本文を段落ブロックのリストに分割する（決定論）。

    規則:
    - 空行（空白のみを含む行も含む）でブロックを区切る。
    - 見出し行（# で始まる行）は次のブロックの先頭に付ける——
      見出しがブロック末に孤立しないようにする。
    - 空ブロック（空文字列）は除去する。

    この分割は可逆であること（ブロックを連結すれば元に戻る）が重要。
    空行の完全復元には join("\n\n") を使う。
    """
    # まず空行（連続した改行）で粗く分割
    raw_blocks: List[str] = re.split(r"\n{2,}", content)
    # 空ブロックは除去
    raw_blocks = [b.strip() for b in raw_blocks if b.strip()]

    # 見出し行が前のブロックの末尾になっていたら次のブロックへ移す
    result: List[str] = []
    pending_heading: Optional[str] = None
    for block in raw_blocks:
        lines = block.split("\n")
        if pending_heading is not None:
            block = pending_heading + "\n" + block
            pending_heading = None
        # ブロックが見出し行のみなら、次のブロックへ繰り越す
        non_empty = [l for l in lines if l.strip()]
        if len(non_empty) == 1 and non_empty[0].startswith("#"):
            pending_heading = non_empty[0]
            continue
        result.append(block)
    # 最後に見出し行が余ったらそのままブロックとして追加
    if pending_heading is not None:
        result.append(pending_heading)
    return result


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


def execute_split(
    conn: sqlite3.Connection,
    page_id: str,
    memopedia: Any,
    llm_client: Any,
) -> Dict[str, Any]:
    """split 実行（LLM はブロック割当ラベルのみ、保存則の機械検証あり）。

    **本文保存則**: LLM はブロックの「どの子に割り当てるか」だけを返し、
    本文テキストは一切出力させない。コードがブロックを逐語で移動する。

    保存則の機械検証: 子ページ全部＋親の残りブロック集合 ＝ 元の全ブロック
    （各ブロックがちょうど一度）でなければ、その分割を**棄却**して ValueError を
    送出する（fail-safe、plan は "failed" になる）。

    Returns:
        dict: {
            "page_id": str,
            "sections": [{"title": str, "child_id": str, "block_count": int}],
            "remaining_block_count": int,
            "total_blocks": int,
        }

    Raises:
        ValueError: ページが見つからない、保存則違反、LLM 応答不正
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
    numbered_blocks = "\n\n".join(
        f"[ブロック{i}]\n{b}" for i, b in enumerate(blocks)
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

    # --- 保存則の機械検証 ---
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

    # --- 逐語でブロックを移動して子ページを作成 ---
    created_sections: List[Dict[str, Any]] = []
    for sec in sections:
        sec_title = str(sec.get("title") or "").strip() or "(無題)"
        indices = [int(i) for i in (sec.get("block_indices") or [])]
        if not indices:
            continue
        # ブロックを index 順に並べて連結（保存則: 逐語）
        child_content = "\n\n".join(blocks[i] for i in sorted(indices))
        child_page = memopedia.create_page(
            parent_id=page_id,
            title=sec_title,
            content=child_content,
            edit_source="curation",
        )
        created_sections.append({
            "title": sec_title,
            "child_id": child_page.id,
            "block_count": len(indices),
        })
        LOGGER.debug(
            "[curation_ops] split: created child page id=%s title=%r block_count=%d",
            child_page.id, sec_title, len(indices),
        )

    # --- 親は remaining ブロック ＋ 子への導線 ---
    remaining_content = "\n\n".join(
        blocks[i] for i in sorted(remaining_indices)
    ) if remaining_indices else ""
    guide_lines: List[str] = []
    for sec in created_sections:
        guide_lines.append(f"- [{sec['title']}]（子ページ）")
    if guide_lines:
        guide_section = "\n\n## 分割された節\n\n" + "\n".join(guide_lines)
    else:
        guide_section = ""
    new_parent_content = remaining_content + guide_section

    memopedia.update_page(
        page_id,
        content=new_parent_content,
        edit_source="curation",
    )

    LOGGER.info(
        "[curation_ops] split done: page_id=%s total_blocks=%d "
        "sections=%d remaining_blocks=%d",
        page_id, total_blocks, len(created_sections), len(remaining_indices),
    )
    return {
        "page_id": page_id,
        "sections": created_sections,
        "remaining_block_count": len(remaining_indices),
        "total_blocks": total_blocks,
    }


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

    # Memopedia インスタンスを取得（adapter が持つ場合 / なければ直接生成）。
    # 背景スレッドから走るため、メインスレッドの書き込み (adapter._db_lock 経由)
    # と同じロックを共有することが必須 — 別ロックの Memopedia を作ると
    # 同一 sqlite conn 上でトランザクションが交錯する。
    import threading as _threading
    db_lock = getattr(adapter, "_db_lock", None) or _threading.RLock()
    memopedia = getattr(adapter, "memopedia", None)
    if memopedia is None:
        from sai_memory.memopedia.core import Memopedia
        memopedia = Memopedia(mem_conn, db_lock=db_lock)

    plans = list_pending(mem_conn)
    if not plans:
        LOGGER.debug(
            "[curation_ops] run_pending_plans: no pending plans (persona=%s)", persona_id,
        )
        return {"done": [], "failed": [], "report_lines": []}

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
                    result = execute_merge(mem_conn, survivor_id, absorbed_id, memopedia)
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
                result = execute_split(mem_conn, page_id_resolved, memopedia, llm)
                section_names = [s["title"] for s in result.get("sections", [])]
                report_lines.append(
                    f"- [split] {page_ref} を {len(section_names)} 件の子ページに分割しました"
                    f"（{', '.join(section_names[:3])}{'…' if len(section_names) > 3 else ''}）。"
                    "編集来歴から差し戻せます。"
                )
            else:
                raise ValueError(f"未知の kind: {kind!r}")

            with db_lock:
                _update_plan_status(mem_conn, plan_id, STATUS_DONE, result)
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
