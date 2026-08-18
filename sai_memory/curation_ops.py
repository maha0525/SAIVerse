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
    kind        TEXT                     -- "split" | "merge"（"fold" は 2026-08-05 に撤去）
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

#: 編纂プランとして受け付ける操作。"fold"（過小ページを親へ畳む）は
#: 2026-08-05 に機構ごと撤去した（docs/issues/curation_duplicate_pages_loop.md）。
VALID_PLAN_KINDS = frozenset({"split", "merge"})


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
        kind:   "split" | "merge"（"fold" は 2026-08-05 に撤去。受理しない）
        op_id:  検知層が付けた決定論の一意 ID（例: "split:m:12"）
        refs:   操作対象ページの参照ラベル（例: ["m:12"]）

    Returns:
        既存 pending 行の id、または新規挿入した行の id。

    P4-a2 の領分（このモジュールでは実装しない）:
        - merge 本体（残す側に消える側を逐語結合、子ページ付け替え、soft-delete）
        - split 本体（段落ブロック割り当て + コード逐語移動 + 保存則機械検証）
        - status を "done"/"failed" に更新し result_json / executed_at を書く
    """
    if kind not in VALID_PLAN_KINDS:
        # fold は 2026-08-05 に撤去。撤去した機構の行を新規に作れる口を残さない
        # （runner の未知 kind ガードは最後の砦であって入口の検査ではない）。
        raise ValueError(
            f"enqueue_plan: 未知の kind: {kind!r}"
            f"（有効なのは {sorted(VALID_PLAN_KINDS)}）"
        )

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
        ORDER BY created_at, id
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


def _assert_active(conn: sqlite3.Connection, page_id: str, role: str) -> None:
    """閉架（soft-delete）済みページへの編纂を拒否する。

    `get_page` は `is_deleted` を見ない（ごみ箱からの復元経路が読むため）。
    編纂が閉架ページを掴むと、統合先が閉架ページになって本文が現役の棚に
    届かない・削除済みページを分割する、といった無言の取りこぼしになる。
    実行の入口で弾いてプランを failed にし、「ページは変更されていません」を
    事実にする（Codex 指摘 2026-08-05）。
    """
    row = conn.execute(
        "SELECT COALESCE(is_deleted, 0) FROM memopedia_pages WHERE id = ?",
        (page_id,),
    ).fetchone()
    if row is not None and row[0]:
        raise ValueError(
            f"編纂の対象が閉架済みです（{role}: {page_id}）。この操作を棄却します。"
        )


def normalize_title(title: str) -> str:
    """タイトル同一判定の正規化（空白除去＋小文字化）。

    「同名」を判定する唯一の入口。検知層 (`saiverse.curation`) と分割の適用
    (`plan_split` / `apply_split`) が同じ規則で「同じ名前」を決めるために共有する。
    """
    return re.sub(r"\s+", "", title or "").lower()


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


def build_merged_content(
    *,
    survivor_content: str,
    absorbed_summary: str,
    absorbed_content: str,
) -> str:
    """統合後の本文を組み立てる（完全決定論・LLM ゼロ）。

    **検知層と実行層が同じ規則を使うための唯一の入口**。健全性規則の
    「統合した結果が肥大するなら候補にしない」（concept_consolidation.md）は
    検知の時点で結果の文字数を知る必要があり、そこで別式を書くと実行結果と
    ずれる。`saiverse.curation` はこの関数の戻り値の長さで判定する。

    区切り見出し（旧 `## 統合: 旧「X」より`）は書かない——統合したらそれは
    もう単一のページであり、由来は編集来歴が持つ（まはー裁定 2026-08-05、
    memopedia_body_to_fragment.md §7 (b)）。
    """
    # **入力の本文には一切触らない**。中身があるかの判定にだけ真偽値を使い、
    # 連結には原文を渡す——rstrip / strip は末尾改行・行末スペース（Markdown の
    # ハードブレーク）・インデントを落とす＝本文の改変であり、「編纂は本文を
    # 生成しない、移動と結合のみ」の保存則に反する（Codex 指摘 2026-08-05）。
    # 欠損扱いにするのは「文字が 1 つも無い」場合だけ。空白や改行だけの本文も
    # 保存則の対象（吸収側はこの直後に閉架されるので、ここで捨てると現役の棚に
    # 原文が残らない。Codex 指摘 2026-08-05）。
    absorbed_parts: List[str] = [
        part for part in (absorbed_summary, absorbed_content) if part
    ]
    if not absorbed_parts:
        # 吸収側に文字が無い＝保存すべき本文が無い。埋め草も区切りも書かない
        return survivor_content or ""
    absorbed_body = "\n\n".join(absorbed_parts)
    if not (survivor_content or ""):
        return absorbed_body
    return (survivor_content or "") + "\n\n" + absorbed_body


def execute_merge(
    conn: sqlite3.Connection,
    survivor_page_id: str,
    absorbed_page_id: str,
    memopedia: Any,
) -> Dict[str, Any]:
    """merge 実行（完全決定論・LLM ゼロ）。

    **本文保存則**: 残す側本文 ＋ 吸収側 summary（あれば）＋ 吸収側本文を
    逐語で連結して残す側に書き込む（区切り見出しは書かない — 由来は編集来歴）。
    LLM は呼ばない——「新しい文章を生成しない」が不変条件。

    処理の流れ:
    1. 残す側・吸収側のページを読む
    2. 吸収側の子ページを残す側へ付け替え（move_pages_to_parent）
    3. 残す側の本文を逐語連結で更新（Memopedia.update_page, edit_source="curation"）
    4. キーワードを和集合にして残す側に書き込む
    5. 吸収側を soft-delete（Memopedia.delete_page, edit_source="curation"）

    **親子を渡して fold の代わりに使わないこと**。fold（過小ページを親へ統合）は
    2026-08-05 に撤去した機構で、この関数はその後継ではない。消える側が実際の
    ページを親に持つ組み合わせは検知層が候補にしない（健全性規則）——ここに
    「親子でも呼べる」と書いてあると、撤去した操作を merge 名義で再現する道案内に
    なる（Codex 指摘 2026-08-05）。

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

    Note:
        「消える側が実際のページを親にも子にも持たない」「統合後が肥大しない」
        は**検知層** (`saiverse.curation`) が候補の段階で保証する（健全性規則
        2026-08-05、まはー裁定「もっと前に見て候補から外せる」）。実行部で
        再判定しないのは、承認済みのプランを実行時に覆すと「承認したのに
        失敗した」がペルソナに返るため——不変条件は候補生成側が持つ。
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

    # Chronicle エントリは merge の対象外 — soft-delete が知覚バッチの付記印を
    # 孤立させる保護 (ChronicleProtectedError) と同族。検知層は chronicle を候補に
    # しないが、**変更前**に検証してプランを failed に落とす — 検証なしで進むと
    # 手前の update/移動が済んだ後の delete_page 例外で中途半端に止まり、
    # プランが done 扱いにならないまま部分変更だけが残る (2026-08-19 Codex
    # 第四巡 #1)。
    for label, page in (("survivor", survivor), ("absorbed", absorbed)):
        if page.category == "chronicle":
            raise ValueError(
                f"execute_merge: {label} ページ ({page.id}) は Chronicle エントリ"
                "です — Chronicle は編纂系の専用経路でのみ操作できます"
            )

    # 同じ晩の先行プランで閉架された相手を掴んでいないか（1 ページ 1 晩 1 操作を
    # 検知側で守っているが、実行側でも閉架だけは弾く）
    _assert_active(conn, survivor.id, "survivor")
    _assert_active(conn, absorbed.id, "absorbed")

    # 親子の統合＝撤去した fold そのもの。健全性の再判定ではなく「もう存在しない
    # 操作を merge 名義で実行させない」ための拒否。検知層は親子を候補にしないので、
    # ここに来るのは直接呼び出しか壊れた/古いプラン行だけ（Codex 指摘 2026-08-05）。
    if survivor.id == absorbed.parent_id or absorbed.id == survivor.parent_id:
        raise ValueError(
            "execute_merge: 親子ページの統合は撤去済みの操作 (fold) です"
            f"（survivor={survivor_page_id} absorbed={absorbed_page_id}）。棄却します。"
        )

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
    new_content = build_merged_content(
        survivor_content=survivor.content or "",
        absorbed_summary=absorbed.summary or "",
        absorbed_content=absorbed.content or "",
    )

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

    # 5.5 吸収側の Fragment を survivor へ付け替える。Fragment の想起可視性は
    # 親ページの生存に従う (unified_recall の _FRAGMENT_VISIBILITY_*) ため、
    # 付け替えずに soft-delete すると吸収側の Fragment は DB に残ったまま
    # keyword / embedding 検索から恒久的に消える (Codex 指摘 2026-08-06)。
    # embedding は「親タイトル: 本文」で生成されているので、旧親タイトルを含む
    # 既存ベクトルは捨てる —— 未生成の Fragment を拾う既存経路
    # (embed_memopedia_fragments) が survivor のタイトルで作り直す。
    moved_frag_ids = [
        row[0] for row in conn.execute(
            "SELECT id FROM memopedia_fragments WHERE entity_id = ?",
            (absorbed_page_id,),
        )
    ]
    if moved_frag_ids:
        conn.execute(
            "UPDATE memopedia_fragments SET entity_id = ? WHERE entity_id = ?",
            (survivor_page_id, absorbed_page_id),
        )
        for i in range(0, len(moved_frag_ids), 500):
            chunk = moved_frag_ids[i:i + 500]
            holes = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM memopedia_fragment_embeddings "
                f"WHERE fragment_id IN ({holes})",
                chunk,
            )

    # 6. 吸収側を soft-delete
    memopedia.delete_page(absorbed_page_id, edit_source="curation")

    LOGGER.info(
        "[curation_ops] merge done: survivor=%s absorbed=%s (absorbed_title=%r) "
        "merged_content_len=%d children_moved=%d fragments_moved=%d",
        survivor_page_id, absorbed_page_id, absorbed.title,
        len(new_content), children_moved, len(moved_frag_ids),
    )
    return {
        "survivor_id": survivor_page_id,
        "absorbed_id": absorbed_page_id,
        "survivor_title": survivor.title,
        "absorbed_title": absorbed.title,
        "merged_content_len": len(new_content),
        "children_moved": children_moved,
        "fragments_moved": len(moved_frag_ids),
    }


