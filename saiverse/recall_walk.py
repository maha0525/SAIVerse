"""随意想起の中身 — 決定論的な連想歩行 (life_concept_map.md §9.3)。

種 (目的ノード参照等) から連想の「辺」を優先度付きで数ホップ歩き、鮮度・
再訪回数で重み付けて上位を束ねる。**LLM 生成コールは一切使わない** — 全て
SQL と既存のローカル埋め込み検索 (fastembed) で完結する (§9.3 の原則)。

実装した辺 (§9.3 の表との対応):

| 辺 | kind | 実体 |
|---|---|---|
| タグ直撃 | ``tag_direct`` | purpose_tags (sai_memory/purpose_tags.py) の seed 目的 → target |
| タグ共起 (2ホップ) | ``tag_cooccur`` | target に付いた別目的 → その目的の別 target |
| 木の構造 | ``tree`` | purpose_tree の親・子・兄弟・来歴 (promoted_from) |
| clip (旧 mark) | ``clip`` | purpose_ref 一致・未貼り付けのクリップ＝観測点 (sai_memory/clips.py) |
| 出来事所属 | ``episode`` | ORIGIN_REF / DIGEST_REF が seed に係る episode (saiverse/episodes.py)。target_ref が ``episode:N`` のタグは tag 辺で拾われる |
| 意味的近傍 | ``semantic`` | seed 目的ノードの title/intent をクエリにした埋め込み検索 (sai_memory/memory/recall.py semantic_recall。ローカル計算) |
| Memopedia 実体言及 | ``memopedia`` | (a) seed タイトルと完全一致するページ (b) 歩行で見つかったメッセージを編集来歴 (memopedia_page_edit_history の ref_start/ref_end_message_id) に持つページ |

§9.3 の「参照」辺 (成果物・origin_quote・source) のうち目的ノードの来歴
(promoted_from) は tree 辺に含めた。それ以外の汎用参照走査は未実装 (メッセージ
本文からの ref 抽出は保存時の索引が無く、全文走査になるため P4 では見送り)。

失敗の正直さ (§9.2): 何も見つからなければ空 items を返す。無理に埋めない。

パラメータ (辺の重み・既定 budget) はモジュール冒頭の定数にまとめてある。
これらは life_concept_map.md §9.3「残る決定: 辺の優先度・重みの初期値／歩行
予算の既定値」への**暫定回答**である — 辺種別の固定優先度は §9.3 の並び
(タグ直撃 > 木構造 > clip > 出来事 > 意味的近傍 > Memopedia) に従い、タグ共起
(2ホップ) はタグ系として木構造と clip の間に置いた。鮮度は半減期つき加点、
再訪回数は上限つき加点。実運用の手応えで調整されることを前提とする。

本モジュールは P4 時点ではどこからも呼ばれない (休眠)。配線先は随意想起
(memory_recall ツールの系譜のタグ鍵拡張 §9.2) と再開手続きの第二手。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from database.models import Episode
from sai_memory import clips as clips_store
from sai_memory import purpose_tags as tags_store
from sai_memory.memopedia import storage as memopedia_storage
from sai_memory.memory.recall import semantic_recall
from sai_memory.memory.storage import get_message
from saiverse import clock, episodes, purpose_tree, references

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# パラメータ (§9.3「残る決定」への暫定回答。module docstring 参照)
# ---------------------------------------------------------------------------

#: 辺種別の基礎スコア。§9.3 の固定優先度 (タグ直撃 > 木構造 > clip >
#: 出来事 > 意味的近傍 > Memopedia)。tag_cooccur は 2 ホップのタグ辺で
#: 木構造と clip の間に置いた。clip は旧 mark (観測点) 辺の後継。
EDGE_WEIGHTS: Dict[str, float] = {
    "tag_direct": 100.0,
    "tree": 80.0,
    "tag_cooccur": 70.0,
    "clip": 60.0,
    "episode": 50.0,
    "semantic": 40.0,
    "memopedia": 30.0,
}

#: 鮮度加点: RECENCY_WEIGHT * 0.5 ** (経過日数 / RECENCY_HALF_LIFE_DAYS)。
#: created_at が取れない候補 (目的ノード等) は加点 0。
RECENCY_WEIGHT = 10.0
RECENCY_HALF_LIFE_DAYS = 7.0

#: 再訪加点: REVISIT_WEIGHT * min(revisit_count, REVISIT_CAP)。
REVISIT_WEIGHT = 2.0
REVISIT_CAP = 5

#: 既定の歩行予算 (WalkBudget の既定値)。
DEFAULT_MAX_NODES = 12
DEFAULT_MAX_CHARS = 4000

#: snippet の決定論切り出し長 (本文先頭 N 文字)。
SNIPPET_CHARS = 120

#: 1 seed × 1 辺あたりの候補収集上限 (2 ホップの組合せ爆発の抑え)。
MAX_PER_EDGE = 8

#: 意味的近傍の topk と除外タグ (recall_snippet と同じ運用ノイズ除外)。
SEMANTIC_TOPK = 4
SEMANTIC_EXCLUDE_TAGS = ("handy_tool", "spell")


@dataclass(frozen=True)
class WalkBudget:
    """歩行予算 (§9.3「結果量の制御は歩行予算」)。行為のコストの一部 (§7)。"""

    max_nodes: int = DEFAULT_MAX_NODES
    max_chars: int = DEFAULT_MAX_CHARS


class WalkItem(NamedTuple):
    """歩行で見つかった 1 件。``path`` は seed からの到達経路 (参照の列)。"""

    ref: str
    kind: str          # EDGE_WEIGHTS のキー (どの辺で到達したか)
    snippet: str       # 本文先頭 SNIPPET_CHARS 文字の決定論切り出し
    score: float
    path: Tuple[str, ...]


@dataclass(frozen=True)
class WalkResult:
    """連想歩行の結果。見つからなければ items は空 (§9.2 失敗の正直さ)。"""

    items: List[WalkItem] = field(default_factory=list)
    truncated: bool = False


# ---------------------------------------------------------------------------
# 内部: スコアと snippet
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    ref: str
    kind: str
    snippet: str
    created_at: int          # 0 = 不明 (鮮度加点なし)
    revisit_count: int
    path: Tuple[str, ...]


def _now_epoch() -> int:
    return int(clock.now().timestamp())


def _score(cand: _Candidate, now_epoch: int) -> float:
    s = EDGE_WEIGHTS[cand.kind]
    if cand.created_at > 0:
        age_days = max(0.0, (now_epoch - cand.created_at) / 86400.0)
        s += RECENCY_WEIGHT * (0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS))
    s += REVISIT_WEIGHT * min(cand.revisit_count, REVISIT_CAP)
    return s


def _snippet(text: Optional[str]) -> str:
    """本文先頭 SNIPPET_CHARS 文字の決定論切り出し (改行は空白に潰す)。"""
    return " ".join((text or "").split())[:SNIPPET_CHARS]


def _resolve_target(
    manager: Any,
    adapter: Any,
    persona_id: str,
    ref: str,
) -> Tuple[str, int]:
    """target 参照から (snippet, created_at) を決定論で引く。

    解決できない kind / 実体なしは参照文字列そのものを snippet にして通す
    (タグ粗製濫造への頑健性 §9.3 — 一本の解決失敗で歩行を止めない)。
    """
    try:
        parsed = references.parse_ref(ref)
    except ValueError:
        return ref, 0
    conn = getattr(adapter, "conn", None)
    lock = getattr(adapter, "_db_lock", None)
    try:
        if parsed.kind == "message" and conn is not None and lock is not None:
            with lock:
                msg = get_message(conn, parsed.key)
            if msg is not None:
                return _snippet(msg.content), int(msg.created_at)
        elif parsed.kind == "episode":
            ep = episodes.get_by_ref(manager, persona_id, ref)
            label = " ".join(
                str(p) for p in (
                    f"[{ep.get('kind')}]", ep.get("building_id"), ep.get("origin_ref"),
                ) if p
            )
            return _snippet(label), int(ep.get("started_at") or 0)
        elif parsed.kind in ("track", "task"):
            node = purpose_tree.resolve_ref(manager, persona_id, ref)
            return _snippet(node.get("title")), 0
        elif parsed.kind == "memopedia" and conn is not None and lock is not None:
            with lock:
                page = memopedia_storage.get_page(conn, parsed.key)
            if page is not None:
                return _snippet(f"{page.title} — {page.summary}"), int(page.updated_at)
    except (episodes.EpisodeNotFoundError, purpose_tree.NodeNotFoundError):
        pass
    return ref, 0


def _page_ref(page: Any) -> str:
    key = page.short_id if page.short_id is not None else page.id
    return references.to_short_ref("memopedia", key)


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------


def walk(
    persona_id: str,
    seed_refs: Sequence[str],
    budget: Optional[WalkBudget] = None,
    *,
    manager: Any,
    adapter: Any,
) -> WalkResult:
    """種から辺を歩いて関連する過去を束ねる (§9.3 随意想起の中身)。

    Args:
        persona_id: 歩く主体のペルソナ。
        seed_refs: 種 (目的ノード参照＋机メモの ref)。統一文法の短縮参照
            (``track:N`` / ``task:N`` 等)。目的ノードに解決できない参照も
            タグ辺の鍵としては使われる (exact match)。
        budget: 歩行予算。None なら既定 (:class:`WalkBudget`)。
        manager: ``SessionLocal`` を持つ world 側の入口 (purpose_tree /
            episodes と同じ流儀)。
        adapter: ペルソナの SAIMemoryAdapter (memory.db = タグ・mark・
            メッセージ・Memopedia・埋め込みの置き場)。

    Returns:
        :class:`WalkResult`。何も見つからなければ空 items (§9.2 失敗の正直さ
        — 「思い出せない」をそのまま返す)。
    """
    budget = budget or WalkBudget()
    seeds: List[str] = []
    for r in seed_refs or []:
        text = str(r or "").strip()
        if text and text not in seeds:
            seeds.append(text)
    if not seeds:
        return WalkResult(items=[], truncated=False)
    seed_set = set(seeds)

    conn = getattr(adapter, "conn", None)
    lock = getattr(adapter, "_db_lock", None)
    has_memory = conn is not None and lock is not None

    candidates: List[_Candidate] = []

    def _add(
        ref: str,
        kind: str,
        snippet: str,
        created_at: int,
        revisit_count: int,
        path: Tuple[str, ...],
    ) -> None:
        if not ref or ref in seed_set:
            return  # 種そのものは「思い出す対象」ではない
        candidates.append(_Candidate(ref, kind, snippet, created_at, revisit_count, path))

    # --- seed の目的ノード解決 (tree / semantic / memopedia 辺の材料) ---
    seed_nodes: Dict[str, Dict[str, Any]] = {}
    for s in seeds:
        try:
            seed_nodes[s] = purpose_tree.resolve_ref(manager, persona_id, s)
        except purpose_tree.NodeNotFoundError:
            continue  # 目的ノードでない種 (机メモの ref 等) も合法

    # --- 辺 1: タグ直撃 + 2 ホップ共起 (purpose_tags) ---
    if has_memory:
        with lock:
            tags_store.init_purpose_tags_tables(conn)  # 冪等 (adapter 未配線のため)
        for s in seeds:
            with lock:
                direct = tags_store.list_by_purpose(conn, s)[:MAX_PER_EDGE]
            for tag in direct:
                snippet, target_created = _resolve_target(
                    manager, adapter, persona_id, tag.target_ref
                )
                _add(
                    tag.target_ref, "tag_direct", snippet,
                    target_created or tag.created_at, tag.revisit_count,
                    (s, tag.target_ref),
                )
                # 2 ホップ: target に付いた別目的 → その目的の別 target
                with lock:
                    cotags = tags_store.list_by_target(conn, tag.target_ref)[:MAX_PER_EDGE]
                for cotag in cotags:
                    other = cotag.purpose_ref
                    if other in seed_set:
                        continue
                    other_snip, _ = _resolve_target(manager, adapter, persona_id, other)
                    _add(
                        other, "tag_cooccur", other_snip,
                        cotag.created_at, cotag.revisit_count,
                        (s, tag.target_ref, other),
                    )
                    with lock:
                        hops = tags_store.list_by_purpose(conn, other)[:MAX_PER_EDGE]
                    for hop in hops:
                        if hop.target_ref == tag.target_ref:
                            continue
                        hop_snip, hop_created = _resolve_target(
                            manager, adapter, persona_id, hop.target_ref
                        )
                        _add(
                            hop.target_ref, "tag_cooccur", hop_snip,
                            hop_created or hop.created_at, hop.revisit_count,
                            (s, tag.target_ref, other, hop.target_ref),
                        )

    # --- 辺 2: 木の構造 (親子・兄弟・来歴 promoted_from) ---
    for s, node in seed_nodes.items():
        try:
            children = purpose_tree.list_children(manager, persona_id, s)
        except purpose_tree.NodeNotFoundError:
            children = []
        for child in children[:MAX_PER_EDGE]:
            _add(child["ref"], "tree", _snippet(child.get("title")), 0, 0, (s, child["ref"]))
        for origin_ref in (node.get("promoted_from") or [])[:MAX_PER_EDGE]:
            snip, created = _resolve_target(manager, adapter, persona_id, origin_ref)
            _add(origin_ref, "tree", snip, created, 0, (s, origin_ref))
        # 親と兄弟 (task が track に接がれている場合)
        if node.get("node_kind") == "task" and node.get("track_id"):
            try:
                parent = purpose_tree.resolve_ref(manager, persona_id, node["track_id"])
            except purpose_tree.NodeNotFoundError:
                parent = None
            if parent is not None:
                _add(
                    parent["ref"], "tree", _snippet(parent.get("title")),
                    0, 0, (s, parent["ref"]),
                )
                try:
                    siblings = purpose_tree.list_children(
                        manager, persona_id, parent["ref"]
                    )
                except purpose_tree.NodeNotFoundError:
                    siblings = []
                for sib in siblings[:MAX_PER_EDGE]:
                    if sib["ref"] == node.get("ref"):
                        continue
                    _add(
                        sib["ref"], "tree", _snippet(sib.get("title")),
                        0, 0, (s, parent["ref"], sib["ref"]),
                    )

    # --- 辺 3: clip (purpose_ref 一致・未貼り付けのクリップ = 旧 mark 観測点) ---
    if has_memory:
        with lock:
            open_clips = clips_store.list_clips(conn, unpasted_only=True)
        for clip in open_clips:
            if clip.purpose_ref not in seed_set:
                continue
            msg_ref = references.to_short_ref("message", clip.message_id)
            _add(
                msg_ref, "clip", _snippet(clip.quote or ""),
                clip.created_at, 0, (clip.purpose_ref, msg_ref),
            )

    # --- 辺 4: 出来事所属 (ORIGIN_REF / DIGEST_REF が seed に係る episode) ---
    db = manager.SessionLocal()
    try:
        rows = (
            db.query(Episode)
            .filter(
                Episode.PERSONA_ID == persona_id,
                (Episode.ORIGIN_REF.in_(seeds)) | (Episode.DIGEST_REF.in_(seeds)),
            )
            .order_by(Episode.STARTED_AT.desc())
            .limit(MAX_PER_EDGE * len(seeds))
            .all()
        )
        episode_hits = [
            (
                f"episode:{ep.SHORT_ID}" if ep.SHORT_ID is not None else ep.EPISODE_ID,
                ep.ORIGIN_REF if ep.ORIGIN_REF in seed_set else ep.DIGEST_REF,
                " ".join(str(p) for p in (f"[{ep.KIND}]", ep.BUILDING_ID, ep.DIGEST_REF) if p),
                int(ep.STARTED_AT or 0),
            )
            for ep in rows
        ]
    finally:
        db.close()
    for ep_ref, via_seed, label, started_at in episode_hits:
        _add(ep_ref, "episode", _snippet(label), started_at, 0, (via_seed, ep_ref))

    # --- 辺 5: 意味的近傍 (ローカル埋め込み検索。生成コールなし) ---
    if has_memory and getattr(adapter, "embedder", None) is not None:
        for s, node in seed_nodes.items():
            query = " ".join(
                str(node.get(k)) for k in ("title", "intent") if node.get(k)
            ).strip()
            if not query:
                continue
            try:
                with lock:
                    msgs = semantic_recall(
                        conn,
                        adapter.embedder,
                        query,
                        thread_id=None,
                        resource_id=None,
                        topk=SEMANTIC_TOPK,
                        range_before=0,
                        range_after=0,
                        scope="thread",
                        exclude_tags=list(SEMANTIC_EXCLUDE_TAGS),
                    )
            except Exception:
                # 埋め込みモデル欠落等。辺 1 本の不調で歩行全体は止めない
                LOGGER.warning(
                    "[recall_walk] semantic edge failed (persona=%s seed=%s)",
                    persona_id, s, exc_info=True,
                )
                continue
            for msg in msgs:
                msg_ref = references.to_short_ref("message", msg.id)
                _add(
                    msg_ref, "semantic", _snippet(msg.content),
                    int(msg.created_at), 0, (s, msg_ref),
                )

    # --- 辺 6: Memopedia 実体言及 ---
    if has_memory:
        # (a) seed 目的ノードのタイトルと完全一致するページ (決定論対応付け。
        #     自動リンク/明示リンクは §9.3 の残る決定 — exact match が暫定回答)
        for s, node in seed_nodes.items():
            title = (node.get("title") or "").strip()
            if not title:
                continue
            with lock:
                page = memopedia_storage.get_page_by_title(conn, title)
                if page is not None:
                    page = memopedia_storage.get_page(conn, page.id)  # short_id 補完
            if page is None:
                continue
            _add(
                _page_ref(page), "memopedia",
                _snippet(f"{page.title} — {page.summary}"),
                int(page.updated_at), 0, (s, _page_ref(page)),
            )
        # (b) 歩行で見つかったメッセージを編集来歴に持つページ (ページと
        #     メッセージの決定論リンクは edit history の ref_*_message_id)
        message_hits: Dict[str, Tuple[str, ...]] = {}
        for cand in candidates:
            try:
                parsed = references.parse_ref(cand.ref)
            except ValueError:
                continue
            if parsed.kind == "message" and parsed.key not in message_hits:
                message_hits[parsed.key] = cand.path
        for message_id, via_path in list(message_hits.items())[:MAX_PER_EDGE]:
            with lock:
                page_rows = conn.execute(
                    "SELECT DISTINCT page_id FROM memopedia_page_edit_history "
                    "WHERE ref_start_message_id = ? OR ref_end_message_id = ?",
                    (message_id, message_id),
                ).fetchall()
            for (page_id,) in page_rows:
                with lock:
                    page = memopedia_storage.get_page(conn, page_id)
                if page is None:
                    continue
                _add(
                    _page_ref(page), "memopedia",
                    _snippet(f"{page.title} — {page.summary}"),
                    int(page.updated_at), 0, via_path + (_page_ref(page),),
                )

    # --- 集計: dedup (最良スコア) → 決定論ソート → 予算で切る ---
    now_epoch = _now_epoch()
    best: Dict[str, Tuple[float, _Candidate]] = {}
    for cand in candidates:
        score = _score(cand, now_epoch)
        current = best.get(cand.ref)
        if current is None or score > current[0]:
            best[cand.ref] = (score, cand)

    ranked = sorted(
        best.values(),
        key=lambda pair: (-pair[0], -pair[1].created_at, pair[1].ref),
    )

    items: List[WalkItem] = []
    used_chars = 0
    truncated = False
    for score, cand in ranked:
        if len(items) >= budget.max_nodes:
            truncated = True
            break
        if used_chars + len(cand.snippet) > budget.max_chars:
            truncated = True
            break
        items.append(WalkItem(cand.ref, cand.kind, cand.snippet, score, cand.path))
        used_chars += len(cand.snippet)

    return WalkResult(items=items, truncated=truncated)
