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
import sqlite3
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 閾値定数（唯一の真実の源。他モジュールはここから import する）
# ---------------------------------------------------------------------------

#: 肥大とみなす content 文字数。この数を**超えた**ページが分割候補になる。
#: memopedia_health.py の 3000字超判定もここを参照する（閾値の一元化）。
OVERSIZED_THRESHOLD: int = 5000

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


def _short_id_label(short_id: Optional[int], page_id: str) -> str:
    """m:N 形式の参照ラベル。short_id が無ければ page_id 先頭8文字。"""
    if short_id is not None:
        return f"memopedia:{short_id}"
    return page_id[:8]


def _title_contains(title_a: str, title_b: str) -> bool:
    """title_b が title_a に包含されているかを正規化後に判定する。

    正規化は分割側 (`sai_memory.curation_ops.normalize_title`) と共有する
    ——「同じ名前」の定義が二箇所でずれないように。
    """
    from sai_memory.curation_ops import normalize_title

    a = normalize_title(title_a)
    b = normalize_title(title_b)
    if not a or not b:
        return False
    return b in a


# ---------------------------------------------------------------------------
# ページ一覧取得（DB 直読み）
# ---------------------------------------------------------------------------


def _fetch_metabolizable_pages(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """metabolizable カテゴリの未削除ページ一覧を返す。

    **trunk も含める**（各検知が個別に除外する）——`_has_real_parent` が
    「親が棚かどうか」を引くために、親側が一覧に居ることが前提。

    返す辞書のキー:
        id, parent_id, title, summary, category, content, keywords,
        created_at, updated_at, last_referenced_at, short_id, is_trunk

    ``summary`` は統合後の文字数を事前に確定するために要る——実行側
    (`build_merged_content`) は消える側の summary も本文へ逐語で連結するので、
    ここで欠けると検知の見積もりが実行結果とずれる。
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
            COALESCE(summary, '') AS summary,
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
        ORDER BY created_at, id
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


def fetch_page_structure(conn: sqlite3.Connection) -> Dict[str, Any]:
    """木の形の判定に使う索引を**全ページ**から作る（削除済み・全カテゴリ込み）。

    候補一覧（`_fetch_metabolizable_pages`）は未削除かつ編纂対象カテゴリだけなので、
    そこから親子を引くと解決できない親が「親なし」に見える＝**吸われてよい**方へ
    倒れる（fail-open）。実際に起きうる: `Memopedia.delete_page` は soft-delete で
    **子に波及しない**ため、閉架された親の下に現役の子が残る（Codex 指摘 2026-08-05）。

    Returns:
        {"parent_of": {id: parent_id}, "trunks": {id...}, "live_parents": {id...}}
        - ``parent_of``: 削除済みも含む全ページの親
        - ``trunks``: is_trunk のページ（棚）
        - ``live_parents``: **現役の**子を 1 枚以上持つページ
          （閉架された子はごみ箱の中なので「根を張っている」に数えない）
    """
    parent_of: Dict[str, Optional[str]] = {}
    trunks: set = set()
    live_parents: set = set()
    cur = conn.execute(
        "SELECT id, parent_id, COALESCE(is_trunk, 0), COALESCE(is_deleted, 0) "
        "FROM memopedia_pages"
    )
    for page_id, parent_id, is_trunk, is_deleted in cur.fetchall():
        parent_of[page_id] = parent_id
        if is_trunk:
            trunks.add(page_id)
        if parent_id and not is_deleted:
            live_parents.add(parent_id)
    return {"parent_of": parent_of, "trunks": trunks, "live_parents": live_parents}


def _has_real_parent(page: Dict[str, Any], structure: Dict[str, Any]) -> bool:
    """実親（棚でない親）にぶら下がっているか。

    親 id はあるのに索引で解決できない（dangling）場合は **True を返す**
    ——分からないものを「親なし」と決めつけて吸わせない fail-closed。
    """
    pid = page.get("parent_id")
    if pid is None:
        return False
    if pid not in structure["parent_of"]:
        return True
    return pid not in structure["trunks"]


def _can_be_absorbed(
    page: Dict[str, Any],
    structure: Dict[str, Any],
) -> bool:
    """統合で「消える側」になれるページか（健全性規則 2026-08-05・まはー裁定）。

    **実際のページ（trunk でない親）を親にも子にも持たないページだけ**が
    消える側になれる。木として根を張り始めたページが別のページに吸われること
    自体を禁じる規則で、親子・兄弟の除外を包含した上で「別の木の子を横から
    吸う」も塞ぐ。

    実機 aifi_city_a では、分割が作った子が類似判定（タイトル包含）で人物
    ページへ吸い戻され、太った親がまた分割されて同名ページが増える輪が
    回っていた。分割の子のタイトルは必ず親の名前を含むため、この吸い戻しは
    事故ではなく構造的に毎回起きる。
    """
    if _has_real_parent(page, structure):
        return False
    return page["id"] not in structure["live_parents"]


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
                # 統合側が「分割待ちのページ」を判定するための実 id
                # （閾値の条件をここ以外に書かないため）
                "page_id": p["id"],
                "content_len": clen,
                "line": (
                    f"[肥大] {label}「{p['title']}」 {clen:,}字"
                    " — 子ページへの分割を提案"
                ),
            })
    # 大きい順で並べる（優先度）
    return sorted(candidates, key=lambda c: -c["content_len"])


# 過小候補（fold: 過小ページを親へ畳む）は 2026-08-05 に**機構ごと撤去**。
# 統合先を親に固定した時点で対象が「実親を持つページ」に縮み、実機ではそれが
# 全て分割の子だった＝構造的に分割の巻き戻ししかできない。「小さく枯れたページを
# 片付ける」という当初の目的には、棚直下の小さいページ（実機 198 枚）に届かないので
# 最初から達しない。実績も 0 件。経緯: docs/issues/curation_duplicate_pages_loop.md


def _split_touched_ids(structure: Dict[str, Any], oversized_ids: set) -> set:
    """分割が触りうるページの id 集合＝肥大ページ本体とその直下の子。

    分割は残りブロックで親を書き換え、同名の既存の子があればそこへ追記する。
    どちらも同じ晩の統合と衝突しうるので、統合候補から予約除外する。
    子の列挙は候補一覧ではなく全ページの索引から引く（`get_children` は
    カテゴリで絞らないので、追記先も候補一覧の外にありうる）。
    """
    touched = set(oversized_ids)
    touched.update(
        page_id for page_id, parent_id in structure["parent_of"].items()
        if parent_id in oversized_ids
    )
    return touched


def _detect_similar(
    pages: List[Dict[str, Any]],
    structure: Dict[str, Any],
    split_touched: set,
) -> List[Dict[str, Any]]:
    """類似候補: 同カテゴリ内ページペアでキーワード共起 >= SIMILAR_MIN_KEYWORDS
    または一方のタイトルが他方のタイトルを包含する。

    **残す側の決定論規則**: 古い方（created_at、同秒なら id の小さい方）が残る。
    trunk は除外。最大候補数はここではフィルタしない（呼び出し元で行う）。

    健全性規則（2026-08-05・まはー裁定）で、次のペアは候補にしない:

    - 消える側が実際のページを親か子に持つ（`_can_be_absorbed`）
    - どちらかが分割待ち（肥大候補）**またはその子**——分割はその晩に親を
      書き換え、同名の既存の子へ追記もするので、統合の見積もりが後から狂う
    - 統合した結果が肥大する——結果の文字数は逐語連結なので事前に確定する
    - **1 ページ 1 晩 1 操作**: 残す側にも消える側にも、既に他の候補で使った
      ページは使わない。候補は晩の最初に一度だけ組むので 2 件目は 1 件目の結果を
      知らない。実機では残す側の重複で 1,444字 → 7,779字 まで積み上がった。
      消える側の重複はさらに悪く、A←B と B←C が同じ晩に承認されると閉架済みの
      B へ C の本文が流し込まれ、現役ページに届かない（`get_page` は
      soft-delete を弾かないので実行側では気付けない。Codex 指摘 2026-08-05）
    """
    from sai_memory.curation_ops import build_merged_content

    # trunk を除いた非 trunk ページだけを対象にする
    active = [p for p in pages if not _is_trunk(p)]

    # カテゴリ別にグループ化
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for p in active:
        by_cat.setdefault(p["category"], []).append(p)

    candidates = []
    seen_pairs: set = set()
    used_pages: set = set()

    for cat_pages in by_cat.values():
        n = len(cat_pages)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = cat_pages[i], cat_pages[j]
                pair_key = tuple(sorted([a["id"], b["id"]]))
                if pair_key in seen_pairs:
                    continue

                # 分割待ちのページとその子は統合に使わない（残す側・消える側とも）。
                # 子まで外すのは、分割が同名の既存の子へ**追記する**ようになったため
                # ——同じ晩に「親を分割して子へ追記」と「その子を残す側にした統合」が
                # 両方走ると、統合の見積もり（検知時点の本文）より子が太る。
                # 「1 ページ 1 晩 1 操作」を操作の種類を跨いで成立させる
                # （Codex 指摘 2026-08-05）。
                if a["id"] in split_touched or b["id"] in split_touched:
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

                # 残す側 = 古い方（created_at が同秒なら id で決める。
                # tie-breaker が無いと同秒作成のページで候補列が入力順に依存し、
                # 決定論（同入力 → 同 op_id 列）が壊れる）
                if (a["created_at"], a["id"]) <= (b["created_at"], b["id"]):
                    keep, discard = a, b
                else:
                    keep, discard = b, a

                # 消える側は「木として根を張っていない」ページに限る。
                # 向きの入れ替えはしない——残す側は古い方という決定論規則が
                # 先にあり、そこを崩すと同じペアの扱いが日によって変わる。
                if not _can_be_absorbed(discard, structure):
                    continue

                # 統合した結果が肥大するなら統合しない（統合が分割を呼ばない）
                merged_len = len(build_merged_content(
                    survivor_content=keep["content"],
                    absorbed_title=discard["title"],
                    absorbed_summary=discard.get("summary") or "",
                    absorbed_content=discard["content"],
                ))
                if merged_len > OVERSIZED_THRESHOLD:
                    continue

                # 1 ページ 1 晩 1 操作（残す側・消える側の両方を使用済みにする）
                if keep["id"] in used_pages or discard["id"] in used_pages:
                    continue

                seen_pairs.add(pair_key)
                used_pages.add(keep["id"])
                used_pages.add(discard["id"])

                label_keep = _short_id_label(keep.get("short_id"), keep["id"])
                label_discard = _short_id_label(discard.get("short_id"), discard["id"])

                if kw_match:
                    # set の走査順はプロセスごとに変わる（PYTHONHASHSEED）ので
                    # 並べ替えてから切る——根拠文まで決定論にする
                    reason = (
                        f"キーワード{len(shared_kws)}語共起"
                        f"（{'/'.join(sorted(shared_kws)[:4])}）"
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

    優先順: 肥大（大きい順）→ 類似

    返す辞書のキー (各候補):
        ``op_id``: 決定論の一意文字列（enum 値に使う）
        ``kind``:  "split" | "merge"
        ``refs``:  ["m:N", ...] — 操作対象ページの参照ラベル
        ``line``:  状況テキスト 1 行

    実装制約:
        - **LLM 呼び出しは一切しない**
        - **ページの書き換えは一切しない**
        - 分割・統合の実処理は P4-a2 の ``sai_memory/curation_ops.py`` の領分

    **候補どうしの整合はここで取る**（健全性規則 2026-08-05・まはー裁定
    「もっと前に見て候補から外せる」）: 分割待ちのページを統合に使わない、
    同じ残す側を二度使わない、といった規則は実行時ではなくこの関数の中で
    満たす——候補は晩の最初に一度だけ組まれ、実行はそれを順に適用するだけ
    なので、リストを組む時点が整合を取れる唯一の場所。
    """
    try:
        all_pages = _fetch_metabolizable_pages(conn)
        # 木の形は候補一覧ではなく全ページから引く（閉架・対象外カテゴリの親を
        # 「親なし」と誤読して吸わせないため。fetch_page_structure の説明を参照）
        structure = fetch_page_structure(conn)
    except Exception:
        LOGGER.warning(
            "[curation] failed to fetch metabolizable pages (persona=%s)",
            persona_id, exc_info=True,
        )
        return []

    oversized = _detect_oversized(all_pages)
    oversized_ids = {c["page_id"] for c in oversized}
    similar = _detect_similar(
        all_pages, structure, _split_touched_ids(structure, oversized_ids)
    )

    # 優先: 肥大 → 類似
    all_candidates = oversized + similar

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
        "(oversized=%d, similar=%d, after_cap=%d)",
        persona_id,
        len(unique),
        len(oversized),
        len(similar),
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