def plan_split(
    conn: sqlite3.Connection,
    page_id: str,
    llm_client: Any,
) -> Dict[str, Any]:
    """split の前段（読み取り＋LLM 呼び出しのみ。DB 書き込みなし）。

    **本文保存則**: LLM はブロックの「どの子に割り当てるか」だけを返し、
    本文テキストは一切出力させない。割当を受けて子ページ本文・親の残り本文を
    逐語で構成する。

    保存則は**棄却ではなく構造で**満たす。LLM の応答は正規化して必ず受け入れ、
    各ブロックの行き先はコードが一意に決める（`claims` → `owner`）。したがって
    重複・漏れ・範囲外を含む応答でも「子ページ全部＋親の残り＝元本文」は常に
    成立する——不正な応答は存在しない。

    **仕様と安全網の区別**: LLM に伝えてある契約は「同じブロックを複数の子に
    挙げない」「どの子にも当てはまらないブロックは挙げなくてよい（親に残る）」
    の 2 つ。前者に違反した重複の親送りと範囲外の無視は**安全網**であって仕様
    ではない——プロンプトでフォールバック挙動を説明すると「迷ったら両方に挙げて
    親に流す」という使い方を教えることになるので、単に禁止として伝える
    （まはー裁定 2026-07-15）。

    残りブロックは補集合として導出するので、LLM には出力させない（導出可能な
    情報を書かせると、子にも残りにも入る矛盾が生まれるだけ）。

    Returns:
        dict: apply_split に渡す割当案 {
            "page_id": str,
            "title": str,
            "content": str,             # 割当時点の本文スナップショット（検証用）
            "sections": [{"title": str, "summary": str, "indices": [int], "content": str}],
            "remaining_indices": [int],
            "remaining_content": str,
            "total_blocks": int,
        }

    Raises:
        ValueError: ページが見つからない、本文が空、ブロックが 1 つ、
            LLM 呼び出しの失敗、応答に sections が無い
    """
    import json as _json
    from sai_memory.memopedia.storage import get_children, get_page

    # 参照の受理は runner と同じ一本（m:N / memopedia:N / 素の数字 / UUID）。
    # ここで独自解決を書くと、同じ候補ラベルが経路によって通ったり通らなかったり
    # する（Codex 指摘 2026-08-05）。
    resolved_id = _resolve_page_id_from_ref(conn, page_id)
    page = get_page(conn, resolved_id) if resolved_id else None
    if page is None:
        raise ValueError(f"execute_split: ページが見つかりません: {page_id}")
    _assert_active(conn, page.id, "split 対象")

    content = page.content or ""
    blocks = _split_into_blocks(content)
    total_blocks = len(blocks)

    if total_blocks == 0:
        raise ValueError(f"execute_split: ページの本文が空です: {page_id}")

    if total_blocks == 1:
        raise ValueError(
            f"execute_split: 段落ブロックが 1 つしかないため分割できません: {page_id}"
        )

    # 既存の子ページ（同名の子を新規に作らせず、既にある棚へ入れさせるため）。
    # get_children は参照解決を持たないので解決済みの page.id で引く。
    existing_children = get_children(conn, page.id)
    # 同名の兄弟が複数いる場合はいちばん古い 1 枚を指す（apply_split と同じ規則）
    existing_by_title: Dict[str, Any] = {}
    for _child in sorted(existing_children, key=lambda c: (c.created_at, c.id)):
        existing_by_title.setdefault(normalize_title(_child.title), _child)

    # --- LLM へのプロンプト（ブロック割当ラベルのみ要求。本文を出力させない） ---
    # 表示は strip して整える（プロンプト表示のみ。割当・移動は原文ブロック）。
    numbered_blocks = "\n\n".join(
        f"[ブロック{i}]\n{b.strip()}" for i, b in enumerate(blocks)
    )
    # 既存の子ページを見せる理由（健全性規則 2026-08-05）: 見せないと LLM は
    # 毎回ゼロから分類軸を立て、同じ対象のページからは内容が違っても同じ軸が
    # 立つ。実機 aifi_city_a で同名ページが 5 枚まで増えた（互いに共通行ゼロ＝
    # 中身は別物）。ただしこれは応答の質の改善であって保証ではない——同名を
    # 作れなくするのは apply 側の物理拘束が担う。
    if existing_children:
        # 提示は正規化タイトルごとに 1 行、実際の追記先（最古の 1 枚）の概要を出す。
        # 既に同名の兄弟が並んでいる棚で全部を素の順（get_children は title 順で
        # 同名の間は未定義）に出すと、提示順が実行ごとに変わり、同じ名前が二度
        # 並んで LLM が区別できない（Codex 指摘 2026-08-05）。
        children_lines = "\n".join(
            f"- 「{c.title}」: {(c.summary or '（概要なし）').strip()}"
            for c in sorted(
                existing_by_title.values(), key=lambda c: (c.created_at, c.id)
            )
        )
        existing_section = (
            f"\nこのページには既に次の子ページがあります。\n"
            f"{children_lines}\n"
            f"**新しく立てるより既存の子ページに入れる方が適切なブロックは、"
            f"その子ページのタイトルをそのまま使ってください**"
            f"（その子ページに追記されます）。\n"
        )
    else:
        existing_section = ""
    prompt = (
        f"以下のページ「{page.title}」の内容を、内容のまとまりに応じて"
        f"子ページへ分割してください。\n"
        f"全部で {total_blocks} 個のブロックがあります（番号は 0 始まり）。\n"
        f"{existing_section}\n"
        f"手順:\n"
        f"1. child_pages: 割り当て先の子ページのタイトルと概要を**先に全部**"
        f"挙げてください（既存の子ページを使う場合もここに挙げます）。\n"
        f"   概要は、そのページを開かなくても何が書かれているか分かる 1〜2 文。\n"
        f"2. sections: 1 で挙げたタイトルごとに、そのページに含めるブロックの"
        f"番号リストを返してください。\n\n"
        f"同じブロックを複数の子ページに挙げないでください。\n"
        f"どの子ページにも当てはまらないブロックは、挙げなくて構いません"
        f"（親ページの本文に残ります）。\n"
        f"親ページと同じタイトル「{page.title}」を子ページに使わないでください。\n"
        f"本文テキストは出力しないでください。ブロック番号の割り当てのみ出力してください。\n\n"
        f"本文:\n{numbered_blocks}"
    )
    # プロパティの定義順がそのまま Gemini の property_ordering になる＝生成順になる。
    # child_pages を先頭に置き、**どういう子ページを作るかを全部宣言させてから**
    # ブロックを振り分けさせる。順序が逆（タイトル→そのブロック群、を繰り返す）だと、
    # 1 枚目のタイトルを宣言した時点で 2 枚目以降が想定できておらず、「いま宣言した
    # タイトルに関連する」で全ブロックを 1 枚目に流し込む応答が出る
    # （実機 2026-07-14/15: m:34 が 163 ブロック全部を 1 枚に入れ、後から空セクション
    # 「残りのブロック」を足して辻褄を合わせた。肥大が解消しないので翌晩また検知され、
    # 毎晩 1 段ずつ入れ子が深くなるループになっていた。まはー指摘 2026-07-15）。
    # 概要も宣言側で書かせる: 「どういうページを作るか」の宣言が具体的なほど、
    # 後続の振り分けがその設計に条件づけられる。
    response_schema = {
        "type": "object",
        "properties": {
            "child_pages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "required": ["title", "summary"],
                },
            },
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
        },
        "required": ["child_pages", "sections"],
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

    if not sections:
        raise ValueError("execute_split: LLM の応答に sections が含まれていません")

    # child_pages は「先に全部の子ページを宣言させる」ための足場であって、割当の
    # 本体は sections。宣言された概要はタイトルで引いて子ページに渡す（概要は本文
    # ではないので、保存則が禁じる「本文の生成」には当たらない）。
    declared_summary: Dict[str, str] = {}
    declared_titles: List[str] = []
    for cp in (parsed.get("child_pages") or []):
        if not isinstance(cp, dict):
            continue
        t = str(cp.get("title") or "").strip()
        if not t:
            continue
        declared_titles.append(t)
        declared_summary[t] = str(cp.get("summary") or "").strip()
    assigned_titles = [str(sec.get("title") or "").strip() for sec in sections]
    if declared_titles and declared_titles != assigned_titles:
        # 宣言が効いていない兆候。割当は sections をそのまま使う（棄却はしない）が、
        # 概要が引けない子ページが出るので観測できるようにする。
        LOGGER.warning(
            "[curation_ops] plan_split: child_pages と sections のタイトルが不一致 "
            "(page=%s declared=%r assigned=%r)", page_id, declared_titles, assigned_titles,
        )

    # --- 割当の正規化: LLM の応答をそのまま受け入れ、行き先を機械が一意に決める ---
    # 保存則は「棄却」でなく「構造」で満たす。LLM が何を返しても行き先の決定は
    # ここで一意に閉じる——不正な応答が存在しない。
    #   - ちょうど 1 つの子が挙げた → その子へ移動
    #   - どの子も挙げなかった      → 親に残す（**仕様**。remaining を LLM に
    #     出力させない代わりで、プロンプトでもそう伝えてある）
    #   - 2 つ以上の子が挙げた      → 親に残す（**安全網**。重複はプロンプトで
    #     禁じてあり、これは守られなかった場合の受け皿。仕様として当てにしない
    #     ——「迷ったら両方に挙げれば親に残る」という使い方はさせない）
    #   - 範囲外の番号              → 無視（同上、安全網）
    claims: Dict[int, List[int]] = {}  # block index → 挙げた section の位置（重複含む）
    for si, sec in enumerate(sections):
        for raw in (sec.get("block_indices") or []):
            try:
                i = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= i < total_blocks:
                claims.setdefault(i, []).append(si)

    owner: Dict[int, int] = {}  # block index → section の位置（-1 = 親の残り）
    for i in range(total_blocks):
        claimed_by = set(claims.get(i) or ())
        owner[i] = claimed_by.pop() if len(claimed_by) == 1 else -1

    # --- 逐語でブロックから子ページ本文・親の残り本文を構成 ---
    # ブロックは lossless（区切りの改行列を自身の末尾に含む）なので、
    # index 順の素の連結（"".join）で原文が保たれる。
    section_plans: List[Dict[str, Any]] = []
    section_pos_map: Dict[int, int] = {}  # 元の section 位置 → section_plans の位置
    plan_pos_by_title: Dict[str, int] = {}  # 正規化タイトル → section_plans の位置
    parent_title_key = normalize_title(page.title)
    dropped_same_as_parent: List[str] = []
    for si, sec in enumerate(sections):
        indices = sorted(i for i in range(total_blocks) if owner[i] == si)
        if not indices:
            # 挙げたブロックが全て他の子と競合した／範囲外だった子は作らない
            continue
        raw_title = str(sec.get("title") or "").strip()
        sec_title = raw_title or "(無題)"
        title_key = normalize_title(sec_title)

        # 親と同名の子は作らない（健全性規則 2026-08-05）。ブロックは親に残る
        # ——入れ子が一段深くなるだけで整理が進まないため。やり直しはさせない
        # （同じ応答が返れば無限ループになる。まはー裁定）。
        if title_key == parent_title_key:
            dropped_same_as_parent.append(sec_title)
            continue

        # 同じ応答の中で同じタイトルが二度挙がったら 1 枚に束ねる
        # （同名ページを作れない、を応答内でも守る）。
        pos = plan_pos_by_title.get(title_key)
        if pos is not None:
            plan = section_plans[pos]
            plan["indices"] = sorted(set(plan["indices"]) | set(indices))
            plan["content"] = "".join(blocks[i] for i in plan["indices"])
            section_pos_map[si] = pos
            continue

        existing = existing_by_title.get(title_key)
        section_pos_map[si] = len(section_plans)
        plan_pos_by_title[title_key] = len(section_plans)
        section_plans.append({
            "title": sec_title,
            "summary": declared_summary.get(raw_title, ""),
            "indices": indices,
            "content": "".join(blocks[i] for i in indices),
            # 既存の同名の子がいれば新規作成せずそこへ追記する（apply_split）。
            "existing_child_id": existing.id if existing is not None else None,
        })
    # 捨てた子を指していた owner を親の残りへ寄せ、位置を section_plans 基準へ詰め直す
    owner = {i: section_pos_map.get(si, -1) for i, si in owner.items()}

    remaining_sorted = sorted(i for i in range(total_blocks) if owner[i] == -1)
    remaining_content = "".join(blocks[i] for i in remaining_sorted)

    # --- 保存則の機械検証: 文字レベルの復元 ---
    # 割当の正規化により保存則は構造的に成立しているので、これは LLM 応答の
    # 検査ではなく、この関数自身のリグレッション検査（ブロック分割・連結・
    # 正規化のいずれかを壊したら気付く）。
    # 復元規則: 各ブロックは割当先ページの本文中に index 昇順で連続して並ぶ。
    # よって元の index 順に、各出力本文を先頭から切り出して繋げば元本文に戻る。
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

    if dropped_same_as_parent:
        LOGGER.info(
            "[curation_ops] plan_split: 親と同名の子 %d 件を棄却 (page=%s title=%r)",
            len(dropped_same_as_parent), page_id, page.title,
        )

    return {
        "page_id": page.id,
        "title": page.title,
        "content": content,
        "sections": section_plans,
        "remaining_indices": remaining_sorted,
        "remaining_content": remaining_content,
        "total_blocks": total_blocks,
        "dropped_same_as_parent": dropped_same_as_parent,
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

    **同名の子ページは作らない**（健全性規則 2026-08-05）: 割り当て先の
    タイトルが既存の子と同じなら、新規作成せずそのページへ逐語で追記する。
    判定は**この関数が適用の直前に引き直す**——plan_split の印は手がかりで
    あって保証ではない（LLM の応答の質に依存しない物理拘束をここに置く）。

    Returns:
        dict: {
            "page_id": str,
            "sections": [{"title": str, "child_id": str, "block_count": int,
                          "appended": bool}],
            "remaining_block_count": int,
            "total_blocks": int,
            "dropped_same_as_parent": [str],
            "no_change": bool,   # 子が 1 枚も作られず親も変えなかった
        }

    Raises:
        ValueError: ページが見つからない、割当案作成後に本文が変更された
    """
    from sai_memory.memopedia.storage import get_children, get_page

    page_id = split_plan["page_id"]
    page = get_page(conn, page_id)
    if page is None:
        raise ValueError(f"execute_split: ページが見つかりません: {page_id}")
    _assert_active(conn, page.id, "split 対象")
    if (page.content or "") != split_plan["content"]:
        raise ValueError(
            f"execute_split: 割当案の作成後に本文が変更されています: {page_id}。"
            "この分割を棄却します。"
        )
    # 親のタイトルが変わっていたら棄却する。plan_split は「親と同名の子を作らない」
    # を**プラン時点の親タイトル**で判定しており、その後に親が改名されると同名の子が
    # 作れてしまう。ここで落とすと段落の行き先が消えて保存則が壊れるので、
    # 部分棄却ではなくプランごと棄却する（Codex 指摘 2026-08-05）。
    if page.title != split_plan.get("title"):
        raise ValueError(
            f"execute_split: 割当案の作成後にページ名が変更されています: {page_id}"
            f"（{split_plan.get('title')!r} → {page.title!r}）。この分割を棄却します。"
        )

    dropped_same_as_parent = list(split_plan.get("dropped_same_as_parent") or [])

    # 同名判定は適用の直前に引き直す（plan からの時間差・別経路の作成に耐える）。
    # 既に同名の兄弟が複数いる実データ（輪が回った後の棚）では、追記先が行順に
    # 依存すると同じ入力で結果が変わる。**いちばん古い 1 枚**に寄せる——統合の
    # 「残す側は古い方」と同じ向きで、重複が徐々に古い側へ畳まれていく
    # （Codex 指摘 2026-08-05）。
    children_by_title: Dict[str, Any] = {}
    for child in sorted(get_children(conn, page_id), key=lambda c: (c.created_at, c.id)):
        children_by_title.setdefault(normalize_title(child.title), child)

    # --- 逐語でブロックを移動（既存の同名の子があれば追記、無ければ作成） ---
    created_sections: List[Dict[str, Any]] = []
    for sec in split_plan["sections"]:
        title_key = normalize_title(sec["title"])
        existing = children_by_title.get(title_key)
        if existing is not None:
            # 既存本文は一文字も触らない（rstrip は末尾の改行・行末スペースを
            # 消す＝本文の改変。編纂は移動と結合のみ、が保存則）。区切りの空行を
            # 足すだけにする（Codex 指摘 2026-08-05）。
            base = existing.content or ""
            merged = (base + "\n\n" + sec["content"]) if base else sec["content"]
            memopedia.update_page(
                existing.id,
                content=merged,
                edit_source="curation",
            )
            created_sections.append({
                "title": existing.title,
                "child_id": existing.id,
                "block_count": len(sec["indices"]),
                "appended": True,
            })
            LOGGER.info(
                "[curation_ops] split: appended to existing child id=%s title=%r "
                "block_count=%d",
                existing.id, existing.title, len(sec["indices"]),
            )
            continue

        child_page = memopedia.create_page(
            parent_id=page_id,
            title=sec["title"],
            summary=sec.get("summary", ""),
            content=sec["content"],
            edit_source="curation",
        )
        children_by_title[title_key] = child_page
        created_sections.append({
            "title": sec["title"],
            "child_id": child_page.id,
            "block_count": len(sec["indices"]),
            "appended": False,
        })
        LOGGER.debug(
            "[curation_ops] split: created child page id=%s title=%r block_count=%d",
            child_page.id, sec["title"], len(sec["indices"]),
        )

    # 子が 1 枚も無い＝分けられなかった。親に触らず「変更なし」で返す
    # （空の導線を足して updated_at だけ動かすと、来歴に中身のない編集が残る）。
    if not created_sections:
        LOGGER.info(
            "[curation_ops] split: 分割先が 1 枚も無いため変更なし "
            "(page_id=%s total_blocks=%d dropped_same_as_parent=%d)",
            page_id, split_plan["total_blocks"], len(dropped_same_as_parent),
        )
        return {
            "page_id": page_id,
            "sections": [],
            "remaining_block_count": split_plan["total_blocks"],
            "total_blocks": split_plan["total_blocks"],
            "dropped_same_as_parent": dropped_same_as_parent,
            "no_change": True,
        }

    # --- 親は remaining ブロックだけ。子への導線は本文に書かない ---
    # 子の一覧は親子関係 (parent_id) から表示側が組み立てる（まはー裁定
    # 2026-08-05、memopedia_body_to_fragment.md §7 (a)。本文に書くと分割の
    # たびに前回の導線が積もり、消すにはペルソナの本文を削る操作になる）。
    memopedia.update_page(
        page_id,
        content=split_plan["remaining_content"],
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
        "dropped_same_as_parent": dropped_same_as_parent,
        "no_change": False,
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
            # 編纂は Memory weave 級の記憶仕事 — Chronicle と同じ解決チェーン
            # (persona.memory_weave_model → env MEMORY_WEAVE_MODEL → 組み込み
            # 既定) を saiverse.memory_weave_llm 経由で使う。ここに独自解決を
            # 書かないこと (2026-07-18: 存在しない persona.LIGHTWEIGHT_MODEL の
            # getattr でペルソナ設定が素通りし、グローバル既定へ貫通していた)。
            from saiverse.memory_weave_llm import get_memory_weave_client
            persona_obj = (getattr(manager, "personas", None) or {}).get(persona_id)
            _llm_client = get_memory_weave_client(persona_obj, purpose="curation")
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
            if kind == "merge":
                if len(refs) < 2:
                    raise ValueError(f"merge には 2 つの refs が必要 (got {refs})")
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
                sections = result.get("sections") or []
                if result.get("no_change"):
                    # 分割先が作れなかった＝失敗ではない。事実を事実として言う
                    # （ペルソナに「失敗しました」と嘘の報告を届けない）。
                    report_lines.append(
                        f"- [split] {page_ref} は分けられませんでした"
                        "（内容がひとまとまりで、分割先を作れませんでした）。"
                        "ページは変更していません。"
                    )
                else:
                    created = [s["title"] for s in sections if not s.get("appended")]
                    appended = [s["title"] for s in sections if s.get("appended")]
                    parts: List[str] = []
                    if created:
                        parts.append(
                            f"{len(created)} 件の子ページに分割"
                            f"（{', '.join(created[:3])}{'…' if len(created) > 3 else ''}）"
                        )
                    if appended:
                        parts.append(
                            f"既存の {len(appended)} 件へ追記"
                            f"（{', '.join(appended[:3])}{'…' if len(appended) > 3 else ''}）"
                        )
                    report_lines.append(
                        f"- [split] {page_ref} を{'、'.join(parts)}しました。"
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
