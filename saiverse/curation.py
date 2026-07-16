"""編纂（curation）の検知層 — 決定論・ゼロ LLM コール。

P4-a の三層（検知 → 裁定 → 実行）のうち「検知」を担う。
判断材料（候補ページ・根拠・状況テキスト一行）を組み立て、
裁定（就寝判断相乗り）に渡す形式で返す。

実際の分割・統合（書き換え）は P4-a2 で実装する `sai_memory/curation_ops.py`
の領分——**このモジュールでは一切実行しない**。

P4-b 命名（テーマ立て）の検知も本モジュールが担う。
``detect_naming_candidates`` を参照。

設計詳細: ``docs/intent/concept_consolidation.md`` §P4-b 命名節
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 閾値定数（唯一の真実の源。他モジュールはここから import する）
# ---------------------------------------------------------------------------

#: 肥大とみなす content 文字数。この数を**超えた**ページが分割候補になる。
#: maintain_memopedia.py の SPLIT_THRESHOLD と memopedia_health.py の
#: 3000字超判定は両方ここを参照する（閾値の一元化）。
OVERSIZED_THRESHOLD: int = 5000

#: 過小とみなす content 文字数。この数**未満**かつ低参照のページが統合候補になる。
UNDERSIZED_THRESHOLD: int = 120

#: 「低参照」の日数基準。最終参照 or 最終更新がこの日数以上前なら低参照と見なす。
STALE_DAYS: int = 30

#: 類似とみなすキーワード共起数（以上）。
SIMILAR_MIN_KEYWORDS: int = 3

#: 状況テキストに提示する候補の最大件数（ノイズ制御）。
MAX_CANDIDATES: int = 3

#: P4-b 命名: 同 desire_type を持つ完了/休眠ノードの最小クラスタ件数。
#: この件数以上のクラスタが 1 テーマ候補になる。
NAMING_CLUSTER_MIN: int = 3

#: P4-b 命名: 1回の就寝判断に提示するテーマ候補の最大件数（ノイズ制御）。
NAMING_MAX_CANDIDATES: int = 1

#: memopedia_health.py の注意ゾーン境界（2000〜OVERSIZED の間）。
#: health レポートで「注意 (2000〜OVERSIZED 字)」セクションに使う。
HEALTH_LARGE_THRESHOLD: int = 2000

#: memopedia_health.py の「分割推奨」境界（OVERSIZED 字超）。
#: health レポートで「分割推奨」セクションに使う。
HEALTH_OVERSIZED_THRESHOLD: int = OVERSIZED_THRESHOLD


# ---------------------------------------------------------------------------
# 内部ヘルパ
# ---------------------------------------------------------------------------


def _now_epoch() -> int:
    """現在時刻 (epoch 秒)。仮想クロック尊重のため saiverse.clock を通す
    (一日シム・サンドボックスで STALE_DAYS 判定が現実時刻とズレないように)。"""
    try:
        from saiverse import clock
        return int(clock.now().timestamp())
    except Exception:
        return int(time.time())


def _stale_cutoff() -> int:
    """STALE_DAYS 日前の epoch 秒を返す（これより古ければ低参照）。"""
    return _now_epoch() - STALE_DAYS * 86400


def _short_id_label(short_id: Optional[int], page_id: str) -> str:
    """m:N 形式の参照ラベル。short_id が無ければ page_id 先頭8文字。"""
    if short_id is not None:
        return f"memopedia:{short_id}"
    return page_id[:8]


def _title_contains(title_a: str, title_b: str) -> bool:
    """title_b が title_a に包含されているかを正規化後に判定する。"""
    a = re.sub(r"\s+", "", title_a.lower())
    b = re.sub(r"\s+", "", title_b.lower())
    if not a or not b:
        return False
    return b in a


# ---------------------------------------------------------------------------
# ページ一覧取得（DB 直読み）
# ---------------------------------------------------------------------------


def _fetch_metabolizable_pages(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """metabolizable カテゴリの未削除・非 trunk ページ一覧を返す。

    返す辞書のキー:
        id, parent_id, title, category, content, keywords,
        created_at, updated_at, last_referenced_at, short_id, is_trunk
    """
    from sai_memory.memopedia.storage import category_keys

    cats = category_keys("metabolizable")
    if not cats:
        return []

    placeholders = ",".join("?" * len(cats))
    cur = conn.execute(
        f"""
        SELECT
            id, parent_id, title, category,
            COALESCE(content, '') AS content,
            COALESCE(keywords, '[]') AS keywords,
            created_at, updated_at,
            last_referenced_at,
            short_id,
            COALESCE(is_trunk, 0) AS is_trunk
        FROM memopedia_pages
        WHERE
            category IN ({placeholders})
            AND COALESCE(is_deleted, 0) = 0
        ORDER BY created_at
        """,
        cats,
    )
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]

    result: List[Dict[str, Any]] = []
    for row in rows:
        d = dict(zip(cols, row))
        try:
            d["keywords"] = [
                k for k in (json.loads(d["keywords"]) or [])
                if isinstance(k, str)
            ]
        except Exception:
            d["keywords"] = []
        result.append(d)
    return result


def _is_trunk(page: Dict[str, Any]) -> bool:
    return bool(page.get("is_trunk"))


def _has_real_parent(page: Dict[str, Any], page_map: Dict[str, Dict[str, Any]]) -> bool:
    """実親（非 trunk の parent）を持つかどうか。"""
    pid = page.get("parent_id")
    if pid is None:
        return False
    parent = page_map.get(pid)
    if parent is None:
        return False
    return not _is_trunk(parent)


# ---------------------------------------------------------------------------
# 候補検知（決定論）
# ---------------------------------------------------------------------------


def _detect_oversized(
    pages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """肥大候補: content 文字数 > OVERSIZED_THRESHOLD。trunk は除外。"""
    candidates = []
    for p in pages:
        if _is_trunk(p):
            continue
        clen = len(p["content"])
        if clen > OVERSIZED_THRESHOLD:
            label = _short_id_label(p.get("short_id"), p["id"])
            candidates.append({
                "op_id": f"split:{label}",
                "kind": "split",
                "refs": [label],
                "content_len": clen,
                "line": (
                    f"[肥大] {label}「{p['title']}」 {clen:,}字"
                    " — 子ページへの分割を提案"
                ),
            })
    # 大きい順で並べる（優先度）
    return sorted(candidates, key=lambda c: -c["content_len"])


def _detect_undersized(
    pages: List[Dict[str, Any]],
    page_map: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """過小候補: content < UNDERSIZED_THRESHOLD かつ STALE_DAYS 以上参照なし。

    **trunk 直下ページは候補にしない**（決定論の統合先が無い——設計の
    「決定論境界」の裁定どおり。親が trunk の場合は類似検知に委ねる）。
    """
    cutoff = _stale_cutoff()
    candidates = []
    for p in pages:
        if _is_trunk(p):
            continue
        clen = len(p["content"])
        if clen >= UNDERSIZED_THRESHOLD:
            continue
        # 最終参照 / 最終更新の新しい方
        last_activity = max(
            p.get("last_referenced_at") or 0,
            p.get("updated_at") or 0,
        )
        if last_activity >= cutoff:
            continue
        # trunk 直下（実親なし）は候補にしない
        if not _has_real_parent(p, page_map):
            continue
        pid = p.get("parent_id")
        parent = page_map.get(pid) if pid else None
        if parent is None:
            # 防御: _has_real_parent() で除外済みのはずだが、親が解決できない
            # ページは実行契約 (refs=[survivor, absorbed]) を満たせないため
            # 候補にしない。
            continue
        label = _short_id_label(p.get("short_id"), p["id"])
        parent_label = _short_id_label(parent.get("short_id"), parent["id"])
        parent_title = parent["title"]
        stale_days = (_now_epoch() - last_activity) // 86400 if last_activity else STALE_DAYS
        candidates.append({
            "op_id": f"fold:{label}",
            "kind": "fold",
            # 実行契約 (run_pending_plans): refs[0]=survivor(親), refs[1]=absorbed(過小ページ)
            "refs": [parent_label, label],
            "content_len": clen,
            "line": (
                f"[過小] {label}「{p['title']}」 {clen}字・"
                f"{stale_days}日間参照なし"
                f" — 親ページ{parent_label}「{parent_title}」への統合を提案"
            ),
        })
    return candidates


def _detect_similar(
    pages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """類似候補: 同カテゴリ内ページペアでキーワード共起 >= SIMILAR_MIN_KEYWORDS
    または一方のタイトルが他方のタイトルを包含する。

    **残す側の決定論規則**: 古い方（created_at が小さい方）が残る。
    trunk は除外。最大候補数はここではフィルタしない（呼び出し元で行う）。
    """
    # trunk を除いた非 trunk ページだけを対象にする
    active = [p for p in pages if not _is_trunk(p)]

    # カテゴリ別にグループ化
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for p in active:
        by_cat.setdefault(p["category"], []).append(p)

    candidates = []
    seen_pairs: set = set()

    for cat_pages in by_cat.values():
        n = len(cat_pages)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = cat_pages[i], cat_pages[j]
                pair_key = tuple(sorted([a["id"], b["id"]]))
                if pair_key in seen_pairs:
                    continue

                # キーワード共起
                kw_a = set(a["keywords"])
                kw_b = set(b["keywords"])
                shared_kws = kw_a & kw_b
                kw_match = len(shared_kws) >= SIMILAR_MIN_KEYWORDS

                # タイトル包含（どちらかが他方を包含）
                title_match = (
                    _title_contains(a["title"], b["title"])
                    or _title_contains(b["title"], a["title"])
                )

                if not (kw_match or title_match):
                    continue

                seen_pairs.add(pair_key)

                # 残す側 = 古い方（created_at が小さい方）
                if a["created_at"] <= b["created_at"]:
                    keep, discard = a, b
                else:
                    keep, discard = b, a

                label_keep = _short_id_label(keep.get("short_id"), keep["id"])
                label_discard = _short_id_label(discard.get("short_id"), discard["id"])

                if kw_match:
                    reason = (
                        f"キーワード{len(shared_kws)}語共起"
                        f"（{'/'.join(list(shared_kws)[:4])}）"
                    )
                else:
                    reason = "タイトル包含"

                candidates.append({
                    "op_id": f"merge:{label_keep}+{label_discard}",
                    "kind": "merge",
                    "refs": [label_keep, label_discard],
                    "line": (
                        f"[類似] {label_keep}「{keep['title']}」と"
                        f" {label_discard}「{discard['title']}」"
                        f" — {reason}。統合を提案"
                    ),
                })

    return candidates


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------


def detect_curation_candidates(
    conn: sqlite3.Connection,
    persona_id: str,
) -> List[Dict[str, Any]]:
    """編纂候補を検知して最大 MAX_CANDIDATES 件返す（決定論・ゼロ LLM コール）。

    対象: per-persona memory.db の memopedia ページで、カテゴリが
    ``category_keys("metabolizable")`` のみ。trunk・削除済みは除外。

    優先順: 肥大（大きい順）→ 類似 → 過小

    返す辞書のキー (各候補):
        ``op_id``: 決定論の一意文字列（enum 値に使う）
        ``kind``:  "split" | "merge" | "fold"
        ``refs``:  ["m:N", ...] — 操作対象ページの参照ラベル
        ``line``:  状況テキスト 1 行

    実装制約:
        - **LLM 呼び出しは一切しない**
        - **ページの書き換えは一切しない**
        - 分割・統合の実処理は P4-a2 の ``sai_memory/curation_ops.py`` の領分
    """
    try:
        all_pages = _fetch_metabolizable_pages(conn)
    except Exception:
        LOGGER.warning(
            "[curation] failed to fetch metabolizable pages (persona=%s)",
            persona_id, exc_info=True,
        )
        return []

    page_map = {p["id"]: p for p in all_pages}

    oversized = _detect_oversized(all_pages)
    similar = _detect_similar(all_pages)
    undersized = _detect_undersized(all_pages, page_map)

    # 優先: 肥大 → 類似 → 過小
    all_candidates = oversized + similar + undersized

    # 重複 op_id を除去（同じ操作が複数枠に入り込まないように）
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for c in all_candidates:
        if c["op_id"] not in seen:
            seen.add(c["op_id"])
            unique.append(c)

    candidates = unique[:MAX_CANDIDATES]

    LOGGER.debug(
        "[curation] persona=%s: %d candidates detected "
        "(oversized=%d, similar=%d, undersized=%d, after_cap=%d)",
        persona_id,
        len(unique),
        len(oversized),
        len(similar),
        len(undersized),
        len(candidates),
    )
    return candidates


# ---------------------------------------------------------------------------
# P4-b: 命名候補の検知（決定論・ゼロ LLM コール）
# ---------------------------------------------------------------------------


def _fetch_existing_theme_member_refs(conn: Any) -> set:
    """既存テーマページの member_refs に含まれる task ref を収集して返す。

    root_theme 配下のページの metadata.member_refs から task:N を取り出す。
    冪等除外に使用する。

    ``conn`` は SQLAlchemy Session ではなく sqlite3.Connection として扱う試みは
    しない——persona DB (memory.db) は sqlite3.Connection。main DB (persona_task)
    は manager 経由の SQLAlchemy 世界で別物。ここでは memory.db 側で
    root_theme ページの metadata を読む。
    """
    result: set = set()
    try:
        cur = conn.execute(
            "SELECT metadata FROM memopedia_pages "
            "WHERE parent_id = 'root_theme' AND COALESCE(is_deleted, 0) = 0"
        )
        for (meta_json,) in cur.fetchall():
            if not meta_json:
                continue
            try:
                meta = json.loads(meta_json)
            except Exception:
                continue
            for ref in meta.get("member_refs") or []:
                if isinstance(ref, str):
                    result.add(ref)
    except Exception:
        pass
    return result


def detect_naming_candidates(
    manager: Any,
    persona_id: str,
) -> List[Dict[str, Any]]:
    """命名（テーマ立て）の候補を検知して最大 NAMING_MAX_CANDIDATES 件返す。

    対象: persona の main DB 上の persona_task で stage が 'completed' または
    'dormant' のノード。同一 desire_type を持つノードが NAMING_CLUSTER_MIN 件
    以上あればそれが 1 クラスタ（テーマ候補）。既にテーマページの member_refs に
    含まれている task:N は除外する（冪等）。

    ``manager``: SAIVerseManager インスタンス（manager.SessionLocal と
    ``personas`` dict を持つことを前提とする）。memory.db への conn は
    personas[persona_id].sai_memory.conn から取る。

    返す dict のキー:
        ``cluster_id``:   "naming:<desire_type>" 形式の一意文字列
        ``kind``:         "naming"
        ``member_refs``:  ["task:N", ...]
        ``line``:         状況テキスト 1 行

    実装制約:
        - **LLM 呼び出しは一切しない**
        - **ページの書き換えは一切しない**
        - 命名の実行（create_theme_page 呼び出し）は judgment_finalize の領分
    """
    from saiverse.persona_task_manager import (
        PersonaTaskManager,
        STAGE_COMPLETED,
        STAGE_DORMANT,
    )

    ptm = PersonaTaskManager(manager.SessionLocal)

    # 完了・休眠ノードを取得
    try:
        completed = ptm.list_tasks(
            persona_id,
            stage=STAGE_COMPLETED,
            include_steps=False,
        )
        dormant = ptm.list_tasks(
            persona_id,
            stage=STAGE_DORMANT,
            include_steps=False,
        )
    except Exception:
        LOGGER.warning(
            "[curation/naming] failed to list tasks (persona=%s)",
            persona_id, exc_info=True,
        )
        return []

    all_tasks = completed + dormant

    # 既にテーマ化済みの task ref を取得（冪等除外）
    existing_refs: set = set()
    try:
        persona_obj = (getattr(manager, "personas", None) or {}).get(persona_id)
        mem_conn = getattr(getattr(persona_obj, "sai_memory", None), "conn", None)
        if mem_conn is not None:
            existing_refs = _fetch_existing_theme_member_refs(mem_conn)
    except Exception:
        LOGGER.debug(
            "[curation/naming] could not read existing theme member_refs (persona=%s)",
            persona_id,
        )

    # desire_type がある行だけクラスタリング（NULL は対象外にする）
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for task in all_tasks:
        dtype = task.get("desire_type")
        if not dtype:
            continue
        task_ref = task.get("task_ref")
        if not task_ref:
            continue
        if task_ref in existing_refs:
            continue
        by_type.setdefault(str(dtype), []).append(task)

    # NAMING_CLUSTER_MIN 件以上のクラスタだけ候補化
    candidates: List[Dict[str, Any]] = []
    for dtype, tasks in by_type.items():
        if len(tasks) < NAMING_CLUSTER_MIN:
            continue
        member_refs = [t["task_ref"] for t in tasks]
        refs_display = ", ".join(member_refs[:6])
        if len(member_refs) > 6:
            refs_display += f"…（他 {len(member_refs) - 6} 件）"
        candidates.append({
            "cluster_id": f"naming:{dtype}",
            "kind": "naming",
            "member_refs": member_refs,
            "line": (
                f"[テーマ候補] 「{dtype}」の完了・休眠ノード {len(member_refs)} 件"
                f" ({refs_display})"
                " — 名前を与えるとテーマとして棚に立ちます"
            ),
        })

    # 最大 NAMING_MAX_CANDIDATES 件（ノイズ制御。P4-b: 就寝判断が重くならないように）
    candidates = candidates[:NAMING_MAX_CANDIDATES]

    LOGGER.debug(
        "[curation/naming] persona=%s: %d naming candidates detected",
        persona_id, len(candidates),
    )
    return candidates
